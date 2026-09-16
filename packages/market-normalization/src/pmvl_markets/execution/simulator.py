"""Simulated venues that fill from real depth, and the faults worth rehearsing.

A simulator that fills every order completely at the quoted top price proves nothing,
because the failures this system exists to survive all live in the gap between "the
quote said X" and "the venue did Y". So :class:`BookBackedVenueClient` walks an actual
order book ladder, consumes the depth it takes, and stops when the ladder runs out or
the limit price is exceeded. Two candidates competing for the same liquidity therefore
interfere with each other here exactly as they would in production.

On top of that, :class:`Fault` injects the specific ways a two-leg trade goes wrong.
Each one is a real incident class, not a random error:

``SUBMIT_REJECT``
    The second leg bounces while the first is already live. The classic one-legged
    position.
``SUBMIT_TIMEOUT``
    The request may have landed. The executor must query, not resend - and the
    scripted fill that follows proves the query finds a *real* position.
``CONFIRM_TIMEOUT``
    The order is gone into the dark. Escalation, never a guess.
``PARTIAL_FILL``
    Both legs fill, unequally. The pair is smaller than intended and lopsided by the
    difference.
``PRICE_JUMP``
    The book moves between acknowledgement and fill, so the average price is worse
    than the one the edge was computed on.
``CANCEL_FILL_RACE``
    A cancellation and a fill cross. The venue's record wins; the local guess loses.

None of these clients can reach a network. ``is_simulation`` is declared here and
checked by :class:`~pmvl_markets.execution.twoleg.TwoLegExecutor`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import ZERO, safe_div
from pmvl_shared.schemas import BookLevel, OrderBook

from .models import OrderStatus
from .twoleg import LegReport, LegTimeout, SubmitAck
from .venues import VenueOrderSpec

__all__ = [
    "Fault",
    "SimulatedLadder",
    "ladder_from_book",
    "BookBackedVenueClient",
]


class Fault(StrEnum):
    """One scripted failure, consumed the next time the named call happens."""

    SUBMIT_REJECT = "submit_reject"
    SUBMIT_TIMEOUT = "submit_timeout"
    CONFIRM_TIMEOUT = "confirm_timeout"
    #: Fill only this fraction of what the ladder would otherwise give.
    PARTIAL_FILL = "partial_fill"
    #: Worsen every ask by one tick before filling.
    PRICE_JUMP = "price_jump"
    #: The order is reported cancelled, then the venue reveals it filled after all.
    CANCEL_FILL_RACE = "cancel_fill_race"
    CLOSE_TIMEOUT = "close_timeout"
    #: Only part of the unwind gets done, so exposure survives the remedy.
    CLOSE_PARTIAL = "close_partial"


@dataclass
class SimulatedLadder:
    """The depth one contract side offers, and what it will pay to take it back.

    ``asks`` is what a buyer consumes; ``bids`` is what a seller hits when unwinding.
    Both are mutated as they are consumed, so liquidity is finite in the simulation
    the same way it is finite on the venue.
    """

    asks: list[BookLevel] = field(default_factory=list)
    bids: list[BookLevel] = field(default_factory=list)

    def take_asks(
        self, quantity: Decimal, limit_price: Decimal
    ) -> tuple[Decimal, Decimal]:
        """Buy up to ``quantity`` at or below ``limit_price``. Returns (filled, cost)."""
        filled = ZERO
        cost = ZERO
        while self.asks and filled < quantity:
            level = self.asks[0]
            if level.price > limit_price:
                break
            take = min(level.size, quantity - filled)
            filled += take
            cost += take * level.price
            remaining = level.size - take
            if remaining <= ZERO:
                self.asks.pop(0)
            else:
                self.asks[0] = BookLevel(price=level.price, size=remaining)
        return filled, cost

    def hit_bids(self, quantity: Decimal) -> tuple[Decimal, Decimal]:
        """Sell up to ``quantity`` into the bid ladder. Returns (sold, proceeds).

        No limit price: an unwind takes what the book pays. That is the point of an
        unwind, and pretending otherwise would understate what a failed pair costs.
        """
        sold = ZERO
        proceeds = ZERO
        while self.bids and sold < quantity:
            level = self.bids[0]
            take = min(level.size, quantity - sold)
            sold += take
            proceeds += take * level.price
            remaining = level.size - take
            if remaining <= ZERO:
                self.bids.pop(0)
            else:
                self.bids[0] = BookLevel(price=level.price, size=remaining)
        return sold, proceeds


def ladder_from_book(book: OrderBook, side: Side) -> SimulatedLadder:
    """Extract one side's tradeable depth from a normalized book.

    Uses ``asks(side)`` for buying and ``bids(side)`` for selling, which on Kalshi are
    already the derived-from-the-opposite-side ladders the provider built. Nothing
    here re-derives a price, so a derivation bug upstream shows up as bad fills rather
    than being papered over by a second, different derivation.
    """
    return SimulatedLadder(
        asks=[BookLevel(price=lvl.price, size=lvl.size) for lvl in book.asks(side)],
        bids=[BookLevel(price=lvl.price, size=lvl.size) for lvl in book.bids(side)],
    )


@dataclass
class _OrderState:
    spec: VenueOrderSpec
    filled_quantity: Decimal = ZERO
    cost: Decimal = ZERO
    fees: Decimal | None = None
    status: OrderStatus = OrderStatus.SUBMITTING
    venue_order_id: str = ""
    confirms: int = 0


class BookBackedVenueClient:
    """A venue simulated from real depth, with optional scripted faults.

    ``is_simulation`` is a class attribute rather than a constructor argument so that
    it cannot be switched off by configuration. A live adapter that wanted to run here
    would have to set it in source.
    """

    is_simulation = True

    def __init__(
        self,
        venue: Platform,
        ladders: dict[str, SimulatedLadder],
        *,
        faults: dict[str, list[Fault]] | None = None,
        partial_fraction: Decimal = Decimal("0.5"),
        charge_fees: bool = True,
    ) -> None:
        self.venue = venue
        self._ladders = ladders
        self._faults = {k: list(v) for k, v in (faults or {}).items()}
        self._orders: dict[str, _OrderState] = {}
        self._partial_fraction = partial_fraction
        self._charge_fees = charge_fees
        self._next_id = 0

    # ------------------------------------------------------------------ faults
    def _take_fault(self, contract_key: str, *candidates: Fault) -> Fault | None:
        """Pop the next scripted fault for this contract, if it is one we handle here."""
        queue = self._faults.get(contract_key)
        if not queue:
            return None
        if queue[0] not in candidates:
            return None
        return queue.pop(0)

    # ------------------------------------------------------------------ submit
    def submit(
        self, spec: VenueOrderSpec, *, client_order_id: str, attempt_id: str
    ) -> SubmitAck:
        fault = self._take_fault(
            spec.contract.contract_key, Fault.SUBMIT_REJECT, Fault.SUBMIT_TIMEOUT
        )
        if fault is Fault.SUBMIT_REJECT:
            return SubmitAck(accepted=False, reject_reason="scripted venue rejection")

        # The order is recorded *before* the timeout is raised, because that is what
        # makes a timeout dangerous: the venue has it, the caller does not know.
        self._next_id += 1
        state = self._orders.setdefault(
            client_order_id,
            _OrderState(spec=spec, venue_order_id=f"{self.venue.value}-{self._next_id}"),
        )

        if fault is Fault.SUBMIT_TIMEOUT:
            raise LegTimeout(f"{self.venue.value} did not answer the submit in time")
        return SubmitAck(accepted=True, venue_order_id=state.venue_order_id)

    # ----------------------------------------------------------------- confirm
    def confirm(self, *, client_order_id: str) -> LegReport:
        state = self._orders.get(client_order_id)
        if state is None:
            # No such order. Not a fill, and not an error: the submission genuinely
            # never landed.
            return LegReport(status=OrderStatus.REJECTED, raw_status="unknown_order")

        key = state.spec.contract.contract_key
        fault = self._take_fault(
            key, Fault.CONFIRM_TIMEOUT, Fault.PARTIAL_FILL, Fault.PRICE_JUMP,
            Fault.CANCEL_FILL_RACE,
        )
        if fault is Fault.CONFIRM_TIMEOUT:
            raise LegTimeout(f"{self.venue.value} did not answer the confirm in time")

        state.confirms += 1
        if fault is Fault.CANCEL_FILL_RACE and state.confirms == 1:
            # First read says cancelled. The position exists anyway; the next read
            # will say so. Anything that treats read #1 as final is now wrong.
            return LegReport(
                status=OrderStatus.CANCELLED,
                filled_quantity=ZERO,
                venue_order_id=state.venue_order_id,
                raw_status="cancelled_but_racing",
            )

        if state.filled_quantity == ZERO:
            self._fill(state, fault)

        status = (
            OrderStatus.FILLED
            if state.filled_quantity >= state.spec.quantity
            else OrderStatus.PARTIALLY_FILLED
            if state.filled_quantity > ZERO
            else OrderStatus.CANCELLED
        )
        state.status = status
        return LegReport(
            status=status,
            filled_quantity=state.filled_quantity,
            average_price=(
                safe_div(state.cost, state.filled_quantity)
                if state.filled_quantity > ZERO
                else None
            ),
            fees_usd=state.fees,
            venue_order_id=state.venue_order_id,
            raw_status=status.value,
        )

    def _fill(self, state: _OrderState, fault: Fault | None) -> None:
        ladder = self._ladders.get(state.spec.contract.contract_key)
        if ladder is None:
            return
        if fault is Fault.PRICE_JUMP:
            tick = state.spec.contract.tick_size
            ladder.asks = [
                BookLevel(price=lvl.price + tick, size=lvl.size) for lvl in ladder.asks
            ]
        wanted = state.spec.quantity
        if fault is Fault.PARTIAL_FILL:
            wanted = state.spec.contract.round_quantity_down(
                wanted * self._partial_fraction
            )
        filled, cost = ladder.take_asks(wanted, state.spec.limit_price)
        state.filled_quantity = filled
        state.cost = cost
        if filled > ZERO and self._charge_fees:
            state.fees = state.spec.contract.fee_for(
                filled, safe_div(cost, filled)
            )

    # --------------------------------------------------------------- close out
    def close_out(
        self, spec: VenueOrderSpec, quantity: Decimal, *, client_order_id: str
    ) -> LegReport:
        fault = self._take_fault(
            spec.contract.contract_key, Fault.CLOSE_TIMEOUT, Fault.CLOSE_PARTIAL
        )
        if fault is Fault.CLOSE_TIMEOUT:
            raise LegTimeout(f"{self.venue.value} did not answer the close in time")

        ladder = self._ladders.get(spec.contract.contract_key)
        if ladder is None:
            return LegReport(status=OrderStatus.REJECTED, raw_status="no_book")

        wanted = quantity
        if fault is Fault.CLOSE_PARTIAL:
            wanted = spec.contract.round_quantity_down(
                quantity * self._partial_fraction
            )
        sold, proceeds = ladder.hit_bids(wanted)
        fees = ZERO
        if sold > ZERO and self._charge_fees:
            fees = spec.contract.fee_for(sold, safe_div(proceeds, sold))
        return LegReport(
            status=OrderStatus.FILLED if sold >= quantity else OrderStatus.PARTIALLY_FILLED,
            filled_quantity=sold,
            average_price=safe_div(proceeds, sold) if sold > ZERO else None,
            fees_usd=fees,
            raw_status="closed",
        )
