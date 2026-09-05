"""The mutation-facing surface. Accepts an approved plan and nothing else.

Every gateway here takes an :class:`AuthorizedExecutionPlan` and no loose order
parameters. There is no ``submit(ticker, price, qty)`` overload and no
``submit(intent)``, so the shortcut that Phase 5A left open - construct the write
client, call ``create_order`` with a raw intent, consult no risk gate - is not
expressible against this API.

Defense in depth is preserved rather than replaced. A gateway re-checks the
plan's seal, its expiry, its mode, and the kill switch **at the point of use**,
and the layers beneath it still enforce their own gates independently: the write
client refuses to construct outside a writing mode, and the transport refuses any
non-GET verb aimed at a production host. A bug in the authorization layer must
not be sufficient to reach production, so nothing below it was relaxed.

``ShadowExecutionGateway`` runs the identical path and stops before the socket.
That is the whole point of shadow: same resolver, same risk policy, same
idempotency, same authorization contract, same ledger - only the final side
effect differs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .authorization import AuthorizationInvalid, AuthorizedExecutionPlan
from .ledger import ExecutionLedger
from .models import ExecutionMode, OrderStatus
from .modes import LiveTradingNotAuthorized
from .risk import KillSwitch

log = get_logger(__name__)

__all__ = [
    "ExecutionGateway",
    "ShadowExecutionGateway",
    "DemoExecutionGateway",
    "GatewayResult",
    "SHADOW_TERMINAL_STATUSES",
]

#: A shadow plan never reaches a venue, so it may never claim a venue outcome.
#: Recording FILLED for something that was never sent is the same fabrication as
#: recording FILLED from a create response, one layer earlier.
SHADOW_TERMINAL_STATUSES = frozenset(
    {OrderStatus.SHADOW_AUTHORIZED, OrderStatus.BLOCKED, OrderStatus.EXPIRED}
)


class GatewayResult:
    """What a gateway did. Deliberately not an order state machine."""

    __slots__ = ("client_order_id", "status", "simulated", "detail", "at")

    def __init__(
        self,
        *,
        client_order_id: str,
        status: OrderStatus,
        simulated: bool,
        detail: str = "",
        at: datetime | None = None,
    ) -> None:
        self.client_order_id = client_order_id
        self.status = status
        self.simulated = simulated
        self.detail = detail
        self.at = at or utcnow()

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "status": self.status.value,
            "simulated": self.simulated,
            "detail": self.detail,
        }


class ExecutionGateway(Protocol):
    """The contract every execution surface implements."""

    mode: ExecutionMode

    async def submit(self, plan: AuthorizedExecutionPlan) -> GatewayResult: ...


class _BaseGateway:
    """Shared point-of-use checks. Subclasses decide the side effect."""

    mode: ExecutionMode

    def __init__(
        self,
        *,
        ledger: ExecutionLedger,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.ledger = ledger
        # Configured by default, not a blank switch. A gateway built with the
        # obvious one-liner must be guarded.
        self.kill_switch = kill_switch or KillSwitch.from_settings(mode=self.mode)

    def _admit(self, plan: AuthorizedExecutionPlan) -> None:
        """Re-verify everything at the moment of use.

        Approval is a statement about a moment. Between that moment and this one
        the plan may have been edited, the authorization may have expired, or an
        operator may have hit the kill switch - so none of it is taken on trust.
        """
        plan.assert_usable(mode=self.mode)
        if self.kill_switch.engaged:
            raise AuthorizationInvalid(
                f"kill switch engaged ({self.kill_switch.reason}); "
                f"refusing to submit {plan.client_order_id}"
            )
        with self.ledger.connect() as con:
            blocking = self.ledger.open_blocking_discrepancies(con, mode=self.mode)
            unresolved = self.ledger.unresolved_orders(con, mode=self.mode)
        if blocking or unresolved:
            raise AuthorizationInvalid(
                f"account state untrusted: {len(unresolved)} unresolved "
                f"order(s) and {len(blocking)} open discrepancy(ies) in "
                f"{self.mode.value}; reconcile before submitting"
            )


class ShadowExecutionGateway(_BaseGateway):
    """Records the approved plan. Opens no socket, ever.

    Holds no transport and no credentials, so "shadow accidentally traded" is not
    a failure this class can have - there is nothing in it to trade with.
    """

    mode = ExecutionMode.SHADOW

    async def submit(self, plan: AuthorizedExecutionPlan) -> GatewayResult:
        self._admit(plan)
        # Shadow is the terminal step on its own path, so it spends the
        # authorization here. Demo spends it in the client, at the POST.
        plan.consume()
        with self.ledger.connect() as con:
            self.ledger.transition(
                con,
                plan.client_order_id,
                OrderStatus.SHADOW_AUTHORIZED,
                # `submitted=True` records that this plan was acted on. Without
                # it `submitted_at` stays NULL and the daily-spend cap measures
                # a hard zero in shadow - the same inert-limit bug the realized
                # loss breaker already had.
                submitted=True,
                note="shadow: risk-authorized, not sent",
            )
        log.info(
            "shadow authorized %s %s %s %s %sc x%s",
            plan.client_order_id,
            plan.ticker,
            plan.action.value,
            plan.outcome.value,
            plan.limit_price_cents,
            plan.quantity,
        )
        return GatewayResult(
            client_order_id=plan.client_order_id,
            status=OrderStatus.SHADOW_AUTHORIZED,
            simulated=True,
            detail="shadow: no venue interaction",
        )


class DemoExecutionGateway(_BaseGateway):
    """Sends the approved plan to the Kalshi sandbox.

    Present so that shadow and demo share one contract; Phase 5B does not drive
    it automatically. The write client it wraps still enforces its own mode and
    transport gates independently of anything decided here.
    """

    mode = ExecutionMode.DEMO

    def __init__(
        self,
        client: Any,
        *,
        ledger: ExecutionLedger,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        super().__init__(ledger=ledger, kill_switch=kill_switch)
        if getattr(client, "mode", None) is not ExecutionMode.DEMO:
            raise LiveTradingNotAuthorized(
                "demo gateway requires a demo execution client, got "
                f"{getattr(client, 'mode', None)}"
            )
        self._client = client

    async def submit(self, plan: AuthorizedExecutionPlan) -> GatewayResult:
        self._admit(plan)
        with self.ledger.connect() as con:
            self.ledger.transition(
                con,
                plan.client_order_id,
                OrderStatus.SUBMITTING,
                submitted=True,
                note="demo submit",
            )
        payload: Mapping[str, Any] = await self._client.create_order(plan)
        return GatewayResult(
            client_order_id=plan.client_order_id,
            status=OrderStatus.OPEN,
            simulated=False,
            detail=str(payload.get("order", {}).get("order_id", ""))[:64],
        )
