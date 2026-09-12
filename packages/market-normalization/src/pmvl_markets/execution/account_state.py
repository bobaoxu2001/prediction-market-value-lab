"""What the real account holds, and how much of that we actually know.

Phase 5D's question is not "may the robot trade" but "does the robot understand
the account". Those need different machinery: the risk gate already decides the
first, and this module supplies the second.

Three properties matter more than the numbers themselves.

**Completeness is part of the answer.** An account read is four endpoints. If one
fails, the other three are not a partial picture of the account - they are a
complete picture of the wrong thing, because the missing endpoint is exactly
where the unseen exposure would be. A snapshot therefore carries whether it is
complete, and an incomplete one blocks rather than under-counting.

**Age is part of the answer.** Exposure measured ten minutes ago is a claim about
ten minutes ago. The risk gate carried ``fetched_at`` onto every authorization
and never once looked at it, which made the field decoration; it is now a cap
like any other.

**Ownership is not assumed.** A position this system did not create is still
exposure, and pretending otherwise is how a size limit gets exceeded by whatever
the operator was already holding. But it is also not evidence of a bug, and it is
never labelled ``BOT_MANAGED``: only a matching ``client_order_id`` in the local
ledger can establish that.

Nothing here can mutate an account. It holds a read-only client, and
``PRODUCTION_READ_ONLY`` cannot construct a write client at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Sequence

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .models import (
    AccountBalance,
    ExecutionMode,
    ExposureSnapshot,
    Fill,
    Order,
    Position,
)

log = get_logger(__name__)

__all__ = [
    "AccountStateCompleteness",
    "ObjectProvenance",
    "AccountSnapshot",
    "ProductionReadOnlyAccountState",
    "MAX_ACCOUNT_STATE_AGE",
]

#: How old an account snapshot may be and still support an authorization.
#: Deliberately short: this is a statement about live exposure, and a resting
#: order can fill in the gap.
MAX_ACCOUNT_STATE_AGE = timedelta(seconds=120)


class AccountStateCompleteness(StrEnum):
    """Whether every endpoint answered.

    There is no PARTIAL. A read that is missing its orders page cannot be
    "partially trusted" for a risk decision - the orders are where the exposure
    it is missing would be.
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class ObjectProvenance(StrEnum):
    """Who created a remote object."""

    #: A local ledger row carries this exact client_order_id. The only claim
    #: that can be made from evidence.
    BOT_MANAGED = "bot_managed"
    #: The venue holds it and this system has no record. Expected on a real
    #: account the operator used before the robot existed. Still exposure.
    PREEXISTING_EXTERNAL = "preexisting_external"
    #: Cannot be decided - e.g. the venue reports no client_order_id at all.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """One point-in-time read of a real account, with its own provenance."""

    mode: ExecutionMode
    observed_at: datetime
    completeness: AccountStateCompleteness
    balance: AccountBalance | None
    positions: tuple[Position, ...]
    open_orders: tuple[Order, ...]
    recent_fills: tuple[Fill, ...]
    #: Endpoint name -> error, for whatever did not answer.
    failures: dict[str, str] = field(default_factory=dict)
    #: (kind, identity) -> provenance, for orders and positions.
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.completeness is AccountStateCompleteness.COMPLETE

    def age(self, *, now: datetime | None = None) -> timedelta:
        return (now or utcnow()) - self.observed_at

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        return self.age(now=now) <= MAX_ACCOUNT_STATE_AGE

    def bot_managed_orders(self) -> tuple[Order, ...]:
        return tuple(
            o
            for o in self.open_orders
            if self.provenance.get(f"order:{o.exchange_order_id}")
            == ObjectProvenance.BOT_MANAGED.value
        )

    def external_orders(self) -> tuple[Order, ...]:
        return tuple(
            o
            for o in self.open_orders
            if self.provenance.get(f"order:{o.exchange_order_id}")
            != ObjectProvenance.BOT_MANAGED.value
        )

    def as_exposure(self) -> ExposureSnapshot:
        """The view the risk gate consumes.

        Every position and every resting order counts, whoever created it. A
        manually-placed order the operator forgot about is exactly the exposure a
        cap exists to bound, and filtering to bot-managed objects here would let
        the robot size on top of a position it can see and chose to ignore.
        """
        realized_loss: Decimal | None = Decimal("0")
        for pos in self.positions:
            if pos.realized_pnl_usd is None:
                realized_loss = None
                break
            if pos.realized_pnl_usd < 0:
                realized_loss = (realized_loss or Decimal("0")) - pos.realized_pnl_usd

        return ExposureSnapshot(
            balance=self.balance,
            positions=self.positions,
            resting_orders=tuple(o for o in self.open_orders if o.is_open),
            fetched_at=self.observed_at,
            realized_loss_today_usd=realized_loss,
            # Daily spend is a ledger fact, not an account fact - the venue has
            # no "what did this strategy spend today" endpoint. Left unmeasured
            # here on purpose; the harness fills it from the ledger, and if
            # nothing does, the gate blocks rather than reading it as zero.
            spend_today_usd=None,
            complete=self.is_complete,
            extra={
                "completeness": self.completeness.value,
                "failures": dict(self.failures),
                "external_order_count": len(self.external_orders()),
                "mode": self.mode.value,
            },
        )

    def as_dict(self) -> dict[str, Any]:
        """Operator-facing summary. Deliberately aggregate: no account ids."""
        return {
            "mode": self.mode.value,
            "observed_at": self.observed_at.isoformat(),
            "age_seconds": round(self.age().total_seconds(), 1),
            "fresh": self.is_fresh(),
            "completeness": self.completeness.value,
            "failures": dict(self.failures),
            "balance_usd": (
                str(self.balance.balance_usd) if self.balance else None
            ),
            "position_count": len(self.positions),
            "open_order_count": len(self.open_orders),
            "recent_fill_count": len(self.recent_fills),
            "bot_managed_orders": len(self.bot_managed_orders()),
            "external_orders": len(self.external_orders()),
        }


