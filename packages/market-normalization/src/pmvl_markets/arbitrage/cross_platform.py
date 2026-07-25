"""Cross-platform arbitrage between two verified-matching markets.

Buy YES on the cheaper venue and NO on the other. If the two markets settle on the
same event under the same rules, exactly one leg pays $1 and the pair is riskless -
*provided the rules really are the same*.

That proviso is the whole problem, and it is why :func:`scan_cross_platform` will not
return :attr:`ArbitrageLabel.EXECUTABLE` unless the match verdict is
``RuleCompatibility.IDENTICAL``. Everything else is surfaced with an explicit label
naming what could go wrong. A pair of markets that *look* the same but settle on
different sources, cutoffs, or measurement bases is not an arbitrage; it is an
unhedged bet on the rules agreeing.

Costs charged beyond fees and slippage:

* **Execution risk.** The legs cannot fill simultaneously across two venues. Between
  the first and second fill the price can move, leaving a naked position. This is
  charged per set as a real cost, not waved away.
* **Transfer cost.** Capital must sit on both venues; the Polygon leg amortises a
  bridge/gas allowance.
* **Capital cost.** Both legs are locked until resolution.
"""

from __future__ import annotations

from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.enums import ArbitrageKind, ArbitrageLabel, RuleCompatibility, Side
from pmvl_shared.money import ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import ArbitrageResult, ArbLeg, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds, hours_until, utcnow

from ..matching.verify import MatchVerdict
from ..pricing.execution import capital_cost_per_contract, transfer_cost_per_contract
from ..pricing.fees import taker_fee
from ..pricing.orderbook import executable_quote

#: Label applied when the rules are not an exact match, ordered by severity.
_LABEL_FOR_COMPATIBILITY = {
    RuleCompatibility.IDENTICAL: ArbitrageLabel.EXECUTABLE,
    RuleCompatibility.EQUIVALENT: ArbitrageLabel.RULE_MISMATCH_RISK,
    RuleCompatibility.SIMILAR: ArbitrageLabel.NOT_GUARANTEED,
    RuleCompatibility.INCOMPATIBLE: ArbitrageLabel.NOT_GUARANTEED,
}


def scan_cross_platform(
    market_a: NormalizedMarket,
    book_a: OrderBook,
    market_b: NormalizedMarket,
    book_b: OrderBook,
    verdict: MatchVerdict,
    *,
    market_a_id: int | None = None,
    market_b_id: int | None = None,
    match_id: int | None = None,
    max_sets: Decimal = Decimal("10000"),
) -> ArbitrageResult | None:
    """Scan both leg orientations of a matched pair and return the better one."""
    if verdict.rule_compatibility == RuleCompatibility.INCOMPATIBLE:
        return None

    best: ArbitrageResult | None = None
    for side_a, side_b in ((Side.YES, Side.NO), (Side.NO, Side.YES)):
        # Inverted polarity means the other venue's YES corresponds to this one's NO,
        # so the hedging leg is the *same* nominal side rather than the opposite.
        effective_b = _flip(side_b) if verdict.polarity_inverted else side_b
        result = _scan_orientation(
            market_a, book_a, side_a,
            market_b, book_b, effective_b,
            verdict,
            market_a_id=market_a_id, market_b_id=market_b_id,
            match_id=match_id, max_sets=max_sets,
        )
        if result is None:
            continue
        if best is None or result.net_profit_per_set > best.net_profit_per_set:
            best = result
    return best


def _flip(side: Side) -> Side:
    return Side.NO if side == Side.YES else Side.YES


