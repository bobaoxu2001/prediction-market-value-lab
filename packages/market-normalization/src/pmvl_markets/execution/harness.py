"""The one path from an intent to a venue. Shadow and demo differ only at the end.

    intent -> ledger(INTENDED) -> exposure -> risk gate -> [SHADOW stops here]
           -> ledger(SUBMITTING) -> venue -> ledger(OPEN/FILLED/REJECTED)

Shadow mode runs every step except the write. That is the point: when the
research system eventually earns authorization, the code that will place real
orders has already been exercised end to end, including the risk arithmetic and
the idempotency derivation, with zero account mutation.

Ambiguity handling
------------------
If the venue call raises after the order may have been received, the order is
written as ``UNKNOWN_RECONCILIATION_REQUIRED`` rather than retried. The retry is
a separate, deliberate act that must reconcile first - see
:mod:`pmvl_markets.execution.reconcile`. The deterministic ``client_order_id``
is what makes that safe.
"""

from __future__ import annotations

from dataclasses import replace

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .client import KalshiAccountClient, KalshiExecutionClient, normalize_order
from .idempotency import derive_client_order_id
from .ledger import ExecutionLedger
from .models import (
    CancelResult,
    ExposureSnapshot,
    OrderIntent,
    OrderResult,
    OrderStatus,
)
from .modes import ExecutionMode, LiveTradingNotAuthorized
from .risk import RiskDecision, RiskGate
from .transport import ExecutionApiError

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .authorization import AuthorizedExecutionPlan

log = get_logger(__name__)

__all__ = ["ExecutionHarness", "PlannedOrder"]


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    """What shadow mode produces: a fully-formed, risk-checked, unsent order."""

    intent: OrderIntent
    client_order_id: str
    decision: RiskDecision
    projected_notional_usd: Any = None
    #: Present only when the decision passed. This is the object a gateway will
    #: accept; a caller holding a PlannedOrder with `authorization is None` has
    #: nothing it can submit, which is the point.
    authorization: "AuthorizedExecutionPlan | None" = None

    @property
    def would_submit(self) -> bool:
        return self.decision.passed and self.authorization is not None


