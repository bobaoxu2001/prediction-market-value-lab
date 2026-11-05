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
from decimal import ROUND_FLOOR, Decimal

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
    if expected_outcome_count is not None and len(legs) != expected_outcome_count:
        # Two-sided on purpose. Too few legs is a partial basket. Too many means a
        # leg duplicates an outcome - a re-keyed market, a stale count, or one of
        # Polymarket's augmented-negative-risk placeholder slots - and then the
        # duplicated outcome winning kills two legs, so a short basket pays $(N-2)
        # against the $(N-1) it was priced for. The venue's count is the authority;
        # validating it and then deriving the payout from len(legs) anyway would
        # discard exactly the check that was just made.
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
        # Compared on return per dollar of capital, not per set. A short set earns on
        # ~$(N-1) of capital where a long set earns on ~$1, so the raw per-set figures
        # are not comparable: a short basket with the larger per-set profit can tie up
        # four times the capital for materially worse return.
        if best is None or result.net_roi > best.net_roi:
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

    sets = _max_matched_sets(legs, side, payout_per_set, settings, max_sets=max_sets)
    if sets <= 0:
        return None

    priced = _price_at(legs, side, sets, settings)
    if priced is None:
        return None
    gross_cost, total_fees, slippage, transfer, arb_legs = priced

    total_cost = gross_cost + total_fees + slippage + transfer
    payout = sets * payout_per_set
    net_profit = payout - total_cost
    if net_profit <= 0:
        return None

    risk_flags: list[str] = []
    label = ArbitrageLabel.EXECUTABLE
    shorting = side == Side.NO

    # ---- what each direction actually needs ------------------------------
    #
    # The two properties form a duality, and applying either to both directions is
    # wrong in one of them. Settled by hand over every assignment of winners:
    #
    #   EXHAUSTIVENESS binds the LONG basket only. If nothing listed wins, every leg
    #   resolves NO and the long basket pays $0 - a total loss. The short basket
    #   collects on every leg and takes $N instead of $(N-1): the missing outcome
    #   HELPS it.
    #
    #   EXCLUSIVITY binds the SHORT basket only. If two outcomes both win, two legs
    #   pay nothing and the short basket takes $(N-2) against a $(N-1) premise. The
    #   long basket receives $2 for its $1 - a windfall, not a risk. An earlier
    #   version demoted long baskets for this and printed the exhaustiveness
    #   consequence as the reason, which was wrong twice over.
    #
    # ``guaranteed`` is tracked separately from ``label`` because it describes the
    # PAYOUT PREMISE, while the gates below describe fill quality. A scalar label
    # let a stale timestamp overwrite "this is not a locked box" with "refresh the
    # quote", which inverts the kind of problem being reported.
    guaranteed = True

    def withdraw(reason: str) -> None:
        nonlocal guaranteed
        guaranteed = False
        risk_flags.insert(0, reason)

    if negative_risk:
        risk_flags.append(
            "negative-risk event: outcomes are a venue-enforced exhaustive partition"
        )

    # Negative risk asserts the partition is exhaustive and always resolves. It does
    # NOT assert exclusivity, so that check runs regardless - it is the one property
    # that genuinely binds the short side, and skipping it was the only route to a
    # false guarantee. (Polymarket happens to derive both flags from one boolean, but
    # relying on that coupling would make this gate depend on a provider detail.)
    if shorting and not mutually_exclusive:
        withdraw(
            "the venue does not assert mutual exclusivity for these outcomes; if two "
            "of them can both resolve YES, two legs pay nothing and the basket "
            "collects less than it is priced for. NOT a guaranteed profit"
        )
    if not shorting and not (exhaustive or negative_risk):
        withdraw(
            "the venue does not assert exhaustiveness for these outcomes; if an "
            "unlisted outcome can occur, every leg resolves NO and the position "
            "loses its entire cost. NOT a guaranteed profit"
        )
    if shorting and not exhaustive:
        risk_flags.append(
            "the outcome set is not asserted exhaustive, which for a short basket is "
            "in your favour: an unlisted winner pays every leg"
        )

    # Event-level void is the short basket's other exposure, and it runs the opposite
    # way from exhaustiveness. Exclusivity and completeness describe the outcome
    # SPACE; neither promises the event resolves at all. If it voids and every leg
    # settles at $0.50 rather than $0/$1, the basket collects N/2 against a cost near
    # N-1 - a loss of (N-2)/2 per set. The long basket is unharmed, because N/2
    # exceeds the $1 it paid.
    #
    # Only a venue-enforced partition rules this out, and it does so through the
    # event's own rules rather than the flags: negative-risk events carry a fallback
    # that always declares a winner ("resolve to the No change bracket", "the nominee
    # first in alphabetical order").
    if shorting:
        void_exposure = (Decimal(len(legs)) - Decimal(2)) / Decimal(2)
        basis = (
            f"an event-level void settling every leg at $0.50 would "
            f"{'lose $' + str(void_exposure) + ' per set' if void_exposure > 0 else 'break even'}"
        )
        if negative_risk:
            risk_flags.append(
                f"{basis}; the venue's negative-risk construction always declares a "
                f"winner, which is what rules that out"
            )
        elif void_exposure > 0:
            withdraw(
                f"the venue does not enforce this partition, so {basis}. Exclusivity "
                f"and completeness describe the outcome set, not whether the event "
                f"resolves at all. NOT a guaranteed profit"
            )
        else:
            risk_flags.append(
                f"{basis}, so a void costs nothing here; the settlement basis is not "
                f"asserted complementary, and a venue that resolves to 'a fair price' "
                f"rather than 50-50 need not pay the two legs $1.00 between them"
            )

    # ---- fill quality ----------------------------------------------------
    ages = [age_seconds(leg.book.observed_at) for leg in legs]
    worst_age = max([a for a in ages if a is not None], default=None)
    if worst_age is not None and worst_age > settings.max_quote_age_seconds:
        label = ArbitrageLabel.STALE_QUOTE
        risk_flags.append(f"oldest leg quote is {humanize_seconds(worst_age)} old")
    # Denominated per set of payout, not per basket. A short set costs about $(N-1)
    # where a long set costs about $1, so a dollar floor on the whole basket binds
    # roughly (N-1) times more loosely on the short side - it would stop measuring
    # "is there enough book to be worth executing" and start measuring "how many legs
    # does this event have".
    notional_per_payout = safe_div(total_cost, payout_per_set)
    if notional_per_payout < settings.min_orderbook_notional_usd:
        label = ArbitrageLabel.INSUFFICIENT_LIQUIDITY
        risk_flags.append(
            f"only ${notional_per_payout:.2f} of matched depth per $1 of payout "
            f"across all legs"
        )
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

    # Applied last so it outranks every fill-quality label. A broken payout premise is
    # not an execution problem a reader can solve by refreshing the quote.
    if not guaranteed:
        label = ArbitrageLabel.NOT_GUARANTEED

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


