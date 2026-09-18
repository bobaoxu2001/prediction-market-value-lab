"""What a multi-leg floor is worth when the legs cannot fill together.

Kalshi fills one order at a time. A two-leg structure is therefore two decisions
separated by however long the first takes to confirm, and in that gap the second leg's
price can move or vanish. A payoff floor computed as though both fills land at once is
an upper bound on a trade nobody can place that way.

Three numbers, deliberately separate:

``atomic_net_floor``
    Both legs at their quoted prices, simultaneously. The theoretical ceiling. Useful
    for ranking candidates and for nothing else.
``sequential_net_floor``
    Leg one fills; leg two must then be completed against the book as it stands *after*
    that. Modelled pessimistically: the remaining size is taken from the next levels
    up, so the cost of the second leg is what it would be if the top of it were gone.
``failure_rescue_floor``
    Leg one fills and leg two is unavailable at any acceptable price. The position is
    unwound by selling leg one into its own bid. This is a loss - the spread plus both
    fees - and it is the number that decides whether a thin edge is worth attempting,
    because it is what happens when the attempt fails.

A structure whose atomic floor is positive and whose sequential floor is not is
``THEORETICAL_ONLY``. It may not be promoted to ``EXECUTABLE_ARBITRAGE_OBSERVED`` on the
strength of the atomic number, because the atomic number describes a fill that the venue
does not offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import ZERO, quantize_usd
from pmvl_shared.schemas import BookLevel, OrderBook

from ..pricing.fees import taker_fee
from ..pricing.orderbook import executable_quote

__all__ = [
    "ExecutionModel",
    "LegPlan",
    "SequentialOutcome",
    "evaluate_sequential",
]


class ExecutionModel(StrEnum):
    #: Both legs at quoted prices at the same instant. Upper bound only.
    ATOMIC_THEORETICAL = "ATOMIC_THEORETICAL"
    #: Leg one fills, then leg two is taken against a book that has moved against you.
    TAKER_SEQUENTIAL_WORST_CASE = "TAKER_SEQUENTIAL_WORST_CASE"
    #: Reserved for measured sequential outcomes once live fill data exists.
    TAKER_SEQUENTIAL_OBSERVED = "TAKER_SEQUENTIAL_OBSERVED"
    #: Reserved for the maker experiment. Never assumed to fill.
    MAKER_EXPERIMENTAL = "MAKER_EXPERIMENTAL"


@dataclass(frozen=True)
class LegPlan:
    """One leg as it would be sent: which side of which book, at what size."""

    contract_key: str
    venue: Platform
    book: OrderBook
    side: Side
    quantity: Decimal
    fee_rate: Decimal | None
    fee_type: str = ""

    def cost(self, *, consumed_size: Decimal = ZERO) -> tuple[Decimal, Decimal] | None:
        """(notional, fee) for this leg after ``consumed_size`` is lifted off the top.

        The adverse move is measured in *size*, not in price levels. Dropping whole
        levels punishes depth: a single resting 5,000 lots would count as entirely
        gone while a thin two-level book survived, which is exactly backwards - depth
        is what makes sequencing survivable. Consuming a quantity leaves 4,900 of that
        5,000 still there, which is what would actually happen.
        """
        levels = list(self.book.asks(self.side))
        remaining_to_skip = consumed_size
        while remaining_to_skip > ZERO and levels:
            top = levels[0]
            if top.size > remaining_to_skip:
                levels[0] = BookLevel(price=top.price, size=top.size - remaining_to_skip)
                remaining_to_skip = ZERO
            else:
                remaining_to_skip -= top.size
                levels.pop(0)
        if not levels:
            return None
        depth = sum((lvl.size for lvl in levels), ZERO)
        if depth < self.quantity:
            return None
        trimmed = OrderBook(
            platform=self.book.platform,
            platform_market_id=self.book.platform_market_id,
            observed_at=self.book.observed_at,
            yes_asks=levels if self.side is Side.YES else [],
            no_asks=levels if self.side is Side.NO else [],
            provenance=self.book.provenance,
        )
        quote = executable_quote(trimmed, self.side, self.quantity)
        if quote is None or quote.filled_size < self.quantity:
            return None
        if self.fee_rate is None:
            return None
        fee = taker_fee(
            self.venue, self.quantity, quote.average_price,
            rate=self.fee_rate, fee_type=self.fee_type,
        )
        return quote.notional, fee

    def unwind_proceeds(self) -> tuple[Decimal, Decimal] | None:
        """(proceeds, fee) from selling this leg back into its own bid ladder."""
        bids: list[BookLevel] = list(self.book.bids(self.side))
        if not bids or self.fee_rate is None:
            return None
        remaining = self.quantity
        proceeds = ZERO
        for level in bids:
            if remaining <= ZERO:
                break
            take = min(level.size, remaining)
            proceeds += take * level.price
            remaining -= take
        if remaining > ZERO:
            return None
        avg = proceeds / self.quantity
        return proceeds, taker_fee(
            self.venue, self.quantity, avg, rate=self.fee_rate, fee_type=self.fee_type
        )


@dataclass
class SequentialOutcome:
    atomic_net_floor: Decimal | None
    sequential_net_floor: Decimal | None
    failure_rescue_floor: Decimal | None
    model: ExecutionModel
    notes: list[str]

    @property
    def theoretical_only(self) -> bool:
        """Positive on paper, not positive once the fills are ordered."""
        return (
            self.atomic_net_floor is not None
            and self.atomic_net_floor > ZERO
            and (self.sequential_net_floor is None or self.sequential_net_floor <= ZERO)
        )

    def as_dict(self) -> dict[str, object]:
        def q(v):
            return str(quantize_usd(v)) if v is not None else None

        return {
            "model": self.model.value,
            "atomic_net_floor_usd": q(self.atomic_net_floor),
            "sequential_net_floor_usd": q(self.sequential_net_floor),
            "failure_rescue_floor_usd": q(self.failure_rescue_floor),
            "theoretical_only": self.theoretical_only,
            "notes": list(self.notes),
        }


def evaluate_sequential(
    legs: list[LegPlan],
    payoff_floor: Decimal,
    *,
    adverse_size: Decimal | None = None,
) -> SequentialOutcome:
    """Price the structure atomically, then again with the fills ordered against you.

    ``adverse_size`` is how much of the remaining legs' depth is assumed lifted before
    you get there. It defaults to the structure's own size, which is the natural
    assumption rather than an arbitrary one: if the dislocation was visible to you it
    was visible to others, and the size that makes it worth taking is the size that
    gets taken.

    The worst ordering is searched rather than assumed - which leg goes first changes
    the answer, and the honest number is the worst one, not the convenient one.
    """
    notes: list[str] = []
    haircut = adverse_size if adverse_size is not None else (
        max((leg.quantity for leg in legs), default=ZERO)
    )

    atomic_cost = ZERO
    for leg in legs:
        priced = leg.cost()
        if priced is None:
            return SequentialOutcome(
                None, None, None, ExecutionModel.TAKER_SEQUENTIAL_WORST_CASE,
                [f"{leg.contract_key}: cannot be priced at {leg.quantity} on the "
                 f"{leg.side.value} side"],
            )
        atomic_cost += priced[0] + priced[1]
    atomic = payoff_floor - atomic_cost

    worst_sequential: Decimal | None = None
    worst_rescue: Decimal | None = None

    for first_index in range(len(legs)):
        first = legs[first_index]
        rest = [leg for i, leg in enumerate(legs) if i != first_index]

        first_priced = first.cost()
        if first_priced is None:
            continue
        cost = first_priced[0] + first_priced[1]

        # The remaining legs are taken after the top of their books has been lifted.
        completable = True
        for leg in rest:
            priced = leg.cost(consumed_size=haircut)
            if priced is None:
                completable = False
                break
            cost += priced[0] + priced[1]

        if completable:
            candidate = payoff_floor - cost
            if worst_sequential is None or candidate < worst_sequential:
                worst_sequential = candidate
        else:
            notes.append(
                f"if {first.contract_key} fills first, the remaining leg cannot be "
                f"completed once {haircut} of its depth is gone"
            )

        # And the rescue: leg one is held, the rest are unavailable, unwind leg one.
        unwound = first.unwind_proceeds()
        if unwound is None:
            notes.append(f"{first.contract_key} cannot be unwound: no bid depth")
            rescue = None
        else:
            proceeds, unwind_fee = unwound
            rescue = proceeds - (first_priced[0] + first_priced[1]) - unwind_fee
        if rescue is not None and (worst_rescue is None or rescue < worst_rescue):
            worst_rescue = rescue

    if worst_sequential is None:
        notes.append(
            "no fill ordering completes the structure under the adverse assumption"
        )

    return SequentialOutcome(
        atomic, worst_sequential, worst_rescue,
        ExecutionModel.TAKER_SEQUENTIAL_WORST_CASE, notes,
    )