class ExecutionHarness:
    """Binds mode, ledger, risk gate and clients into a single guarded path."""

    def __init__(
        self,
        mode: ExecutionMode,
        *,
        ledger: ExecutionLedger,
        risk_gate: RiskGate,
        account_client: KalshiAccountClient | None = None,
        execution_client: KalshiExecutionClient | None = None,
    ) -> None:
        self.mode = mode
        self.ledger = ledger
        self.risk_gate = risk_gate
        self._account = account_client
        self._execution = execution_client

        if execution_client is not None and not mode.may_write:
            raise LiveTradingNotAuthorized(
                f"an execution client was supplied for mode {mode.value}, "
                "which may not write"
            )

    # ------------------------------------------------------------------ plan
    async def plan(
        self,
        intent: OrderIntent,
        *,
        exposure: ExposureSnapshot | None = None,
        best_bid_cents: int | None = None,
        best_ask_cents: int | None = None,
    ) -> PlannedOrder:
        """Everything short of the write. Used directly by SHADOW mode."""
        if intent.mode is not self.mode:
            raise LiveTradingNotAuthorized(
                f"intent mode {intent.mode.value} does not match harness mode "
                f"{self.mode.value}"
            )

        client_order_id = derive_client_order_id(intent)

        if exposure is None and self._account is not None:
            exposure = await self._account.get_exposure()

        with self.ledger.connect() as con:
            self.ledger.record_intent(con, intent, client_order_id)
            # The daily-spend cap needs a measurement, and the ledger is the only
            # thing that has one. Left unset it defaults to None, which the risk
            # gate blocks on rather than reading as "nothing spent".
            if exposure is not None and exposure.spend_today_usd is None:
                exposure = replace(
                    exposure,
                    spend_today_usd=self.ledger.spend_today_usd(
                        con,
                        today=utcnow().strftime("%Y-%m-%d"),
                        mode=self.mode,
                    ),
                )
            # Unresolved orders AND orphan blocking discrepancies. The
            # second kind - a ghost order or position at the venue with no
            # local row - is the one that used to be invisible here.
            unresolved = len(
                self.ledger.unresolved_orders(con, mode=self.mode)
            ) + len(self.ledger.open_blocking_discrepancies(con, mode=self.mode))

        decision, authorization = self.risk_gate.authorize(
            intent,
            exposure,
            client_order_id=client_order_id,
            best_bid_cents=best_bid_cents,
            best_ask_cents=best_ask_cents,
            unresolved_orders=unresolved,
        )

        if not decision.passed:
            with self.ledger.connect() as con:
                record = self.ledger.get(con, client_order_id)
                if record is not None and record.status is OrderStatus.INTENDED:
                    self.ledger.transition(
                        con,
                        client_order_id,
                        OrderStatus.BLOCKED,
                        error=";".join(decision.detail)[:500],
                        note="risk gate",
                    )

        return PlannedOrder(
            intent=intent,
            client_order_id=client_order_id,
            decision=decision,
            projected_notional_usd=intent.notional_usd,
            authorization=authorization,
        )

    # ---------------------------------------------------------------- submit
    async def submit(
        self,
        intent: OrderIntent,
        *,
        exposure: ExposureSnapshot | None = None,
        best_bid_cents: int | None = None,
        best_ask_cents: int | None = None,
    ) -> OrderResult:
        """Plan, then write if - and only if - the mode and risk gate both allow."""
        planned = await self.plan(
            intent,
            exposure=exposure,
            best_bid_cents=best_bid_cents,
            best_ask_cents=best_ask_cents,
        )
        coid = planned.client_order_id

        if not planned.decision.passed:
            return OrderResult(
                intent_id=intent.intent_id,
                client_order_id=coid,
                status=OrderStatus.BLOCKED,
                mode=self.mode,
                error="; ".join(planned.decision.detail),
                simulated=True,
            )

        if planned.authorization is None:
            # Defensive: a passing decision must carry an authorization. If it
            # does not, something changed the risk gate's contract and the safe
            # reading is "not approved".
            return OrderResult(
                intent_id=intent.intent_id,
                client_order_id=coid,
                status=OrderStatus.BLOCKED,
                mode=self.mode,
                error="risk gate passed without issuing an authorization",
                simulated=True,
            )

        if not self.mode.may_write:
            # SHADOW stops at the gateway, having done every other step: the same
            # resolver, the same risk policy, the same idempotency, the same
            # authorization contract, the same ledger. Only the side effect
            # differs, which is what makes a shadow result evidence about the
            # real path rather than about a parallel one.
            from .gateway import ShadowExecutionGateway

            gateway = ShadowExecutionGateway(
                ledger=self.ledger, kill_switch=self.risk_gate.kill_switch
            )
            result = await gateway.submit(planned.authorization)
            return OrderResult(
                intent_id=intent.intent_id,
                client_order_id=coid,
                status=result.status,
                mode=self.mode,
                remaining_quantity=intent.quantity,
                simulated=True,
            )

        if self._execution is None:
            raise LiveTradingNotAuthorized(
                "no execution client is configured; refusing to submit"
            )

        with self.ledger.connect() as con:
            self.ledger.transition(
                con, coid, OrderStatus.SUBMITTING, submitted=True, note="submitting"
            )

        try:
            payload = await self._execution.create_order(planned.authorization)
        except ExecutionApiError as exc:
            return self._handle_submit_failure(intent, coid, exc)

        return self._apply_submit_response(intent, coid, payload)

    def _handle_submit_failure(
        self, intent: OrderIntent, coid: str, exc: ExecutionApiError
    ) -> OrderResult:
        """A 4xx we understand is a rejection; anything else is ambiguous."""
        rejected = exc.status_code is not None and exc.status_code in (400, 403, 422)
        duplicate = exc.status_code == 409

        if duplicate:
            # The venue already has this client_order_id. That is the
            # idempotency key doing its job - not an error, and emphatically not
            # a reason to submit again.
            status = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            note = "duplicate client_order_id; reconcile before any further action"
        elif rejected:
            status = OrderStatus.REJECTED
            note = f"venue rejected: {exc}"
        else:
            # Timeout, 5xx, transport error. The order may or may not exist.
            status = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            note = f"ambiguous submit outcome: {exc}"

        with self.ledger.connect() as con:
            self.ledger.transition(
                con, coid, status, error=str(exc)[:500], note=note
            )

        log.error("submit failed client_order_id=%s status=%s: %s", coid, status.value, exc)
        return OrderResult(
            intent_id=intent.intent_id,
            client_order_id=coid,
            status=status,
            mode=self.mode,
            error=str(exc),
        )

    def _apply_submit_response(
        self, intent: OrderIntent, coid: str, payload: Mapping[str, Any]
    ) -> OrderResult:
        raw = (payload or {}).get("order") or payload or {}
        order = normalize_order(raw)
        status = order.status
        # A create response that does not name a status we recognise must not be
        # optimistically called OPEN.
        if status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED:
            log.error(
                "create_order returned unrecognised status for %s; flagged", coid
            )

        # A create response is not fill evidence.
        #
        # `record_fill` is the only path to a filled quantity (see ledger.py), so
        # writing FILLED here produced a row that was terminal-and-complete by
        # status and entirely unfilled by quantity - contradicting the stated
        # invariant, and worse, dropping the ticker out of the expected-position
        # map that reconciliation builds from `filled_quantity`.
        #
        # The venue saying "filled" is a claim to verify, not a fill to record.
        # Reconciliation against the fills endpoint resolves it.
        if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            log.info(
                "create_order reported %s for %s; holding at OPEN pending fill "
                "evidence from reconciliation",
                status.value,
                coid,
            )
            status = OrderStatus.OPEN

        with self.ledger.connect() as con:
            self.ledger.transition(
                con,
                coid,
                status,
                exchange_order_id=order.exchange_order_id or None,
                note="submit response",
            )

        return OrderResult(
            intent_id=intent.intent_id,
            client_order_id=coid,
            status=status,
            mode=self.mode,
            exchange_order_id=order.exchange_order_id or None,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity,
            submitted_at=utcnow(),
        )

    # ---------------------------------------------------------------- cancel
    async def cancel(self, client_order_id: str) -> CancelResult:
        """Cancel a demo order. Idempotent; cancelling a resolved order is fine."""
        # A cancel is a venue mutation - modes.py says so explicitly - so the
        # operator's stop control has to reach it. It did not: the authorization
        # contract covered create only, and `cancel` consulted neither the kill
        # switch nor the risk gate that holds it.
        if self.risk_gate.kill_switch.engaged:
            raise LiveTradingNotAuthorized(
                f"kill switch engaged ({self.risk_gate.kill_switch.reason}); "
                f"refusing to cancel {client_order_id}"
            )

        if not self.mode.may_write:
            raise LiveTradingNotAuthorized(
                f"cancel is not permitted in mode {self.mode.value}"
            )
        if self._execution is None:
            raise LiveTradingNotAuthorized("no execution client is configured")

        with self.ledger.connect() as con:
            record = self.ledger.get(con, client_order_id)

        if record is None:
            return CancelResult(
                client_order_id=client_order_id,
                status=OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
                error="no local record of this order",
            )
        if record.exchange_order_id is None:
            return CancelResult(
                client_order_id=client_order_id,
                status=record.status,
                already_resolved=record.status in {
                    OrderStatus.BLOCKED,
                    OrderStatus.INTENDED,
                },
                error="order has no exchange id; nothing to cancel remotely",
            )
        if record.status in {
            OrderStatus.CANCELLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }:
            return CancelResult(
                client_order_id=client_order_id,
                status=record.status,
                exchange_order_id=record.exchange_order_id,
                already_resolved=True,
            )

        with self.ledger.connect() as con:
            self.ledger.transition(
                con, client_order_id, OrderStatus.CANCEL_PENDING, note="cancel requested"
            )

        try:
            payload = await self._execution.cancel_order(record.exchange_order_id)
        except ExecutionApiError as exc:
            with self.ledger.connect() as con:
                self.ledger.transition(
                    con,
                    client_order_id,
                    OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
                    error=str(exc)[:500],
                    note="cancel outcome ambiguous",
                )
            return CancelResult(
                client_order_id=client_order_id,
                status=OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
                exchange_order_id=record.exchange_order_id,
                error=str(exc),
            )

        already = bool((payload or {}).get("already_resolved"))

        # "The venue no longer has this order resting" is not "the venue
        # cancelled it unfilled". An order that filled in the window between the
        # last poll and the cancel produces exactly the same answer, so treating
        # it as CANCELLED is the absence-is-a-fill inversion in reverse: it
        # reports a real position as flat, terminally, where reconciliation can
        # no longer reach it.
        #
        # A confirmed cancel of an order we know to be unfilled is terminal.
        # Anything else needs reconciliation.
        resolved = OrderStatus.CANCELLED
        note = "cancel confirmed"
        if already:
            resolved = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            note = "venue reported nothing to cancel; fill state unverified"
        elif record.filled_quantity:
            resolved = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            note = "cancelled with fills outstanding; position unverified"

        with self.ledger.connect() as con:
            self.ledger.transition(con, client_order_id, resolved, note=note)
        return CancelResult(
            client_order_id=client_order_id,
            status=resolved,
            exchange_order_id=record.exchange_order_id,
            already_resolved=already,
        )