class ProductionReadOnlyAccountState:
    """Reads a real account. Holds nothing that could write to one.

    Feeds the *same* ``RiskGate`` that shadow and demo use - there is no
    production-specific risk policy, because a weaker one is how a production
    path ends up permitting what the sandbox refused.
    """

    def __init__(
        self,
        account_client: Any,
        *,
        ledger: Any = None,
        mode: ExecutionMode = ExecutionMode.PRODUCTION_READ_ONLY,
    ) -> None:
        # The guarantee is about the CLIENT, not the mode. Refusing writing
        # modes outright was wrong twice over: it blocked reading a demo account
        # (which the burn-in loop and `account-snapshot --mode demo` both need),
        # and it implied a safety property this class does not provide. What it
        # actually provides is that it holds an object with no mutating method,
        # so there is nothing here to write with.
        for forbidden in ("create_order", "cancel_order", "amend_order"):
            if hasattr(account_client, forbidden):
                raise ValueError(
                    f"{type(self).__name__} is a read-only surface; it was "
                    f"handed a client exposing {forbidden}()"
                )
        # The declared mode has to be the client's mode. It drives both the
        # ownership query and the label a snapshot is persisted under, so a
        # mismatch classifies a real account against demo ledger rows and files
        # the observation where nobody will look for it. KalshiAccountClient
        # already asserts the same thing about its transport.
        client_mode = getattr(account_client, "mode", None)
        if client_mode is not None and client_mode is not mode:
            raise ValueError(
                f"account client is {client_mode.value} but this reader was "
                f"declared {mode.value}; the mode scopes ownership and storage"
            )
        self.mode = mode
        self._account = account_client
        self._ledger = ledger

    async def snapshot(self, *, now: datetime | None = None) -> AccountSnapshot:
        """Read every endpoint. Record what failed rather than papering over it."""
        observed_at = now or utcnow()
        failures: dict[str, str] = {}

        async def attempt(name: str, coro):  # noqa: ANN001
            try:
                return await coro
            except Exception as exc:  # noqa: BLE001 - the failure IS the result
                failures[name] = f"{type(exc).__name__}: {str(exc)[:200]}"
                log.warning("account read %s failed: %s", name, type(exc).__name__)
                return None

        balance = await attempt("balance", self._account.get_balance())
        positions = await attempt("positions", self._account.get_positions())
        orders = await attempt("orders", self._account.get_orders())
        fills = await attempt("fills", self._account.get_fills())

        completeness = (
            AccountStateCompleteness.COMPLETE
            if not failures
            else AccountStateCompleteness.INCOMPLETE
        )

        open_orders = tuple(o for o in (orders or ()) if o.is_open)
        snapshot = AccountSnapshot(
            mode=self.mode,
            observed_at=observed_at,
            completeness=completeness,
            balance=balance,
            positions=tuple(positions or ()),
            open_orders=open_orders,
            recent_fills=tuple(fills or ()),
            failures=failures,
            provenance=self._classify(open_orders, tuple(positions or ())),
        )
        if not snapshot.is_complete:
            log.error(
                "account snapshot INCOMPLETE (%s); it must not be used to "
                "authorize anything",
                ",".join(sorted(failures)),
            )
        return snapshot

    # ---------------------------------------------------------------- ownership
    def _classify(
        self, orders: Sequence[Order], positions: Sequence[Position]
    ) -> dict[str, str]:
        """Ownership from evidence only.

        A remote object is ``BOT_MANAGED`` when, and only when, a local ledger
        row carries its ``client_order_id``. Everything else is external or
        unknown - never invented ownership, because "we placed this" is the claim
        that would justify reasoning about it as ours.
        """
        known: set[str] = set()
        if self._ledger is not None:
            try:
                with self._ledger.connect() as con:
                    known = {
                        row.client_order_id
                        for row in self._ledger.all_orders(con, mode=self.mode)
                        # Only rows that actually reached a venue. A BLOCKED or
                        # INTENDED row with no submitted_at is positive evidence
                        # this system did NOT place the order, and reading it as
                        # ownership moves a foreign order out of the bucket an
                        # operator uses to triage their real account.
                        if row.submitted_at is not None
                    }
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "could not read the local ledger for provenance (%s); "
                    "every remote object will be UNKNOWN rather than assumed ours",
                    type(exc).__name__,
                )
                known = set()

        out: dict[str, str] = {}
        for order in orders:
            key = f"order:{order.exchange_order_id}"
            coid = order.client_order_id
            if not coid:
                out[key] = ObjectProvenance.UNKNOWN.value
            elif coid in known:
                out[key] = ObjectProvenance.BOT_MANAGED.value
            else:
                out[key] = ObjectProvenance.PREEXISTING_EXTERNAL.value

        for position in positions:
            # The venue reports no creating order on a position, so ownership of
            # one cannot be established from evidence at all.
            out[f"position:{position.ticker}:{position.outcome.value}"] = (
                ObjectProvenance.UNKNOWN.value
            )
        return out
