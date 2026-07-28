"""Multi-outcome (complete-set) arbitrage across an event's outcomes.

When an event's outcomes are mutually exclusive **and** exhaustive, buying YES on
every outcome guarantees exactly $1: precisely one of them resolves YES. If the sum
of executable asks plus costs is below $1, the difference is locked in.

Both properties are load-bearing and neither may be assumed:

*Exhaustive* - if an "Other"/"None of the above" outcome exists but is not listed,
buying every listed outcome can pay $0. Polymarket's ``negRisk`` events are
constructed to be exhaustive, which is why they are the only case treated as
guaranteed here. A Kalshi event whose strikes merely look like a partition is
labelled :attr:`ArbitrageLabel.NOT_GUARANTEED` unless the venue asserts exclusivity.

*Mutually exclusive* - if two outcomes can both resolve YES, the sum can exceed $1
in payout terms and the "arbitrage" is really a correlated bet.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.timeutil import humanize_seconds
from pmvl_shared.enums import ArbitrageKind, ArbitrageLabel, RuleCompatibility, Side
from pmvl_shared.money import ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import ArbitrageResult, ArbLeg, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds

from ..pricing.execution import transfer_cost_per_contract
from ..pricing.fees import taker_fee
from ..pricing.orderbook import executable_quote


@dataclass
class OutcomeLeg:
    market: NormalizedMarket
    book: OrderBook
    market_id: int | None = None


def scan_multi_outcome(
    legs: list[OutcomeLeg],
    *,
    event_title: str,
    mutually_exclusive: bool,
    exhaustive: bool,
    negative_risk: bool = False,
    expected_outcome_count: int | None = None,
    max_sets: Decimal = Decimal("10000"),
) -> ArbitrageResult | None:
    """Buy YES across every outcome of an event for less than $1 in total.

    ``expected_outcome_count`` is the number of outcomes the event is known to have.
    If fewer legs are supplied the scan refuses to run, because a partial basket is
    not a complete set: the missing outcomes hold real probability mass, and pricing
    only the cheap subset would report a guaranteed profit that does not exist.
    """
    settings = get_settings()
    if len(legs) < 2:
        return None
    if expected_outcome_count is not None and len(legs) < expected_outcome_count:
        return None

    # Every leg must be quotable; a partition with an unquotable member cannot be
    # completed, and buying the rest is a directional position.
    tops: list[Decimal] = []
    for leg in legs:
        top = leg.book.best_ask(Side.YES)
        if top is None:
            return None
        tops.append(top)

    if sum(tops, ZERO) >= ONE:
        return None

    sets = _max_matched_sets(legs, max_sets=max_sets)
    if sets <= 0:
        return None

    gross_cost = ZERO
    total_fees = ZERO
    arb_legs: list[ArbLeg] = []

    for leg in legs:
        quote = executable_quote(leg.book, Side.YES, sets)
        if quote is None or quote.filled_size < sets:
            return None
        gross_cost += quote.average_price * sets
        fee = taker_fee(
            leg.market.platform, sets, quote.average_price,
            rate=leg.market.fee_rate, fee_type=leg.market.fee_type,
        )
        total_fees += fee
        arb_legs.append(
            ArbLeg(
                platform=leg.market.platform,
                platform_market_id=leg.market.platform_market_id,
                market_id=leg.market_id,
                title=leg.market.title,
                side=Side.YES,
                price=quote.average_price,
                size_available=quote.filled_size,
                fee_per_contract=quantize_usd(safe_div(fee, sets)),
                token_id=leg.market.yes_token_id,
            )
        )

    slippage = sum(
        (leg.market.tick_size * Decimal(settings.slippage_ticks) for leg in legs), ZERO
    ) * sets
    transfer = sum(
        (transfer_cost_per_contract(leg.market.platform, sets) for leg in legs), ZERO
    ) * sets

    total_cost = gross_cost + total_fees + slippage + transfer
    payout = sets * ONE
    net_profit = payout - total_cost
    if net_profit <= 0:
        return None

    risk_flags: list[str] = []
    label = ArbitrageLabel.EXECUTABLE

    if negative_risk:
        # Polymarket negative-risk events are constructed as an exhaustive partition
        # with shared collateral, which is exactly the guarantee this needs.
        risk_flags.append(
            "negative-risk event: outcomes are a venue-enforced exhaustive partition"
        )
    elif not (mutually_exclusive and exhaustive):
        label = ArbitrageLabel.NOT_GUARANTEED
        missing = []
        if not mutually_exclusive:
            missing.append("mutual exclusivity")
        if not exhaustive:
            missing.append("exhaustiveness")
        risk_flags.insert(
            0,
            "the venue does not assert " + " or ".join(missing)
            + " for these outcomes; if an unlisted outcome can occur, every leg can "
            "resolve NO and the position loses its entire cost",
        )

    ages = [age_seconds(leg.book.observed_at) for leg in legs]
    worst_age = max([a for a in ages if a is not None], default=None)
    if worst_age is not None and worst_age > settings.max_quote_age_seconds:
        label = ArbitrageLabel.STALE_QUOTE
        risk_flags.append(f"oldest leg quote is {humanize_seconds(worst_age)} old")
    if total_cost < settings.min_orderbook_notional_usd:
        label = ArbitrageLabel.INSUFFICIENT_LIQUIDITY
        risk_flags.append(f"only ${total_cost:.2f} of matched depth across all legs")
    if len(legs) > 6:
        risk_flags.append(
            f"{len(legs)} legs must all fill; execution risk grows with leg count"
        )

    per_set_total = safe_div(total_cost, sets)
    resolution = max(
        [leg.market.expected_resolution_time for leg in legs
         if leg.market.expected_resolution_time],
        default=None,
    )

    return ArbitrageResult(
        kind=ArbitrageKind.MULTI_OUTCOME,
        label=label,
        title=f"{event_title} - complete set across {len(legs)} outcomes",
        legs=arb_legs,
        gross_edge_per_set=quantize_usd(ONE - safe_div(gross_cost, sets)),
        total_cost_per_set=quantize_usd(per_set_total),
        net_profit_per_set=quantize_usd(ONE - per_set_total),
        max_executable_sets=sets,
        max_net_profit=quantize_usd(net_profit),
        capital_required=quantize_usd(total_cost),
        net_roi=quantize_usd(safe_div(net_profit, total_cost)),
        rule_compatibility=(
            RuleCompatibility.IDENTICAL if negative_risk else RuleCompatibility.EQUIVALENT
        ),
        risk_flags=risk_flags,
        quote_age_seconds=int(worst_age) if worst_age is not None else None,
        expected_resolution_time=resolution,
        cost_breakdown={
            "gross_cost": str(quantize_usd(gross_cost)),
            "fees": str(quantize_usd(total_fees)),
            "slippage": str(quantize_usd(slippage)),
            "transfer": str(quantize_usd(transfer)),
            "payout": str(quantize_usd(payout)),
            "n_legs": len(legs),
        },
        provenance=legs[0].market.provenance,
    )


def _max_matched_sets(legs: list[OutcomeLeg], *, max_sets: Decimal) -> Decimal:
    """Largest N where every leg can be bought at N contracts and the sum stays < $1.

    Bisects on N. The total cost of a complete set rises monotonically with N (each
    leg's VWAP is non-decreasing in size), so the profitable region is a prefix.
    """
    capacities = []
    for leg in legs:
        capacity = sum((lvl.size for lvl in leg.book.asks(Side.YES)), ZERO)
        if capacity <= 0:
            return ZERO
        capacities.append(capacity)
    hi = min(min(capacities), max_sets)
    if hi <= 0:
        return ZERO

    def total_at(size: Decimal) -> Decimal | None:
        total = ZERO
        for leg in legs:
            quote = executable_quote(leg.book, Side.YES, size)
            if quote is None or quote.filled_size < size:
                return None
            total += quote.average_price
        return total

    top = total_at(hi)
    if top is not None and top < ONE:
        return hi

    lo = Decimal("0")
    for _ in range(24):
        mid = (lo + hi) / Decimal("2")
        value = total_at(mid)
        if value is not None and value < ONE:
            lo = mid
        else:
            hi = mid
    return lo.quantize(Decimal("0.01"))