def _scan_orientation(
    market_a: NormalizedMarket,
    book_a: OrderBook,
    side_a: Side,
    market_b: NormalizedMarket,
    book_b: OrderBook,
    side_b: Side,
    verdict: MatchVerdict,
    *,
    market_a_id: int | None,
    market_b_id: int | None,
    match_id: int | None,
    max_sets: Decimal,
) -> ArbitrageResult | None:
    settings = get_settings()

    top_a = book_a.best_ask(side_a)
    top_b = book_b.best_ask(side_b)
    if top_a is None or top_b is None:
        return None
    if top_a + top_b >= ONE:
        return None

    sets, gross_cost = _walk_matched_pair(
        book_a, side_a, book_b, side_b, max_sets=max_sets
    )
    if sets <= 0:
        return None

    quote_a = executable_quote(book_a, side_a, sets)
    quote_b = executable_quote(book_b, side_b, sets)
    if quote_a is None or quote_b is None:
        return None

    fee_a = taker_fee(
        market_a.platform, sets, quote_a.average_price,
        rate=market_a.fee_rate, fee_type=market_a.fee_type,
    )
    fee_b = taker_fee(
        market_b.platform, sets, quote_b.average_price,
        rate=market_b.fee_rate, fee_type=market_b.fee_type,
    )

    slippage = (
        market_a.tick_size * Decimal(settings.slippage_ticks)
        + market_b.tick_size * Decimal(settings.slippage_ticks)
    ) * sets

    transfer = (
        transfer_cost_per_contract(market_a.platform, sets)
        + transfer_cost_per_contract(market_b.platform, sets)
    ) * sets

    # The legs settle at different times if the venues resolve at different times;
    # charge capital cost against the later of the two.
    later_resolution = max(
        [t for t in (market_a.expected_resolution_time, market_b.expected_resolution_time) if t],
        default=None,
    )
    capital = (
        capital_cost_per_contract(quote_a.average_price + quote_b.average_price, later_resolution)
        * sets
    )

    # Execution risk: the two legs cannot fill atomically across venues.
    execution_risk = settings.cross_platform_execution_risk_usd * sets

    total_cost = gross_cost + fee_a + fee_b + slippage + transfer + capital + execution_risk
    payout = sets * ONE
    net_profit = payout - total_cost
    if net_profit <= 0:
        return None

    per_set_total = safe_div(total_cost, sets)
    per_set_gross = safe_div(gross_cost, sets)

    age_a = age_seconds(book_a.observed_at)
    age_b = age_seconds(book_b.observed_at)
    worst_age = max([a for a in (age_a, age_b) if a is not None], default=None)

    label = _LABEL_FOR_COMPATIBILITY[verdict.rule_compatibility]
    risk_flags = list(verdict.mismatch_reasons)

    if verdict.rule_compatibility != RuleCompatibility.IDENTICAL:
        risk_flags.insert(
            0,
            "settlement rules are not an exact match; this is NOT a guaranteed profit",
        )
    if worst_age is not None and worst_age > settings.max_quote_age_seconds:
        label = ArbitrageLabel.STALE_QUOTE
        risk_flags.append(f"oldest quote is {worst_age:.0f}s old")
    if total_cost < settings.min_orderbook_notional_usd:
        label = ArbitrageLabel.INSUFFICIENT_LIQUIDITY
        risk_flags.append(f"only ${total_cost:.2f} of matched depth available")
    if not (market_a.accepting_orders and market_b.accepting_orders):
        label = ArbitrageLabel.EXECUTION_RISK
        risk_flags.append("at least one venue is not accepting orders")

    # A large gap in resolution timing means capital is tied up unevenly and one leg
    # can settle long before the other, which is an execution risk in its own right.
    hours_a = hours_until(market_a.expected_resolution_time)
    hours_b = hours_until(market_b.expected_resolution_time)
    if hours_a is not None and hours_b is not None and abs(hours_a - hours_b) > 24:
        risk_flags.append(
            f"legs resolve {abs(hours_a - hours_b):.0f}h apart; capital is not released together"
        )
        if label == ArbitrageLabel.EXECUTABLE:
            label = ArbitrageLabel.EXECUTION_RISK

    return ArbitrageResult(
        kind=ArbitrageKind.CROSS_PLATFORM,
        label=label,
        title=(
            f"{market_a.platform.value} {side_a.value.upper()} / "
            f"{market_b.platform.value} {side_b.value.upper()}: {market_a.title[:80]}"
        ),
        legs=[
            ArbLeg(
                platform=market_a.platform,
                platform_market_id=market_a.platform_market_id,
                market_id=market_a_id,
                title=market_a.title,
                side=side_a,
                price=quote_a.average_price,
                size_available=quote_a.filled_size,
                fee_per_contract=quantize_usd(safe_div(fee_a, sets)),
                token_id=market_a.yes_token_id if side_a == Side.YES else market_a.no_token_id,
            ),
            ArbLeg(
                platform=market_b.platform,
                platform_market_id=market_b.platform_market_id,
                market_id=market_b_id,
                title=market_b.title,
                side=side_b,
                price=quote_b.average_price,
                size_available=quote_b.filled_size,
                fee_per_contract=quantize_usd(safe_div(fee_b, sets)),
                token_id=market_b.yes_token_id if side_b == Side.YES else market_b.no_token_id,
            ),
        ],
        gross_edge_per_set=quantize_usd(ONE - per_set_gross),
        total_cost_per_set=quantize_usd(per_set_total),
        net_profit_per_set=quantize_usd(ONE - per_set_total),
        max_executable_sets=sets,
        max_net_profit=quantize_usd(net_profit),
        capital_required=quantize_usd(total_cost),
        net_roi=quantize_usd(safe_div(net_profit, total_cost)),
        rule_compatibility=verdict.rule_compatibility,
        risk_flags=risk_flags,
        quote_age_seconds=int(worst_age) if worst_age is not None else None,
        expected_resolution_time=later_resolution,
        cost_breakdown={
            "gross_cost": str(quantize_usd(gross_cost)),
            "fee_a": str(quantize_usd(fee_a)),
            "fee_b": str(quantize_usd(fee_b)),
            "slippage": str(quantize_usd(slippage)),
            "transfer": str(quantize_usd(transfer)),
            "capital_cost": str(quantize_usd(capital)),
            "execution_risk": str(quantize_usd(execution_risk)),
            "payout": str(quantize_usd(payout)),
            "match_confidence": str(verdict.match_confidence),
        },
        match_id=match_id,
        provenance=market_a.provenance,
    )


def _walk_matched_pair(
    book_a: OrderBook,
    side_a: Side,
    book_b: OrderBook,
    side_b: Side,
    *,
    max_sets: Decimal,
) -> tuple[Decimal, Decimal]:
    """Walk two books in lockstep while the combined pair costs under $1."""
    levels_a = list(book_a.asks(side_a))
    levels_b = list(book_b.asks(side_b))
    if not levels_a or not levels_b:
        return ZERO, ZERO

    i = j = 0
    left_a = levels_a[0].size
    left_b = levels_b[0].size
    sets = ZERO
    cost = ZERO

    while i < len(levels_a) and j < len(levels_b) and sets < max_sets:
        pair_price = levels_a[i].price + levels_b[j].price
        if pair_price >= ONE:
            break
        take = min(left_a, left_b, max_sets - sets)
        if take <= 0:
            break
        sets += take
        cost += take * pair_price
        left_a -= take
        left_b -= take
        if left_a <= 0:
            i += 1
            if i < len(levels_a):
                left_a = levels_a[i].size
        if left_b <= 0:
            j += 1
            if j < len(levels_b):
                left_b = levels_b[j].size

    return sets, cost
