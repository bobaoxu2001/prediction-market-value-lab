"""Normalized execution types. Raw venue JSON stops at the adapter boundary.

Two invariants carry most of the safety weight here.

**Orientation is never positional.** ``(ticker, "yes", "buy")`` and
``(ticker, "buy", "yes")`` are both three strings, and a transposition between
them is a silent inversion from "buy YES" to "buy NO" - the difference between a
position and its opposite. Every constructor in this module takes
:class:`Outcome` and :class:`OrderAction` as named enum fields, so a
transposition is a type error rather than a trade.

**Absent is not zero.** A venue that does not report a fee, an average fill
price, or a realized PnL gets ``None``, never ``Decimal("0")``. A fabricated zero
propagates into risk arithmetic as "this costs nothing", which is the direction
that loses money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from pmvl_shared.enums import Side

from .modes import ExecutionMode

__all__ = [
    "Outcome",
    "OrderAction",
    "OrderStatus",
    "TimeInForce",
    "AccountBalance",
    "Position",
    "Order",
    "Fill",
    "OrderIntent",
    "OrderResult",
    "CancelResult",
    "ExposureSnapshot",
    "TERMINAL_STATUSES",
    "OPEN_STATUSES",
    "VALID_TRANSITIONS",
    "is_valid_transition",
]


class Outcome(StrEnum):
    """Which side of the binary contract. Distinct from :class:`OrderAction`."""

    YES = "yes"
    NO = "no"

    @property
    def opposite(self) -> "Outcome":
        return Outcome.NO if self is Outcome.YES else Outcome.YES

    def to_side(self) -> Side:
        """Bridge to the research layer's existing ``Side`` enum."""
        return Side.YES if self is Outcome.YES else Side.NO

    @classmethod
    def from_side(cls, side: Side) -> "Outcome":
        return cls.YES if side is Side.YES else cls.NO


class OrderAction(StrEnum):
    """Direction of the order. Distinct from :class:`Outcome`."""

    BUY = "buy"
    SELL = "sell"


class TimeInForce(StrEnum):
    #: Rest on the book until filled or cancelled. The only value Phase 5A uses.
    GOOD_TILL_CANCELLED = "gtc"
    IMMEDIATE_OR_CANCEL = "ioc"
    FILL_OR_KILL = "fok"


class OrderStatus(StrEnum):
    """Lifecycle of one logical order.

    ``FILLED`` is never inferred from an order's absence from the open-orders
    list - see :mod:`pmvl_markets.execution.reconcile`. An order that vanished
    without fill evidence is ``UNKNOWN_RECONCILIATION_REQUIRED``, which blocks
    further mutation rather than guessing.
    """

    INTENDED = "intended"
    BLOCKED = "blocked"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN_RECONCILIATION_REQUIRED = "unknown_reconciliation_required"

    #: Shadow planning outcome. A shadow plan passed the real risk gate and was
    #: recorded, but no venue ever saw it - so it must never be described with a
    #: venue word. FILLED/PARTIALLY_FILLED/CANCELLED are unreachable from here,
    #: because no fill and no cancellation happened.
    SHADOW_AUTHORIZED = "shadow_authorized"


TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.BLOCKED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)

#: Statuses under which the venue may still hold working quantity.
OPEN_STATUSES = frozenset(
    {
        OrderStatus.SUBMITTING,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
    }
)

#: Allowed transitions. Anything absent here is a reconciliation defect, not a
#: state to be written. ``UNKNOWN_RECONCILIATION_REQUIRED`` is reachable from
#: every non-terminal state because ambiguity can arrive at any point.
VALID_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.INTENDED: frozenset(
        {
            OrderStatus.BLOCKED,
            OrderStatus.SUBMITTING,
            OrderStatus.SHADOW_AUTHORIZED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.BLOCKED: frozenset(),
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
        }
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCELLED,
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
        }
    ),
    #: Terminal for a plan that never left the process. Deliberately cannot
    #: reach FILLED, PARTIALLY_FILLED or CANCELLED: claiming a venue outcome for
    #: an order no venue received is the same fabrication the reconciler refuses.
    OrderStatus.SHADOW_AUTHORIZED: frozenset({OrderStatus.EXPIRED}),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
}


