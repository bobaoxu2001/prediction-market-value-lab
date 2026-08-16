"""Binary complete-set arbitrage within a single market.

Buying one YES and one NO of the same binary market guarantees exactly $1 at
settlement, because precisely one of them pays. If the pair can be bought for less
than $1 including all costs, the difference is locked in.

Both legs are walked **in parallel** so the quantities stay matched. Taking 100 YES
and only 60 NO is not an arbitrage - it is a 40-contract naked long position wearing
an arbitrage's clothes.
"""

from __future__ import annotations

from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.timeutil import humanize_seconds
from pmvl_shared.enums import ArbitrageKind, ArbitrageLabel, RuleCompatibility, Side
from pmvl_shared.money import ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import ArbitrageResult, ArbLeg, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds, utcnow

from ..pricing.fees import taker_fee
from ..pricing.orderbook import executable_quote


def scan_complete_set(
    market: NormalizedMarket,
    book: OrderBook,
    *,
    market_id: int | None = None,
    max_sets: Decimal = Decimal("10000"),
) -> ArbitrageResult | None:
    """Find a profitable YES+NO pair in one market, if one exists.

    Returns ``None`` when no pair clears $1 after costs. Returns a *labelled* result
    when a raw price edge exists but something disqualifies it from being called
    executable - a stale book, or depth too thin to matter.
    """
    settings = get_settings()

    yes_top = book.best_ask(Side.YES)
    no_top = book.best_ask(Side.NO)
    if yes_top is None or no_top is None:
        return None

    # Quick reject on top-of-book before doing any real work.
    if yes_top + no_top >= ONE:
        return None

    sets, gross_cost = _walk_matched(book, max_sets=max_sets)
    if sets <= 0:
        return None

    yes_quote = executable_quote(book, Side.YES, sets)
    no_quote = executable_quote(book, Side.NO, sets)
    if yes_quote is None or no_quote is None:
        return None

    yes_fee = taker_fee(
        market.platform, sets, yes_quote.average_price,
        rate=market.fee_rate, fee_type=market.fee_type,
    )
    no_fee = taker_fee(
        market.platform, sets, no_quote.average_price,
        rate=market.fee_rate, fee_type=market.fee_type,
    )

    # Slippage: one tick of latency padding per leg.
    slippage = market.tick_size * Decimal(settings.slippage_ticks) * Decimal("2") * sets
    # Both legs are on the same venue, so a single transfer covers the position.
    from ..pricing.execution import capital_cost_per_contract, transfer_cost_per_contract

    transfer = transfer_cost_per_contract(market.platform, sets) * sets
    # The capital for the pair is locked until the market's own resolution, the
    # same cost the cross-platform scanner charges against its later leg.
    # Omitting it overstated complete-set profit for long-dated markets and
    # contradicted the "pessimistic at every step" cost-stack claim.
    capital = (
        capital_cost_per_contract(
            yes_quote.average_price + no_quote.average_price,
            market.expected_resolution_time,
        )
        * sets
    )

    total_cost = gross_cost + yes_fee + no_fee + slippage + transfer + capital
    payout = sets * ONE
    net_profit = payout - total_cost

    if net_profit <= 0:
        return None

    per_set_gross = safe_div(gross_cost, sets)
    per_set_total = safe_div(total_cost, sets)
    quote_age = age_seconds(book.observed_at)

    risk_flags: list[str] = []
    label = ArbitrageLabel.EXECUTABLE

    if quote_age is not None and quote_age > settings.max_quote_age_seconds:
        label = ArbitrageLabel.STALE_QUOTE
        risk_flags.append(
            f"quote is {humanize_seconds(quote_age)} old "
            f"(limit {humanize_seconds(settings.max_quote_age_seconds)})"
        )
    if total_cost < settings.min_orderbook_notional_usd:
        label = ArbitrageLabel.INSUFFICIENT_LIQUIDITY
        risk_flags.append(
            f"total executable size is only ${total_cost:.2f}; below the "
            f"${settings.min_orderbook_notional_usd} minimum to be worth acting on"
        )
    if not market.accepting_orders:
        label = ArbitrageLabel.EXECUTION_RISK
        risk_flags.append("market is not currently accepting orders")

    return ArbitrageResult(
        kind=ArbitrageKind.COMPLETE_SET,
        label=label,
        title=f"{market.title} - YES+NO complete set",
        legs=[
            ArbLeg(
                platform=market.platform,
                platform_market_id=market.platform_market_id,
                market_id=market_id,
                title=market.title,
                side=Side.YES,
                price=yes_quote.average_price,
                size_available=yes_quote.filled_size,
                fee_per_contract=quantize_usd(safe_div(yes_fee, sets)),
                token_id=market.yes_token_id,
            ),
            ArbLeg(
                platform=market.platform,
                platform_market_id=market.platform_market_id,
                market_id=market_id,
                title=market.title,
                side=Side.NO,
                price=no_quote.average_price,
                size_available=no_quote.filled_size,
                fee_per_contract=quantize_usd(safe_div(no_fee, sets)),
                token_id=market.no_token_id,
            ),
        ],
        gross_edge_per_set=quantize_usd(ONE - per_set_gross),
        total_cost_per_set=quantize_usd(per_set_total),
        net_profit_per_set=quantize_usd(ONE - per_set_total),
        max_executable_sets=sets,
        max_net_profit=quantize_usd(net_profit),
        capital_required=quantize_usd(total_cost),
        net_roi=quantize_usd(safe_div(net_profit, total_cost)),
        # Both legs are the same contract on the same venue, so the settlement rules
        # are identical by construction - there is no cross-venue rule risk here.
        rule_compatibility=RuleCompatibility.IDENTICAL,
        risk_flags=risk_flags,
        quote_age_seconds=int(quote_age) if quote_age is not None else None,
        expected_resolution_time=market.expected_resolution_time,
        cost_breakdown={
            "gross_cost": str(quantize_usd(gross_cost)),
            "yes_fee": str(quantize_usd(yes_fee)),
            "no_fee": str(quantize_usd(no_fee)),
            "slippage": str(quantize_usd(slippage)),
            "transfer": str(quantize_usd(transfer)),
            "capital": str(quantize_usd(capital)),
            "payout": str(quantize_usd(payout)),
        },
        provenance=market.provenance,
    )


def _walk_matched(book: OrderBook, *, max_sets: Decimal) -> tuple[Decimal, Decimal]:
    """Walk YES and NO ask ladders together while the pair stays under $1.

    Returns ``(sets, gross_cost)``. Stops as soon as the next matched pair would cost
    $1 or more - buying past that point destroys the guarantee.
    """
    yes_levels = list(book.yes_asks)
    no_levels = list(book.no_asks)
    if not yes_levels or not no_levels:
        return ZERO, ZERO

    i = j = 0
    yes_left = yes_levels[0].size
    no_left = no_levels[0].size
    sets = ZERO
    cost = ZERO

    while i < len(yes_levels) and j < len(no_levels) and sets < max_sets:
        pair_price = yes_levels[i].price + no_levels[j].price
        if pair_price >= ONE:
            break
        take = min(yes_left, no_left, max_sets - sets)
        if take <= 0:
            break
        sets += take
        cost += take * pair_price
        yes_left -= take
        no_left -= take
        if yes_left <= 0:
            i += 1
            if i < len(yes_levels):
                yes_left = yes_levels[i].size
        if no_left <= 0:
            j += 1
            if j < len(no_levels):
                no_left = no_levels[j].size

    return sets, cost
