"""Kalshi account (read) and execution (write) clients.

Split on purpose. :class:`KalshiAccountClient` has no method that can mutate an
account, so ``PRODUCTION_READ_ONLY`` is expressed as "you get one of these and
not the other" rather than as a runtime flag inside a class that *could* write.
The write client refuses to even construct in a non-writing mode.

All venue JSON is normalized here and nowhere else. An unrecognised order status
becomes ``UNKNOWN_RECONCILIATION_REQUIRED`` rather than a guess, because a status
this code has never seen is exactly the case where guessing is most expensive.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .credentials import KalshiCredentials, load_credentials
from .models import (
    AccountBalance,
    ExposureSnapshot,
    Fill,
    Order,
    OrderAction,
    OrderIntent,
    OrderStatus,
    Outcome,
    Position,
    TimeInForce,
)
from .modes import ExecutionMode, LiveTradingNotAuthorized, ModeProfile
from .transport import ExecutionApiError, SignedTransport

log = get_logger(__name__)

__all__ = [
    "KalshiAccountClient",
    "KalshiExecutionClient",
    "normalize_balance",
    "normalize_position",
    "normalize_order",
    "normalize_fill",
    "KALSHI_STATUS_MAP",
]

#: Kalshi's documented order statuses. Anything absent maps to
#: ``UNKNOWN_RECONCILIATION_REQUIRED`` - never to a terminal state.
KALSHI_STATUS_MAP: Mapping[str, OrderStatus] = {
    "resting": OrderStatus.OPEN,
    "open": OrderStatus.OPEN,
    "pending": OrderStatus.SUBMITTING,
    "executed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


def _cents_to_usd(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)) / Decimal(100)
    except Exception:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Any:
    if not value:
        return None
    from datetime import datetime

    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def normalize_balance(payload: Mapping[str, Any], environment: str) -> AccountBalance:
    """Kalshi reports balances in cents."""
    balance = _cents_to_usd(payload.get("balance"))
    return AccountBalance(
        balance_usd=balance if balance is not None else Decimal("0"),
        available_usd=_cents_to_usd(payload.get("available_balance")),
        payout_usd=_cents_to_usd(payload.get("payout")),
        fetched_at=utcnow(),
        environment=environment,
    )


def normalize_position(payload: Mapping[str, Any]) -> Position:
    """One market position.

    Kalshi's ``position`` is signed: positive is long YES, negative is long NO.
    The sign is folded into :class:`Outcome` and the quantity is made absolute,
    so no downstream arithmetic has to remember the convention.
    """
    raw = _int_or_none(payload.get("position")) or 0
    outcome = Outcome.YES if raw >= 0 else Outcome.NO
    exposure = _cents_to_usd(payload.get("market_exposure"))
    return Position(
        ticker=str(payload.get("ticker", "")),
        outcome=outcome,
        quantity=abs(raw),
        average_price_cents=None,  # not reported on this endpoint; never invented
        realized_pnl_usd=_cents_to_usd(payload.get("realized_pnl")),
        exposure_usd=exposure,
        fetched_at=utcnow(),
    )


def normalize_order(payload: Mapping[str, Any]) -> Order:
    raw_status = str(payload.get("status", "")).lower()
    status = KALSHI_STATUS_MAP.get(
        raw_status, OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    )
    if raw_status and raw_status not in KALSHI_STATUS_MAP:
        log.warning("unrecognised kalshi order status %r; flagged for reconciliation",
                    raw_status)

    side = str(payload.get("side", "")).lower()
    outcome = Outcome.NO if side == "no" else Outcome.YES
    action = (
        OrderAction.SELL
        if str(payload.get("action", "")).lower() == "sell"
        else OrderAction.BUY
    )
    price = _int_or_none(
        payload.get("yes_price") if outcome is Outcome.YES else payload.get("no_price")
    )
    original = _int_or_none(payload.get("initial_count"))
    if original is None:
        original = _int_or_none(payload.get("count")) or 0
    remaining = _int_or_none(payload.get("remaining_count"))
    filled = _int_or_none(payload.get("taker_fill_count"))
    if filled is None and remaining is not None:
        filled = max(original - remaining, 0)
    return Order(
        exchange_order_id=str(payload.get("order_id", "")),
        ticker=str(payload.get("ticker", "")),
        outcome=outcome,
        action=action,
        status=status,
        limit_price_cents=price,
        original_quantity=original,
        filled_quantity=filled or 0,
        remaining_quantity=remaining if remaining is not None else original,
        client_order_id=payload.get("client_order_id") or None,
        created_at=_parse_dt(payload.get("created_time")),
        time_in_force=TimeInForce.GOOD_TILL_CANCELLED,
        raw_status=raw_status,
    )


def normalize_fill(payload: Mapping[str, Any]) -> Fill:
    side = str(payload.get("side", "")).lower()
    outcome = Outcome.NO if side == "no" else Outcome.YES
    action = (
        OrderAction.SELL
        if str(payload.get("action", "")).lower() == "sell"
        else OrderAction.BUY
    )
    price = _int_or_none(
        payload.get("yes_price") if outcome is Outcome.YES else payload.get("no_price")
    )
    return Fill(
        fill_id=str(payload.get("trade_id") or payload.get("fill_id") or ""),
        ticker=str(payload.get("ticker", "")),
        outcome=outcome,
        action=action,
        quantity=_int_or_none(payload.get("count")) or 0,
        price_cents=price if price is not None else 0,
        exchange_order_id=payload.get("order_id") or None,
        # Kalshi does not return a per-fill fee on this endpoint. None means
        # unknown; it must never be read as "free".
        fee_usd=_cents_to_usd(payload.get("fee")),
        filled_at=_parse_dt(payload.get("created_time")),
        is_taker=payload.get("is_taker"),
    )


class KalshiAccountClient:
    """Read-only view of a Kalshi account.

    Structurally incapable of mutation: there is no method here that issues a
    non-GET request, in any mode.
    """

    def __init__(
        self,
        mode: ExecutionMode,
        *,
        credentials: KalshiCredentials | None = None,
        transport: SignedTransport | None = None,
    ) -> None:
        if mode is ExecutionMode.SHADOW:
            raise LiveTradingNotAuthorized(
                "SHADOW mode does not authenticate; there is no account to read"
            )
        self.profile = ModeProfile.of(mode)
        self._transport = transport or SignedTransport(
            self.profile,
            credentials or load_credentials(mode),
        )

    @property
    def mode(self) -> ExecutionMode:
        return self.profile.mode

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def get_balance(self) -> AccountBalance:
        payload = await self._transport.get("/portfolio/balance")
        return normalize_balance(payload or {}, self.profile.environment.value)

    async def get_positions(self, *, limit: int = 200) -> list[Position]:
        payload = await self._transport.get(
            "/portfolio/positions", params={"limit": limit}
        )
        rows = (payload or {}).get("market_positions") or []
        return [normalize_position(r) for r in rows]

    async def get_orders(
        self, *, ticker: str | None = None, status: str | None = None, limit: int = 200
    ) -> list[Order]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        payload = await self._transport.get("/portfolio/orders", params=params)
        rows = (payload or {}).get("orders") or []
        return [normalize_order(r) for r in rows]

    async def get_fills(
        self, *, ticker: str | None = None, limit: int = 200
    ) -> list[Fill]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        payload = await self._transport.get("/portfolio/fills", params=params)
        rows = (payload or {}).get("fills") or []
        return [normalize_fill(r) for r in rows]

    async def get_exposure(self) -> ExposureSnapshot:
        """Everything the risk gate needs, fetched fresh.

        Resting orders are included because an account with no position but
        three working bids is not flat, and treating it as flat is how a size
        limit gets exceeded three times over.
        """
        balance = await self.get_balance()
        positions = await self.get_positions()
        orders = await self.get_orders(status="resting")
        # Realized loss is measured, not assumed. The venue already reports
        # realized P&L per position; only the losing side counts toward the
        # daily loss breaker, and an unreported P&L leaves the total unknown
        # rather than optimistically zero.
        realized_loss: Decimal | None = Decimal("0")
        for pos in positions:
            if pos.realized_pnl_usd is None:
                realized_loss = None
                break
            if pos.realized_pnl_usd < 0:
                realized_loss = (realized_loss or Decimal("0")) - pos.realized_pnl_usd

        return ExposureSnapshot(
            balance=balance,
            positions=tuple(positions),
            resting_orders=tuple(o for o in orders if o.is_open),
            fetched_at=utcnow(),
            realized_loss_today_usd=realized_loss,
        )


class KalshiExecutionClient:
    """Write path. Constructible only in a mode that may mutate an account.

    Every mutating method re-asserts the mode rather than trusting the
    constructor, because an object can outlive the reasoning that created it.
    """

    def __init__(
        self,
        mode: ExecutionMode,
        *,
        credentials: KalshiCredentials | None = None,
        transport: SignedTransport | None = None,
    ) -> None:
        # Gate 4: refuse to exist in a mode that may not write.
        if not mode.may_write:
            raise LiveTradingNotAuthorized(
                f"cannot construct an execution (write) client in mode "
                f"{mode.value}; only {ExecutionMode.DEMO.value} may mutate an "
                "account"
            )
        self.profile = ModeProfile.of(mode)
        self.profile.assert_may_write("client construction")
        self._transport = transport or SignedTransport(
            self.profile,
            credentials or load_credentials(mode),
        )

    @property
    def mode(self) -> ExecutionMode:
        return self.profile.mode

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def create_order(
        self, intent: OrderIntent, client_order_id: str
    ) -> Mapping[str, Any]:
        """Submit one order. The caller supplies the derived idempotency key.

        This method does **not** retry. An ambiguous outcome is the caller's to
        reconcile - see :mod:`pmvl_markets.execution.reconcile` - because a blind
        retry here is precisely the duplicate-order bug the idempotency key
        exists to prevent, and reusing the key without first checking remote
        state still risks acting on an order we have not observed.
        """
        self.profile.assert_may_write("create_order")
        if intent.mode is not self.profile.mode:
            raise LiveTradingNotAuthorized(
                f"intent mode {intent.mode.value} does not match client mode "
                f"{self.profile.mode.value}"
            )

        body: dict[str, Any] = {
            "action": intent.action.value,
            "client_order_id": client_order_id,
            "count": intent.quantity,
            "side": intent.outcome.value,
            "ticker": intent.ticker,
            "type": "limit",
            "time_in_force": intent.time_in_force.value,
        }
        # Price field name carries the orientation. Sending yes_price for a NO
        # order would be an inversion, so the key is chosen from the enum.
        if intent.outcome is Outcome.YES:
            body["yes_price"] = intent.limit_price_cents
        else:
            body["no_price"] = intent.limit_price_cents

        log.info(
            "submitting demo order client_order_id=%s ticker=%s %s %s %sc x%d",
            client_order_id,
            intent.ticker,
            intent.action.value,
            intent.outcome.value,
            intent.limit_price_cents,
            intent.quantity,
        )
        return await self._transport.post("/portfolio/orders", json=body)

    async def cancel_order(self, exchange_order_id: str) -> Mapping[str, Any]:
        """Cancel by venue order id. Idempotent: a 404 is reported, not raised."""
        self.profile.assert_may_write("cancel_order")
        try:
            return await self._transport.delete(
                f"/portfolio/orders/{exchange_order_id}"
            )
        except ExecutionApiError as exc:
            if exc.status_code == 404:
                # Already gone. Cancelling twice is a normal retry outcome.
                return {"already_resolved": True}
            raise
