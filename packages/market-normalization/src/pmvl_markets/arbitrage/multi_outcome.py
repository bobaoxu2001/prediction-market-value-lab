"""Multi-outcome (complete-set) arbitrage across an event's outcomes, both ways.

An event with N mutually exclusive outcomes can be traded as a basket in either
direction, and the two have *different* guarantee requirements:

**Long the basket** - buy YES on every outcome. Exactly one resolves YES, so the
payout is $1. Profitable when the executable asks plus costs sum below $1.

**Short the basket** - buy NO on every outcome. Exactly one resolves YES, so N-1 of
the NO legs pay, and the payout is $(N-1). Profitable when the NO asks plus costs sum
below N-1.

Scanning only the long side leaves real edge unexamined, and on live data it is the
side where edge does *not* appear: of twelve complete, fully-priced baskets measured
on 14 September 2026, zero were gross-profitable long and one was gross-profitable
short.

The properties each direction needs:

*Mutually exclusive* - load-bearing for BOTH. If two outcomes can both resolve YES,
the long basket pays $2 on a $1 premise and the short basket pays only $(N-2).

*Exhaustive* - load-bearing for the LONG basket only. If an unlisted
"Other"/"None of the above" outcome wins, every listed leg resolves NO and the long
basket pays $0 - a total loss. The short basket is the opposite: every leg paying
means it collects $N rather than $(N-1), so a missing outcome can only *help* it.
Requiring exhaustiveness to short would reject sound positions for a risk that runs
the other way.

Polymarket's ``negRisk`` events are constructed as an exhaustive partition with
shared collateral, which is why they are the only case treated as guaranteed for the
long side. A Kalshi event whose strikes merely look like a partition is labelled
:attr:`ArbitrageLabel.NOT_GUARANTEED` unless the venue asserts the property that
direction needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.timeutil import humanize_seconds
from pmvl_shared.enums import ArbitrageKind, ArbitrageLabel, RuleCompatibility, Side
from pmvl_shared.money import ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import (
    ArbLeg,
    ArbitrageResult,
    NormalizedMarket,
    OrderBook,
    outcome_label,
)
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
    """Trade an event's outcomes as a complete basket, in whichever direction pays.

    ``expected_outcome_count`` is the number of outcomes the event is known to have.
    If fewer legs are supplied the scan refuses to run, because a partial basket is
    not a complete set: the missing outcomes hold real probability mass, and pricing
    only the cheap subset would report a guaranteed profit that does not exist.

    Both directions are priced and the better net result is returned. They cannot
    both be profitable - long needs the asks to sum below $1 and short needs the NO
    asks to sum below $(N-1), and the bid-ask spread sits between them - so this is a
    search, not a choice between two live positions.
    """
    if len(legs) < 2:
        return None
    if expected_outcome_count is not None and len(legs) < expected_outcome_count:
        return None

    best: ArbitrageResult | None = None
    for side, payout_per_set in (
        (Side.YES, ONE),
        # Shorting pays for every leg but the winner.
        (Side.NO, Decimal(len(legs) - 1)),
    ):
        result = _scan_basket(
            legs, side, payout_per_set,
            event_title=event_title,
            mutually_exclusive=mutually_exclusive,
            exhaustive=exhaustive,
            negative_risk=negative_risk,
            max_sets=max_sets,
        )
        if result is None:
            continue
        if best is None or result.net_profit_per_set > best.net_profit_per_set:
            best = result
    return best


def _scan_basket(
    legs: list[OutcomeLeg],
    side: Side,
    payout_per_set: Decimal,
    *,
    event_title: str,
    mutually_exclusive: bool,
    exhaustive: bool,
    negative_risk: bool,
    max_sets: Decimal,
) -> ArbitrageResult | None:
    """Price one direction of the basket."""
    settings = get_settings()

    # Every leg must be quotable; a partition with an unquotable member cannot be
    # completed, and buying the rest is a directional position.
    tops: list[Decimal] = []
    for leg in legs:
        top = leg.book.best_ask(side)
        if top is None:
            return None
        tops.append(top)

    if sum(tops, ZERO) >= payout_per_set:
        return None

    sets = _max_matched_sets(legs, side, payout_per_set, max_sets=max_sets)
    if sets <= 0:
        return None

    gross_cost = ZERO
    total_fees = ZERO
    arb_legs: list[ArbLeg] = []

    for leg in legs:
        quote = executable_quote(leg.book, side, sets)
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
                side=side,
                price=quote.average_price,
                size_available=quote.filled_size,
                fee_per_contract=quantize_usd(safe_div(fee, sets)),
                token_id=(
                    leg.market.yes_token_id if side == Side.YES else leg.market.no_token_id
                ),
                outcome_label=outcome_label(leg.market, side),
            )
        )

    slippage = sum(
        (leg.market.tick_size * Decimal(settings.slippage_ticks) for leg in legs), ZERO
    ) * sets
    transfer = sum(
        (transfer_cost_per_contract(leg.market.platform, sets) for leg in legs), ZERO
    ) * sets

    total_cost = gross_cost + total_fees + slippage + transfer
    payout = sets * payout_per_set
    net_profit = payout - total_cost
    if net_profit <= 0:
        return None

    risk_flags: list[str] = []
    label = ArbitrageLabel.EXECUTABLE

    shorting = side == Side.NO
    if negative_risk:
        # Polymarket negative-risk events are constructed as an exhaustive partition
        # with shared collateral, which is exactly the guarantee this needs.
        risk_flags.append(
            "negative-risk event: outcomes are a venue-enforced exhaustive partition"
        )
    else:
        # Exhaustiveness is load-bearing one way only. Shorting collects on every leg
        # but the winner, so an unlisted outcome winning means ALL legs pay and the
        # basket collects $N instead of $(N-1) - the missing outcome helps. Going
        # long, the same event pays nothing at all.
        missing = []
        if not mutually_exclusive:
            missing.append("mutual exclusivity")
        if not exhaustive and not shorting:
            missing.append("exhaustiveness")
        if missing:
            label = ArbitrageLabel.NOT_GUARANTEED
            consequence = (
                "if two outcomes can both resolve YES, two legs pay nothing and the "
                "basket collects less than it is priced for"
                if shorting
                else "if an unlisted outcome can occur, every leg can resolve NO and "
                "the position loses its entire cost"
            )
            risk_flags.insert(
                0,
                "the venue does not assert " + " or ".join(missing)
                + f" for these outcomes; {consequence}",
            )
    if shorting and not exhaustive:
        risk_flags.append(
            "the outcome set is not asserted exhaustive, which for a short basket is "
            "in your favour: an unlisted winner pays every leg"
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
    # Every leg must actually accept an order. A basket is only a complete set if
    # *all* of it can be bought: an open market that has stopped accepting orders
    # leaves the buyer holding the other legs naked, which is a directional bet on
    # the outcomes they did manage to fill. Open status does not imply this - the
    # venues suspend order entry around settlement while the market is still listed.
    blocked = [
        leg.market.platform_market_id
        for leg in legs
        if not leg.market.accepting_orders
    ]
    if blocked:
        label = ArbitrageLabel.EXECUTION_RISK
        risk_flags.append(
            f"{len(blocked)} of {len(legs)} legs are not accepting orders "
            f"({', '.join(blocked[:3])}); the basket cannot be completed"
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
        title=(
            f"{event_title} - "
            f"{'short' if side == Side.NO else 'long'} complete set across "
            f"{len(legs)} outcomes"
        ),
        legs=arb_legs,
        gross_edge_per_set=quantize_usd(payout_per_set - safe_div(gross_cost, sets)),
        total_cost_per_set=quantize_usd(per_set_total),
        net_profit_per_set=quantize_usd(payout_per_set - per_set_total),
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
            "payout_per_set": str(quantize_usd(payout_per_set)),
            "direction": "short" if side == Side.NO else "long",
            "n_legs": len(legs),
        },
        provenance=legs[0].market.provenance,
    )


def _max_matched_sets(
    legs: list[OutcomeLeg],
    side: Side,
    payout_per_set: Decimal,
    *,
    max_sets: Decimal,
) -> Decimal:
    """Largest N where every leg fills at N contracts and the basket stays profitable.

    Bisects on N. The total cost of a complete set rises monotonically with N (each
    leg's VWAP is non-decreasing in size), so the profitable region is a prefix.
    """
    capacities = []
    for leg in legs:
        capacity = sum((lvl.size for lvl in leg.book.asks(side)), ZERO)
        if capacity <= 0:
            return ZERO
        capacities.append(capacity)
    hi = min(min(capacities), max_sets)
    if hi <= 0:
        return ZERO

    def total_at(size: Decimal) -> Decimal | None:
        total = ZERO
        for leg in legs:
            quote = executable_quote(leg.book, side, size)
            if quote is None or quote.filled_size < size:
                return None
            total += quote.average_price
        return total

    top = total_at(hi)
    if top is not None and top < payout_per_set:
        return hi

    lo = Decimal("0")
    for _ in range(24):
        mid = (lo + hi) / Decimal("2")
        value = total_at(mid)
        if value is not None and value < payout_per_set:
            lo = mid
        else:
            hi = mid
    return lo.quantize(Decimal("0.01"))