def _price_at(
    legs: list[OutcomeLeg], side: Side, sets: Decimal, settings  # noqa: ANN001
) -> tuple[Decimal, Decimal, Decimal, Decimal, list[ArbLeg]] | None:
    """Cost stack and leg detail for buying ``sets`` of every leg, or None."""
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
    return gross_cost, total_fees, slippage, transfer, arb_legs


def _max_matched_sets(
    legs: list[OutcomeLeg],
    side: Side,
    payout_per_set: Decimal,
    settings,  # noqa: ANN001
    *,
    max_sets: Decimal,
) -> Decimal:
    """The size maximising NET profit across the basket, not gross.

    Sizing on gross and then charging costs at whatever size that picked discards
    real arbitrage. Per-set fees and slippage scale with leg count, so on exactly the
    multi-leg baskets this scanner targets the gross-maximal size is routinely
    net-negative - a 3-leg ladder with a cheap prefix returned nothing at its gross
    size while 100 sets of the same book were worth $45.50. Where the basket did
    survive, the reported size was still not the profitable one.

    Net profit is piecewise-linear in size between ladder boundaries, because each
    leg's VWAP only changes slope where a level is exhausted. Evaluating the
    boundaries is therefore exact rather than a sample.

    Sizes are floored to whole contracts: both venues trade integer contracts, and a
    headline profit quoted for 637.5 sets is quoted for a position neither will
    accept.
    """
    boundaries: set[Decimal] = set()
    ceiling = max_sets
    for leg in legs:
        cumulative = ZERO
        for level in leg.book.asks(side):
            cumulative += level.size
            if cumulative <= max_sets:
                boundaries.add(cumulative)
        if cumulative <= 0:
            return ZERO
        # No basket can exceed the thinnest leg's total depth.
        ceiling = min(ceiling, cumulative)
    if ceiling <= 0:
        return ZERO
    boundaries.add(ceiling)

    minimum = max((leg.market.min_order_size for leg in legs), default=ONE)
    candidates = sorted(
        {
            size
            for size in (b.to_integral_value(rounding=ROUND_FLOOR) for b in boundaries)
            if size >= minimum and size <= ceiling
        },
        reverse=True,
    )
    if not candidates:
        return ZERO

    best_size = ZERO
    best_profit = ZERO
    for size in candidates:
        priced = _price_at(legs, side, size, settings)
        if priced is None:
            continue
        gross_cost, fees, slippage, transfer, _ = priced
        profit = size * payout_per_set - (gross_cost + fees + slippage + transfer)
        if profit > best_profit:
            best_profit, best_size = profit, size
    return best_size
