"""Run real research decisions through the real execution path, and stop.

Shadow is only worth running if it is the *same* path. This module therefore
reuses the production resolver, the production risk gate, the production
idempotency derivation, the production authorization contract and the production
ledger. There is no ``ShadowRiskGate`` and no relaxed limit set: a shadow verdict
that came from weaker rules would answer a question nobody asked.

The single difference is the final side effect. ``ShadowExecutionGateway`` holds
no transport and no credentials, so "shadow accidentally traded" is not a failure
mode it can have.

Every decision is recorded, not just the approved ones. The interesting number in
this phase is the denominator: how many real signals existed, how many were
expressible as orders, and where the rest stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .authorization import AuthorizationInvalid
from .gateway import ShadowExecutionGateway
from .idempotency import derive_client_order_id
from .ledger import ExecutionLedger
from .models import (
    AccountBalance,
    ExecutionMode,
    ExposureSnapshot,
    Order,
    OrderIntent,
    OrderStatus,
    Outcome,
    Position,
)
from .risk import KillSwitch, RiskGate
from .signals import IneligibleSignal, ResearchDecision, SignalExecutionAdapter

log = get_logger(__name__)

__all__ = ["ShadowAccountState", "ShadowRunReport", "ShadowSignalPipeline"]

#: Virtual capital for shadow. Deliberately a fixed, small, declared number - not
#: the operator's real Kalshi balance, which is not risk capital and must never
#: be treated as an allowance.
SHADOW_VIRTUAL_BALANCE_USD = Decimal("100")


@dataclass
class ShadowAccountState:
    """Exposure a shadow run has committed to, reconstructed from the ledger.

    Shaped to be swappable: the risk gate consumes an
    :class:`ExposureSnapshot`, so a later phase can hand it a
    ``PRODUCTION_READ_ONLY`` snapshot instead without changing risk semantics.
    """

    ledger: ExecutionLedger
    virtual_balance_usd: Decimal = SHADOW_VIRTUAL_BALANCE_USD

    def snapshot(self, *, now: datetime | None = None) -> ExposureSnapshot:
        now = now or utcnow()
        positions: list[Position] = []
        resting: list[Order] = []
        with self.ledger.connect() as con:
            rows = self.ledger.all_orders(con, mode=ExecutionMode.SHADOW)
            spend = self.ledger.spend_today_usd(
                con, today=now.strftime("%Y-%m-%d"), mode=ExecutionMode.SHADOW
            )
            unresolved = self.ledger.unresolved_orders(
                con, mode=ExecutionMode.SHADOW
            )
            blocking = self.ledger.open_blocking_discrepancies(
                con, mode=ExecutionMode.SHADOW
            )

        for row in rows:
            if row.status is not OrderStatus.SHADOW_AUTHORIZED:
                continue
            # An authorized shadow plan is committed exposure for the purposes of
            # the next decision. Ignoring it would let one run approve the same
            # market repeatedly and call the total "flat".
            price = row.requested_price_cents or 0
            qty = row.requested_quantity or 0
            resting.append(
                Order(
                    exchange_order_id=f"shadow:{row.client_order_id}",
                    ticker=row.ticker,
                    outcome=Outcome(str(row.outcome)),
                    action=row.action,  # type: ignore[arg-type]
                    status=OrderStatus.OPEN,
                    limit_price_cents=price,
                    original_quantity=qty,
                    remaining_quantity=qty,
                    client_order_id=row.client_order_id,
                )
            )

        return ExposureSnapshot(
            balance=AccountBalance(
                balance_usd=self.virtual_balance_usd,
                available_usd=self.virtual_balance_usd,
            ),
            positions=tuple(positions),
            resting_orders=tuple(resting),
            fetched_at=now,
            # Shadow books no realized P&L, because nothing traded. That is a
            # measured zero, not an unmeasured one.
            realized_loss_today_usd=Decimal("0"),
            spend_today_usd=spend,
            extra={
                "unresolved_orders": len(unresolved),
                "blocking_discrepancies": len(blocking),
            },
        )


@dataclass
class ShadowRunReport:
    """Denominator-first accounting for one shadow pass."""

    signals_seen: int = 0
    ineligible: dict[str, int] = field(default_factory=dict)
    resolution_blocked: int = 0
    risk_blocked: dict[str, int] = field(default_factory=dict)
    authorized: int = 0
    deduplicated: int = 0
    errors: list[str] = field(default_factory=list)
    authorized_plans: list[dict[str, Any]] = field(default_factory=list)

    @property
    def actionable(self) -> int:
        return self.signals_seen - sum(self.ineligible.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "signals_seen": self.signals_seen,
            "actionable": self.actionable,
            "ineligible": dict(sorted(self.ineligible.items())),
            "resolution_blocked": self.resolution_blocked,
            "risk_blocked": dict(sorted(self.risk_blocked.items())),
            "deduplicated": self.deduplicated,
            "shadow_authorized": self.authorized,
            "errors": self.errors[:10],
        }


class ShadowSignalPipeline:
    """ResearchDecision -> intent -> risk -> authorization -> shadow ledger."""

    def __init__(
        self,
        *,
        ledger: ExecutionLedger,
        adapter: SignalExecutionAdapter | None = None,
        risk_gate: RiskGate | None = None,
        account: ShadowAccountState | None = None,
    ) -> None:
        self.ledger = ledger
        self.adapter = adapter or SignalExecutionAdapter(mode=ExecutionMode.SHADOW)
        # The real gate, with the real limits and the configured kill switch.
        self.risk_gate = risk_gate or RiskGate()
        self.account = account or ShadowAccountState(ledger=ledger)
        self.gateway = ShadowExecutionGateway(
            ledger=ledger, kill_switch=self.risk_gate.kill_switch
        )

    async def run(
        self, decisions: Iterable[ResearchDecision], *, now: datetime | None = None
    ) -> ShadowRunReport:
        report = ShadowRunReport()
        for decision in decisions:
            report.signals_seen += 1
            outcome = self.adapter.to_intent(decision, now=now)
            if isinstance(outcome, IneligibleSignal):
                key = outcome.reason.value
                report.ineligible[key] = report.ineligible.get(key, 0) + 1
                continue
            try:
                await self._process(outcome, report, now=now)
            except Exception as exc:  # noqa: BLE001 - one bad signal must not
                # abort the pass; the denominator is the point of the run.
                report.errors.append(f"{outcome.intent_id}: {type(exc).__name__}")
                log.warning("shadow pipeline error for %s", outcome.intent_id)
        return report

    async def _process(
        self,
        intent: OrderIntent,
        report: ShadowRunReport,
        *,
        now: datetime | None,
    ) -> None:
        coid = derive_client_order_id(intent)

        with self.ledger.connect() as con:
            existing = self.ledger.get(con, coid)
            if existing is not None:
                # Same logical decision, already planned. Not an error and not a
                # second plan - this is the idempotency key doing its job across
                # runs and across restarts.
                report.deduplicated += 1
                return
            self.ledger.record_intent(con, intent, coid)

        exposure = self.account.snapshot(now=now)
        untrusted = int(exposure.extra.get("unresolved_orders", 0)) + int(
            exposure.extra.get("blocking_discrepancies", 0)
        )

        decision, plan = self.risk_gate.authorize(
            intent,
            exposure,
            client_order_id=coid,
            now=now,
            unresolved_orders=untrusted,
        )

        if plan is None:
            for reason in decision.reasons:
                report.risk_blocked[reason.value] = (
                    report.risk_blocked.get(reason.value, 0) + 1
                )
            with self.ledger.connect() as con:
                record = self.ledger.get(con, coid)
                if record is not None and record.status is OrderStatus.INTENDED:
                    self.ledger.transition(
                        con,
                        coid,
                        OrderStatus.BLOCKED,
                        error="; ".join(decision.detail)[:500],
                        note="risk gate",
                    )
            return

        try:
            await self.gateway.submit(plan)
        except AuthorizationInvalid as exc:
            # The gateway re-checks at the point of use and may still refuse -
            # an expired authorization, a kill switch flipped mid-run, account
            # state that went untrusted. That refusal is a block, not an error.
            report.risk_blocked["gateway_refused"] = (
                report.risk_blocked.get("gateway_refused", 0) + 1
            )
            with self.ledger.connect() as con:
                record = self.ledger.get(con, coid)
                if record is not None and record.status is OrderStatus.INTENDED:
                    self.ledger.transition(
                        con, coid, OrderStatus.BLOCKED,
                        error=str(exc)[:500], note="gateway",
                    )
            return

        report.authorized += 1
        report.authorized_plans.append(plan.as_dict())


def load_research_decisions(
    db_path: str, *, limit: int = 500, only_actionable: bool = False
) -> list[ResearchDecision]:
    """Read the canonical research record. Read-only, never writes.

    Opened read-only against ``pmvl.db`` on purpose: the execution subsystem is
    a *consumer* of research output and must not be able to alter it, and the
    contention programme means it should not be adding writes there either.
    """
    import sqlite3

    from pmvl_shared.enums import RecommendationState, Side

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = """
        SELECT r.*, m.platform_market_id, m.platform, m.status AS market_status,
               m.close_time
        FROM recommendations r JOIN markets m ON m.id = r.market_id
    """
    if only_actionable:
        sql += " WHERE r.state = 'still_actionable'"
    sql += " ORDER BY r.created_at DESC, r.id DESC LIMIT ?"

    def _dec(value: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    def _dt(value: Any) -> datetime | None:
        """Stored timestamps are naive UTC; comparisons here are tz-aware."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    out: list[ResearchDecision] = []
    try:
        for row in con.execute(sql, (limit,)):
            try:
                state = RecommendationState(row["state"])
            except ValueError:
                continue
            out.append(
                ResearchDecision(
                    recommendation_id=row["id"],
                    batch_id=row["batch_id"] or "",
                    ticker=row["platform_market_id"] or "",
                    platform=row["platform"] or "",
                    side=Side(row["side"]),
                    state=state,
                    market_status=row["market_status"] or "",
                    entry_price=_dec(row["entry_price"]),
                    current_price=_dec(row["current_price"]),
                    fair_probability=_dec(row["fair_probability"]),
                    conservative_probability=_dec(row["fair_probability_low"]),
                    net_ev_per_contract=_dec(row["net_ev_per_contract"]),
                    conservative_net_ev=_dec(row["conservative_net_ev"]),
                    total_cost_per_contract=_dec(row["total_cost_per_contract"]),
                    recommended_position_cap=_dec(row["recommended_position_cap"]),
                    model_version=row["model_version"] or "",
                    provenance=row["provenance"] or "",
                    horizon=row["horizon"] or "",
                    created_at=_dt(row["created_at"]),
                    evidence_updated_at=_dt(row["evidence_updated_at"]),
                    close_time=_dt(row["close_time"]),
                    expected_resolution_time=_dt(row["expected_resolution_time"]),
                )
            )
    finally:
        con.close()
    return out
