"""Same-venue exhaustive baskets, proven by tiling rather than by shared event id.

A Kalshi temperature event lists rungs like::

    <74 | [74,75] | [76,77] | [78,79] | [80,81] | >81

If exactly one of those settles YES and each pays $0 or $1, two structures follow:

* buying **every YES** costs whatever the rungs cost and pays **$1** - one rung wins;
* buying **every NO** pays **$(N-1)** - every rung but the winner pays.

Both floors are exact, and neither depends on which rung wins. The whole question is
whether the premise holds, and the premise is *not* "these share an event ticker".

**Exhaustiveness is a claim about a grid, not about the reals.** The rungs above leave
open gaps at (75, 76), (77, 78) and (79, 80). Over the real line they do not cover, so
the all-NO floor of $(N-1) would be wrong - a 75.4 degree reading settles every rung NO
and pays $N. Over the integers they tile exactly. So this module infers the observation
grid from the rung boundaries, verifies the tiling *on that grid*, and records the
inferred step as a stated assumption rather than burying it. A basket whose gaps are
wider than one grid step is refused, because then the gap really can swallow an outcome.

Mutual exclusivity is checked the same way: any overlap between two rungs - ``[74,75]``
against ``[75,76]``, which share 75 - means two rungs can settle YES together and the
all-YES floor of $1 is not a floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.enums import Side
from pmvl_shared.money import ONE, ZERO, quantize_usd
from pmvl_shared.schemas import NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds, iso

from ..pricing.fees import taker_fee
from ..pricing.orderbook import executable_quote
from ..settlement.payoff import (
    ContractRule,
    ExclusiveGroup,
    HeldLeg,
    LinkageEvidence,
    StructuralStatus,
    VoidPayout,
    evaluate_payoff,
)
from .implication import FieldCheck, FieldVerdict

__all__ = [
    "Interval",
    "Bucket",
    "BasketDirection",
    "PartitionProof",
    "BasketCertificate",
    "interval_for",
    "prove_partition",
    "scan_basket",
]


@dataclass(frozen=True)
class Interval:
    """A half-line or closed range. ``None`` bounds are infinite."""

    lo: Decimal | None
    lo_closed: bool
    hi: Decimal | None
    hi_closed: bool

    def describe(self) -> str:
        left = "(-inf" if self.lo is None else ("[" if self.lo_closed else "(") + str(self.lo)
        right = "inf)" if self.hi is None else str(self.hi) + ("]" if self.hi_closed else ")")
        return f"{left}, {right}"


def interval_for(market: NormalizedMarket) -> Interval | None:
    """The set of observations that settle this rung YES, or None if not a rung."""
    comparator = (market.strike_type or "").lower()
    floor, cap = market.floor_strike, market.cap_strike
    if comparator == "less" and cap is not None:
        return Interval(None, False, cap, False)
    if comparator == "less_or_equal" and cap is not None:
        return Interval(None, False, cap, True)
    if comparator == "greater" and floor is not None:
        return Interval(floor, False, None, False)
    if comparator == "greater_or_equal" and floor is not None:
        return Interval(floor, True, None, False)
    if comparator == "between" and floor is not None and cap is not None:
        return Interval(floor, True, cap, True)
    return None


@dataclass(frozen=True)
class Bucket:
    market: NormalizedMarket
    book: OrderBook
    interval: Interval

    @property
    def key(self) -> str:
        return self.market.platform_market_id


class BasketDirection(StrEnum):
    #: Buy YES on every rung. Exactly one wins, so the floor is $1 per set.
    ALL_YES = "all_yes"
    #: Buy NO on every rung. Every rung but the winner pays, so the floor is $(N-1).
    ALL_NO = "all_no"


@dataclass
class PartitionProof:
    proven: bool
    grid_step: Decimal | None = None
    checks: list[FieldCheck] = field(default_factory=list)
    reason: str = ""
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "proven": self.proven,
            "grid_step": str(self.grid_step) if self.grid_step is not None else None,
            "reason": self.reason,
            "assumptions": list(self.assumptions),
            "checks": [c.as_dict() for c in self.checks],
        }


def _infer_grid_step(bounds: list[Decimal]) -> Decimal | None:
    """Smallest positive distance between distinct rung boundaries.

    The rungs are the only evidence available about how finely the venue expects the
    observation to be reported. Boundaries one apart are consistent with an integer
    grid and inconsistent with anything finer, so the minimum spacing is the strongest
    step the data supports. It is inferred, not known, and travels as an assumption.
    """
    unique = sorted(set(bounds))
    if len(unique) < 2:
        return None
    return min(b - a for a, b in zip(unique, unique[1:]) if b > a)


def prove_partition(buckets: list[Bucket]) -> PartitionProof:
    """Prove the rungs are pairwise disjoint and cover the grid exactly once."""
    checks: list[FieldCheck] = []
    if len(buckets) < 2:
        return PartitionProof(False, reason="a basket needs at least two rungs")

    # Same venue, event, cutoff and source - the floor is meaningless across different
    # observations, exactly as in the implication route.
    for name, getter in (
        ("event", lambda m: m.platform_event_id),
        ("cutoff", lambda m: iso(m.close_time) if m.close_time else ""),
        ("settlement_source", lambda m: m.settlement_source),
    ):
        values = {str(getter(b.market) or "") for b in buckets}
        if "" in values:
            checks.append(FieldCheck(name, FieldVerdict.INSUFFICIENT, "absent on a rung"))
            return PartitionProof(
                False, checks=checks,
                reason=f"{name} is not recorded on every rung, so the rungs cannot be "
                       "shown to describe one observation",
            )
        if len(values) > 1:
            checks.append(FieldCheck(name, FieldVerdict.MISMATCH, f"{len(values)} distinct values"))
            return PartitionProof(
                False, checks=checks,
                reason=f"rungs disagree on {name}; they are not one event",
            )
        checks.append(FieldCheck(name, FieldVerdict.MATCH, values.pop()))

    intervals = sorted(
        ((b, b.interval) for b in buckets),
        key=lambda pair: (
            pair[1].lo is not None,
            pair[1].lo if pair[1].lo is not None else ZERO,
        ),
    )

    # Exactly one rung may be unbounded below and one unbounded above, or the basket
    # cannot be a partition of the line.
    lower_tails = [i for _b, i in intervals if i.lo is None]
    upper_tails = [i for _b, i in intervals if i.hi is None]
    if len(lower_tails) != 1 or len(upper_tails) != 1:
        checks.append(
            FieldCheck("tails", FieldVerdict.MISMATCH,
                       f"{len(lower_tails)} unbounded-below, {len(upper_tails)} unbounded-above")
        )
        return PartitionProof(
            False, checks=checks,
            reason="a partition of the line needs exactly one rung open at each end; "
                   "this basket is missing a tail or has two",
        )

    bounds = [i.lo for _b, i in intervals if i.lo is not None]
    bounds += [i.hi for _b, i in intervals if i.hi is not None]
    step = _infer_grid_step(bounds)
    if step is None:
        return PartitionProof(False, checks=checks, reason="too few distinct boundaries to infer a grid")

    ordered = sorted(
        intervals,
        key=lambda pair: (pair[1].lo is not None, pair[1].lo if pair[1].lo is not None else ZERO),
    )
    gaps: list[str] = []
    overlaps: list[str] = []
    for (b_left, left), (b_right, right) in zip(ordered, ordered[1:]):
        if left.hi is None:
            overlaps.append(f"{b_left.key} is unbounded above but is not the last rung")
            continue
        if right.lo is None:
            overlaps.append(f"{b_right.key} is unbounded below but is not the first rung")
            continue
        if right.lo < left.hi or (right.lo == left.hi and left.hi_closed and right.lo_closed):
            overlaps.append(
                f"{b_left.key} {left.describe()} overlaps {b_right.key} {right.describe()}"
            )
            continue
        touching = right.lo == left.hi and (left.hi_closed != right.lo_closed)
        if touching:
            continue
        # An open gap between two closed rungs. Both endpoints are grid points, so the
        # open interval between them holds no grid point exactly when it is no wider
        # than one step: (75, 76) on an integer grid is empty, (75, 78) is not.
        span = right.lo - left.hi
        if span > step:
            gaps.append(
                f"{left.describe()} to {right.describe()} leaves {span} uncovered, "
                f"wider than the inferred grid step {step}"
            )

    if overlaps:
        checks.append(FieldCheck("mutual_exclusivity", FieldVerdict.MISMATCH, "; ".join(overlaps[:3])))
        return PartitionProof(
            False, step, checks,
            reason="rungs overlap, so two can settle YES together: " + "; ".join(overlaps[:3]),
        )
    checks.append(FieldCheck("mutual_exclusivity", FieldVerdict.MATCH, "no two rungs intersect"))

    if gaps:
        checks.append(FieldCheck("exhaustiveness", FieldVerdict.MISMATCH, "; ".join(gaps[:3])))
        return PartitionProof(
            False, step, checks,
            reason="rungs do not cover every outcome: " + "; ".join(gaps[:3]),
        )
    checks.append(
        FieldCheck("exhaustiveness", FieldVerdict.MATCH,
                   f"rungs tile the line on a grid of {step}")
    )

    return PartitionProof(
        True, step, checks,
        reason=f"{len(buckets)} rungs are pairwise disjoint and cover every grid point",
        assumptions=[
            f"the observation is reported on a grid of {step}, inferred from the rung "
            "boundaries; a finer reported value would fall in a gap between rungs and "
            "settle every rung NO",
        ],
    )


@dataclass
class BasketCertificate:
    structure: str
    venue: str
    event_key: str
    direction: str
    proof: PartitionProof
    rung_count: int = 0
    legs: list[dict[str, object]] = field(default_factory=list)
    quantity: Decimal = ZERO
    payoff_floor_usd: Decimal = ZERO
    gross_cost_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    total_cost_usd: Decimal = ZERO
    net_floor_usd: Decimal = ZERO
    structural_status: str = StructuralStatus.UNKNOWN.value
    combined_top_of_book: Decimal | None = None
    payoff_per_set: Decimal = ZERO
    accepted: bool = False
    rejection_reason: str = ""
    worst_book_age_seconds: int | None = None
    payoff_report: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "structure": self.structure,
            "venue": self.venue,
            "event_key": self.event_key,
            "direction": self.direction,
            "proof": self.proof.as_dict(),
            "rung_count": self.rung_count,
            "legs": self.legs,
            "quantity": str(self.quantity),
            "payoff_per_set": str(self.payoff_per_set),
            "payoff_floor_usd": str(quantize_usd(self.payoff_floor_usd)),
            "gross_cost_usd": str(quantize_usd(self.gross_cost_usd)),
            "fees_usd": str(quantize_usd(self.fees_usd)),
            "total_cost_usd": str(quantize_usd(self.total_cost_usd)),
            "net_floor_usd": str(quantize_usd(self.net_floor_usd)),
            "structural_status": self.structural_status,
            "combined_top_of_book": (
                str(self.combined_top_of_book) if self.combined_top_of_book is not None else None
            ),
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "worst_book_age_seconds": self.worst_book_age_seconds,
            "payoff_report": self.payoff_report,
        }


def _accretive_basket_size(
    buckets: list[Bucket], side: Side, payoff_per_set: Decimal, ceiling: Decimal
) -> Decimal:
    """Take sets while the marginal cost of one more stays under its payoff.

    The same rule the implication route needed: average cost keeps buying after the
    marginal set stops paying. With N legs the marginal cost of a set is the sum of
    each leg's current ask level, and it rises every time any one leg's level is
    exhausted.
    """
    ladders = [list(b.book.asks(side)) for b in buckets]
    if any(not ladder for ladder in ladders):
        return ZERO

    idx = [0] * len(ladders)
    left = [ladder[0].size for ladder in ladders]
    sets = ZERO

    while sets < ceiling:
        if any(i >= len(ladder) for i, ladder in zip(idx, ladders)):
            break
        marginal = sum((ladders[k][idx[k]].price for k in range(len(ladders))), ZERO)
        if marginal >= payoff_per_set:
            break
        take = min(min(left), ceiling - sets)
        if take <= ZERO:
            break
        sets += take
        for k in range(len(ladders)):
            left[k] -= take
            if left[k] <= ZERO:
                idx[k] += 1
                if idx[k] < len(ladders[k]):
                    left[k] = ladders[k][idx[k]].size
    return sets


def scan_basket(
    buckets: list[Bucket],
    *,
    max_sets: Decimal = Decimal("10000"),
    min_net_floor_usd: Decimal = Decimal("0.01"),
    max_quote_age_seconds: int = 300,
) -> list[BasketCertificate]:
    """Price both directions of one basket. Both, because either can be the mispriced one."""
    proof = prove_partition(buckets)
    out: list[BasketCertificate] = []
    for direction in (BasketDirection.ALL_YES, BasketDirection.ALL_NO):
        out.append(
            _price_basket(
                buckets, proof, direction,
                max_sets=max_sets,
                min_net_floor_usd=min_net_floor_usd,
                max_quote_age_seconds=max_quote_age_seconds,
            )
        )
    return out


def _price_basket(
    buckets: list[Bucket],
    proof: PartitionProof,
    direction: BasketDirection,
    *,
    max_sets: Decimal,
    min_net_floor_usd: Decimal,
    max_quote_age_seconds: int,
) -> BasketCertificate:
    n = len(buckets)
    side = Side.YES if direction is BasketDirection.ALL_YES else Side.NO
    payoff_per_set = ONE if direction is BasketDirection.ALL_YES else Decimal(n - 1)
    event_key = buckets[0].market.platform_event_id or ""

    cert = BasketCertificate(
        structure=f"basket:{direction.value}",
        venue=buckets[0].market.platform.value,
        event_key=event_key,
        direction=direction.value,
        proof=proof,
        rung_count=n,
        payoff_per_set=payoff_per_set,
    )
    ages = [age_seconds(b.book.observed_at) for b in buckets]
    known = [a for a in ages if a is not None]
    cert.worst_book_age_seconds = int(max(known)) if known else None

    tops = [b.book.best_ask(side) for b in buckets]
    if all(t is not None for t in tops):
        cert.combined_top_of_book = sum(tops, ZERO)

    if not proof.proven:
        cert.rejection_reason = f"partition not proven: {proof.reason}"
        return cert

    sets = _accretive_basket_size(buckets, side, payoff_per_set, max_sets)
    if sets <= ZERO:
        cert.rejection_reason = (
            f"no size clears the ${payoff_per_set} floor"
            + (f"; cheapest combined ask is ${cert.combined_top_of_book}"
               if cert.combined_top_of_book is not None else "; a rung has no ask")
        )
        return cert

    legs: list[HeldLeg] = []
    gross = ZERO
    fees = ZERO
    for bucket in buckets:
        quote = executable_quote(bucket.book, side, sets)
        if quote is None or quote.filled_size < sets:
            cert.rejection_reason = f"{bucket.key} cannot fill {sets} on the {side.value} side"
            return cert
        fee = taker_fee(
            bucket.market.platform, sets, quote.average_price,
            rate=bucket.market.fee_rate, fee_type=bucket.market.fee_type,
        )
        gross += quote.notional
        fees += fee
        legs.append(HeldLeg(bucket.key, side, sets, quote.average_price, fee))
        cert.legs.append({
            "contract_key": bucket.key,
            "title": bucket.market.title[:110],
            "interval": bucket.interval.describe(),
            "side": side.value,
            "quantity": str(sets),
            "vwap": str(quote.average_price),
            "worst_price": str(quote.worst_price),
            "levels_consumed": quote.levels_consumed,
            "fee_usd": str(quantize_usd(fee)),
        })

    rules = {
        b.key: ContractRule(
            contract_key=b.key, venue=b.market.platform, event_key=event_key,
            can_void=False, void_payout=VoidPayout.UNKNOWN,
            rule_source=(b.market.settlement_rules_raw or "")[:300],
            rule_version=b.market.resolution_hash or "",
        )
        for b in buckets
    }
    group = ExclusiveGroup(
        contract_keys=tuple(b.key for b in buckets),
        evidence=LinkageEvidence.PROVEN,
        exhaustive=True,
        covers_void=True,
        note=proof.reason,
    )
    report = evaluate_payoff(legs, rules, [], groups=[group])

    cert.quantity = sets
    cert.gross_cost_usd = gross
    cert.fees_usd = fees
    cert.total_cost_usd = report.total_cost_usd
    cert.payoff_floor_usd = report.min_payout_usd or ZERO
    cert.net_floor_usd = report.worst_case_net_usd or ZERO
    cert.structural_status = report.structural_status.value
    cert.payoff_report = report.as_dict()

    reasons: list[str] = []
    if report.structural_status is not StructuralStatus.GUARANTEED:
        reasons.append(f"structural_status={report.structural_status.value}")
    if cert.net_floor_usd <= min_net_floor_usd:
        reasons.append(
            f"net floor ${quantize_usd(cert.net_floor_usd)} does not clear the "
            f"${quantize_usd(min_net_floor_usd)} tolerance"
        )
    if cert.worst_book_age_seconds is None:
        reasons.append("book age unknown")
    elif cert.worst_book_age_seconds > max_quote_age_seconds:
        reasons.append(
            f"oldest book is {cert.worst_book_age_seconds}s old, over the "
            f"{max_quote_age_seconds}s limit"
        )
    cert.accepted = not reasons
    cert.rejection_reason = "; ".join(reasons)
    return cert