def is_valid_transition(old: OrderStatus, new: OrderStatus) -> bool:
    if old == new:
        # Idempotent re-application of the same state is always allowed; fill
        # reconciliation replays the same status routinely.
        return True
    return new in VALID_TRANSITIONS.get(old, frozenset())


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """Cash the venue reports. ``None`` fields were not reported, not zero."""

    balance_usd: Decimal
    available_usd: Decimal | None = None
    payout_usd: Decimal | None = None
    fetched_at: datetime | None = None
    environment: str = ""


@dataclass(frozen=True, slots=True)
class Position:
    ticker: str
    outcome: Outcome
    quantity: int
    #: Average entry price in cents, when the venue reports it.
    average_price_cents: int | None = None
    realized_pnl_usd: Decimal | None = None
    exposure_usd: Decimal | None = None
    fetched_at: datetime | None = None

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0


@dataclass(frozen=True, slots=True)
class Order:
    """A resting or historical order as the venue describes it."""

    exchange_order_id: str
    ticker: str
    outcome: Outcome
    action: OrderAction
    status: OrderStatus
    limit_price_cents: int | None = None
    original_quantity: int = 0
    filled_quantity: int = 0
    remaining_quantity: int = 0
    client_order_id: str | None = None
    created_at: datetime | None = None
    time_in_force: TimeInForce = TimeInForce.GOOD_TILL_CANCELLED
    raw_status: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution against an order.

    ``fee_usd`` is ``None`` when the venue did not report a fee on this fill.
    Callers must treat that as unknown cost, never as free.
    """

    fill_id: str
    ticker: str
    outcome: Outcome
    action: OrderAction
    quantity: int
    price_cents: int
    exchange_order_id: str | None = None
    fee_usd: Decimal | None = None
    filled_at: datetime | None = None
    is_taker: bool | None = None

    @property
    def notional_usd(self) -> Decimal:
        return (Decimal(self.price_cents) * Decimal(self.quantity)) / Decimal(100)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What the strategy layer wants, before anything is permitted to happen.

    Provenance fields are optional because the current research layer does not
    expose all of them yet. They exist so that a later audit can answer "why was
    this order placed, from which signal, against which market data, and how
    stale was each" without reconstructing it from logs.
    """

    intent_id: str
    ticker: str
    outcome: Outcome
    action: OrderAction
    limit_price_cents: int
    quantity: int
    mode: ExecutionMode

    time_in_force: TimeInForce = TimeInForce.GOOD_TILL_CANCELLED
    strategy: str = ""
    strategy_version: str = ""
    decision_epoch: str = ""
    source_signal_id: str = ""
    revision: int = 0

    model_probability: Decimal | None = None
    conservative_probability: Decimal | None = None
    expected_edge_after_fees: Decimal | None = None
    max_acceptable_price_cents: int | None = None

    decision_timestamp: datetime | None = None
    signal_timestamp: datetime | None = None
    market_data_timestamp: datetime | None = None
    expires_at: datetime | None = None
    reason: str = ""

    #: Depth ceiling from research, if one exists. Carried so an auditor can see
    #: that the quantity was bounded by it - never used *as* the quantity.
    capacity_ceiling: int | None = None
    #: The issued decision that chose ``quantity``. ``None`` only on intents
    #: built by hand in tests; the signal adapter always supplies one.
    sizing: Any = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"intent quantity must be positive, got {self.quantity}")
        if not 1 <= self.limit_price_cents <= 99:
            # Kalshi binary contracts trade in 1..99 cents. 0 and 100 are
            # settlement values, not order prices.
            raise ValueError(
                f"limit price must be 1..99 cents, got {self.limit_price_cents}"
            )
        if not self.ticker:
            raise ValueError("intent requires a ticker")

    @property
    def notional_usd(self) -> Decimal:
        """Cash at risk if this order fills completely.

        For a BUY this is the full premium paid. For a SELL of a contract already
        held it overstates the cash movement, which is the conservative direction
        and is what the risk gate wants.
        """
        return (
            Decimal(self.limit_price_cents) * Decimal(self.quantity)
        ) / Decimal(100)


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Outcome of attempting to place one intent."""

    intent_id: str
    client_order_id: str
    status: OrderStatus
    mode: ExecutionMode
    exchange_order_id: str | None = None
    filled_quantity: int = 0
    remaining_quantity: int = 0
    average_fill_price_cents: Decimal | None = None
    fees_usd: Decimal | None = None
    submitted_at: datetime | None = None
    error: str = ""
    #: True when nothing was sent to a venue - SHADOW, or a risk block.
    simulated: bool = False

    @property
    def accepted(self) -> bool:
        return self.status not in {
            OrderStatus.BLOCKED,
            OrderStatus.REJECTED,
        }


@dataclass(frozen=True, slots=True)
class CancelResult:
    client_order_id: str
    status: OrderStatus
    exchange_order_id: str | None = None
    #: True when the venue had nothing to cancel. Not an error: cancelling twice
    #: is a normal retry outcome and must be idempotent.
    already_resolved: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    """Everything the risk gate needs to know about current commitments.

    Built from the venue, never assumed. ``positions`` and ``resting_orders`` are
    both counted: an account with no position but three resting bids is not flat.
    """

    balance: AccountBalance | None = None
    positions: tuple[Position, ...] = ()
    resting_orders: tuple[Order, ...] = ()
    fetched_at: datetime | None = None
    #: Realized loss booked today, positive number meaning money lost.
    #: ``None`` means "not measured", which the risk gate treats as a block. A
    #: default of zero made the daily loss breaker permanently inert: unknown
    #: state rendered as "nothing lost yet" and the check always passed.
    realized_loss_today_usd: Decimal | None = None
    #: Cash spent placing orders today, for the daily-spend cap. ``None`` blocks,
    #: for the same reason.
    spend_today_usd: Decimal | None = None
    #: Whether every account endpoint answered. A read missing its orders page
    #: is not a partial picture of the account - it is a complete picture of the
    #: wrong thing, because the missing page is where unseen exposure would be.
    #: Defaults True so hand-built snapshots behave as before; the production
    #: reader always sets it from the actual read.
    complete: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def position_exposure_usd(self, ticker: str) -> Decimal:
        total = Decimal("0")
        for pos in self.positions:
            if pos.ticker != ticker:
                continue
            if pos.quantity == 0 and not pos.exposure_usd:
                # Genuinely nothing here. A zero quantity WITH a reported
                # exposure is a contradiction, not a flat position, and is
                # handled below rather than skipped.
                continue
            if pos.exposure_usd:
                # Truthiness, not `is not None`. A venue reporting
                # market_exposure 0 alongside 40 contracts was trusted over the
                # contract count, so partial information scored lower than none:
                # 40 contracts with no exposure field charged $40, the same 40
                # with a reported 0 charged nothing.
                total += abs(pos.exposure_usd)
            elif pos.average_price_cents is not None:
                total += (
                    Decimal(pos.average_price_cents) * Decimal(abs(pos.quantity))
                ) / Decimal(100)
            else:
                # Unknown entry price. Charge the maximum a binary contract can
                # be worth rather than skipping it, so unknown never reads as
                # cheap.
                total += Decimal(abs(pos.quantity))
        return total

    def resting_exposure_usd(self, ticker: str | None = None) -> Decimal:
        total = Decimal("0")
        for order in self.resting_orders:
            if ticker is not None and order.ticker != ticker:
                continue
            if not order.is_open:
                continue
            qty = order.remaining_quantity or order.original_quantity
            price = order.limit_price_cents
            if price is None:
                total += Decimal(max(qty, 0))
            else:
                total += (Decimal(price) * Decimal(max(qty, 0))) / Decimal(100)
        return total

    def total_exposure_usd(self) -> Decimal:
        tickers = {p.ticker for p in self.positions if p.quantity != 0}
        total = sum(
            (self.position_exposure_usd(t) for t in tickers), start=Decimal("0")
        )
        return total + self.resting_exposure_usd()

    def open_market_count(self) -> int:
        tickers = {p.ticker for p in self.positions if p.quantity != 0}
        tickers |= {o.ticker for o in self.resting_orders if o.is_open}
        return len(tickers)

    def open_order_count(self, ticker: str) -> int:
        return sum(1 for o in self.resting_orders if o.ticker == ticker and o.is_open)
