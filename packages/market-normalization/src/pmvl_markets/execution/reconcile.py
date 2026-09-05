"""Compare what we believe against what the venue says, and never trade to fix it.

The rule
--------

    **A discrepancy is surfaced, not resolved.** No path in this module places,
    cancels, or amends an order. The only remedy for an unexplained difference is
    an operator looking at it.

That is not caution for its own sake. Every "self-healing" execution system that
places a compensating trade to square its books does so on the belief that its
own view is right, which is exactly the belief the discrepancy has called into
question.

Absence is not a fill
---------------------
An order in the local ledger that the venue does not list as open has *not*
necessarily filled. It may have filled, been cancelled, expired, been rejected
after acceptance, or never existed. Without a fill record or an explicit terminal
status from the venue, it becomes
:attr:`~pmvl_markets.execution.models.OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED`,
which blocks further mutation through the risk gate.

Restart recovery
----------------
:func:`recover_after_restart` reloads the ledger and reconciles it against the
venue. It never re-submits anything. An intent that was ``SUBMITTING`` when the
process died is the dangerous case - it may or may not have reached the venue -
and it is resolved by *looking*, using the deterministic ``client_order_id`` to
find the order remotely, rather than by sending it again.
"""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Mapping, Sequence

from pmvl_shared.logging_setup import get_logger

from .ledger import ExecutionLedger, LedgerRecord
from .models import (
    ExecutionMode,
    Fill,
    Order,
    OrderStatus,
    Position,
    TERMINAL_STATUSES,
)

log = get_logger(__name__)

__all__ = [
    "DiscrepancyKind",
    "Discrepancy",
    "ReconciliationReport",
    "reconcile_orders",
    "reconcile_positions",
    "resolve_unknown_order",
    "OrderResolution",
    "recover_after_restart",
]


class DiscrepancyKind(StrEnum):
    RECONCILED = "reconciled"
    LOCAL_ONLY = "local_only"
    REMOTE_ONLY = "remote_only"
    STATUS_MISMATCH = "status_mismatch"
    FILL_MISMATCH = "fill_mismatch"
    POSITION_MISMATCH = "position_mismatch"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: DiscrepancyKind
    client_order_id: str = ""
    exchange_order_id: str = ""
    ticker: str = ""
    detail: str = ""
    #: True when this discrepancy must block further mutation.
    blocking: bool = False


@dataclass(slots=True)
class ReconciliationReport:
    discrepancies: list[Discrepancy] = field(default_factory=list)
    reconciled: int = 0
    checked: int = 0

    @property
    def blocking(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.blocking]

    @property
    def is_clean(self) -> bool:
        return not self.blocking

    def add(self, d: Discrepancy) -> None:
        self.discrepancies.append(d)
        if d.kind is DiscrepancyKind.RECONCILED:
            self.reconciled += 1

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for d in self.discrepancies:
            counts[d.kind.value] = counts.get(d.kind.value, 0) + 1
        parts = [f"{k}={v}" for k, v in sorted(counts.items())]
        return (
            f"checked={self.checked} " + " ".join(parts)
            if parts
            else f"checked={self.checked}"
        )


def _fills_by_order(fills: Iterable[Fill]) -> Mapping[str, list[Fill]]:
    out: dict[str, list[Fill]] = {}
    for f in fills:
        key = f.exchange_order_id or ""
        out.setdefault(key, []).append(f)
    return out


def reconcile_orders(
    local: Sequence[LedgerRecord],
    remote_orders: Sequence[Order],
    remote_fills: Sequence[Fill],
) -> ReconciliationReport:
    """Classify every local and remote order against the other side."""
    report = ReconciliationReport()
    by_client = {o.client_order_id: o for o in remote_orders if o.client_order_id}
    by_exchange = {o.exchange_order_id: o for o in remote_orders}
    fills_by_order = _fills_by_order(remote_fills)

    seen_remote: set[str] = set()

    for record in local:
        report.checked += 1
        remote = by_client.get(record.client_order_id)
        if remote is None and record.exchange_order_id:
            remote = by_exchange.get(record.exchange_order_id)
        if remote is not None:
            seen_remote.add(remote.exchange_order_id)

        local_fills = fills_by_order.get(record.exchange_order_id or "", [])
        remote_filled = sum(f.quantity for f in local_fills)

        if remote is None:
            if record.status in TERMINAL_STATUSES:
                # Already settled locally and gone remotely. Consistent.
                report.add(
                    Discrepancy(
                        DiscrepancyKind.RECONCILED,
                        client_order_id=record.client_order_id,
                        ticker=record.ticker,
                    )
                )
                continue

            if remote_filled > 0:
                # Gone from the open list, but there is fill evidence. This is
                # the ONLY route to a filled conclusion.
                kind = (
                    DiscrepancyKind.RECONCILED
                    if remote_filled == record.requested_quantity
                    else DiscrepancyKind.FILL_MISMATCH
                )
                report.add(
                    Discrepancy(
                        kind,
                        client_order_id=record.client_order_id,
                        exchange_order_id=record.exchange_order_id or "",
                        ticker=record.ticker,
                        detail=(
                            f"absent remotely with {remote_filled}/"
                            f"{record.requested_quantity} filled"
                        ),
                        blocking=kind is DiscrepancyKind.FILL_MISMATCH,
                    )
                )
                continue

            # Open locally, absent remotely, no fills. Do not call this FILLED.
            report.add(
                Discrepancy(
                    DiscrepancyKind.LOCAL_ONLY,
                    client_order_id=record.client_order_id,
                    exchange_order_id=record.exchange_order_id or "",
                    ticker=record.ticker,
                    detail=(
                        f"local status {record.status.value} but the venue does "
                        "not know this order and reports no fills"
                    ),
                    blocking=True,
                )
            )
            continue

        # Present on both sides.
        if remote.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED:
            report.add(
                Discrepancy(
                    DiscrepancyKind.STATUS_MISMATCH,
                    client_order_id=record.client_order_id,
                    exchange_order_id=remote.exchange_order_id,
                    ticker=record.ticker,
                    detail=f"venue returned unrecognised status {remote.raw_status!r}",
                    blocking=True,
                )
            )
            continue

        if remote.filled_quantity != record.filled_quantity:
            report.add(
                Discrepancy(
                    DiscrepancyKind.FILL_MISMATCH,
                    client_order_id=record.client_order_id,
                    exchange_order_id=remote.exchange_order_id,
                    ticker=record.ticker,
                    detail=(
                        f"local filled {record.filled_quantity}, "
                        f"remote filled {remote.filled_quantity}"
                    ),
                    blocking=True,
                )
            )
            continue

        if remote.status is not record.status:
            report.add(
                Discrepancy(
                    DiscrepancyKind.STATUS_MISMATCH,
                    client_order_id=record.client_order_id,
                    exchange_order_id=remote.exchange_order_id,
                    ticker=record.ticker,
                    detail=(
                        f"local {record.status.value}, remote {remote.status.value}"
                    ),
                    # A status difference with matching fills is usually a lag,
                    # not a loss. Non-blocking, but recorded.
                    blocking=False,
                )
            )
            continue

        report.add(
            Discrepancy(
                DiscrepancyKind.RECONCILED,
                client_order_id=record.client_order_id,
                exchange_order_id=remote.exchange_order_id,
                ticker=record.ticker,
            )
        )

    # Anything the venue holds that we have no record of at all.
    for remote in remote_orders:
        if remote.exchange_order_id in seen_remote:
            continue
        if not remote.is_open:
            continue
        report.add(
            Discrepancy(
                DiscrepancyKind.REMOTE_ONLY,
                exchange_order_id=remote.exchange_order_id,
                client_order_id=remote.client_order_id or "",
                ticker=remote.ticker,
                detail="venue holds an open order with no local record",
                blocking=True,
            )
        )

    return report


def reconcile_positions(
    local_positions: Mapping[str, int],
    remote_positions: Sequence[Position],
) -> list[Discrepancy]:
    """Compare expected net quantity against the venue's positions.

    Keyed by ``(ticker, outcome)``, not by ticker.

    Orientation is the headline invariant of this subsystem, and collapsing it
    here defeated the one check that compares local belief against venue truth:
    believing you are long 5 YES while the account is long 5 NO - a total
    inversion of the model's view - reconciled clean. Summing per ticker also
    made the quantity comparison unreliable whenever two orders touched the same
    market from opposite sides.
    """
    out: list[Discrepancy] = []
    remote_map: dict[tuple[str, str], int] = {}
    for p in remote_positions:
        key = (p.ticker, p.outcome.value)
        remote_map[key] = remote_map.get(key, 0) + p.quantity

    for key in set(local_positions) | set(remote_map):
        expected = local_positions.get(key, 0)
        actual = remote_map.get(key, 0)
        if expected != actual:
            ticker, outcome = key
            out.append(
                Discrepancy(
                    DiscrepancyKind.POSITION_MISMATCH,
                    ticker=ticker,
                    detail=(
                        f"expected {expected} {outcome.upper()} contracts, "
                        f"venue reports {actual}"
                    ),
                    blocking=True,
                )
            )
    return out


def recover_after_restart(
    ledger: ExecutionLedger,
    remote_orders: Sequence[Order],
    remote_fills: Sequence[Fill],
    remote_positions: Sequence[Position],
    *,
    mode: ExecutionMode = ExecutionMode.DEMO,
) -> ReconciliationReport:
    """Rebuild state after a process restart. Submits nothing.

    ``mode`` scopes both the local rows considered and the discrepancies
    written. Reconciling a production account against demo bookkeeping compares
    two different accounts: every difference is a manufactured discrepancy, and
    the demo orders it wedges were never wrong. Local rows from another mode are
    therefore not even read.

    Orders left mid-flight are resolved by looking them up remotely via their
    deterministic ``client_order_id``. Anything still ambiguous is written as
    ``UNKNOWN_RECONCILIATION_REQUIRED``, which the risk gate treats as a hard
    block on new mutations until an operator clears it.
    """
    with ledger.connect() as con:
        local = ledger.open_orders(con, mode=mode)
        report = reconcile_orders(local, remote_orders, remote_fills)

        # Persist fill evidence before touching statuses, so a status can never
        # claim more than the fills support.
        by_exchange: dict[str, str] = {
            r.exchange_order_id: r.client_order_id
            for r in local
            if r.exchange_order_id
        }
        by_client = {r.client_order_id: r for r in local}
        for fill in remote_fills:
            coid = by_exchange.get(fill.exchange_order_id or "")
            if coid and fill.fill_id:
                ledger.record_fill(con, fill, client_order_id=coid)

        remote_by_client = {
            o.client_order_id: o for o in remote_orders if o.client_order_id
        }

        for d in report.discrepancies:
            record = by_client.get(d.client_order_id)
            if record is None:
                # Nothing local to mark, so persist the discrepancy itself.
                # Skipping it here is what let a ghost order at the venue coexist
                # with a clean-looking local ledger.
                if d.blocking:
                    ledger.record_blocking_discrepancy(
                        con,
                        mode=mode,
                        kind=d.kind.value,
                        ticker=d.ticker,
                        detail=d.detail,
                    )
                continue
            if d.kind is DiscrepancyKind.RECONCILED:
                remote = remote_by_client.get(d.client_order_id)
                if remote is not None and remote.status is not record.status:
                    _safe_transition(
                        ledger, con, record, remote.status, "restart reconciliation"
                    )
                ledger.mark_reconciled(con, record.client_order_id)
                continue

            # Everything else is ambiguous. Block rather than guess.
            _safe_transition(
                ledger,
                con,
                record,
                OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
                f"{d.kind.value}: {d.detail}",
            )

        local_net: dict[tuple[str, str], int] = {}
        for row in ledger.all_orders(con, mode=mode):
            if not row.filled_quantity:
                continue
            key = (row.ticker, str(row.outcome))
            local_net[key] = local_net.get(key, 0) + row.filled_quantity
        for d in reconcile_positions(local_net, remote_positions):
            report.add(d)
            ledger.record_blocking_discrepancy(
                con, mode=mode, kind=d.kind.value, ticker=d.ticker, detail=d.detail
            )

        ledger.record_reconciliation(con, mode.value, report.summary())

    log.info("restart reconciliation: %s", report.summary())
    return report


def _safe_transition(
    ledger: ExecutionLedger,
    con: object,
    record: LedgerRecord,
    new_status: OrderStatus,
    note: str,
) -> None:
    """Apply a transition, downgrading an illegal one to a logged no-op.

    A refused transition during recovery must not abort the whole recovery: the
    remaining orders still need reconciling, and the offending one stays in its
    prior state where it continues to block.
    """
    from .ledger import InvalidTransition

    try:
        ledger.transition(con, record.client_order_id, new_status, note=note)  # type: ignore[arg-type]
    except InvalidTransition as exc:
        log.error(
            "reconciliation could not apply %s -> %s for %s: %s",
            record.status.value,
            new_status.value,
            record.client_order_id,
            exc,
        )


@dataclass(frozen=True, slots=True)
class OrderResolution:
    """Outcome of an evidence-driven attempt to resolve one ambiguous order."""

    client_order_id: str
    resolved: bool
    status: OrderStatus
    evidence: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "resolved": self.resolved,
            "status": self.status.value,
            "evidence": list(self.evidence),
            "detail": self.detail,
        }


def resolve_unknown_order(
    ledger: ExecutionLedger,
    client_order_id: str,
    *,
    mode: ExecutionMode,
    remote_order: Order | None,
    remote_fills: Sequence[Fill] = (),
    operator_note: str = "",
) -> OrderResolution:
    """Move one order out of UNKNOWN, but only where the venue says so.

    There is deliberately no ``force``, no ``mark_filled``, no ``ignore``. The
    entire value of ``UNKNOWN_RECONCILIATION_REQUIRED`` is that it means "this
    system does not know", and a switch that lets an operator assert otherwise
    converts a safe stop into a guess with a human's name on it. What an operator
    can do is *fetch evidence*; what the evidence says is not up to them.

    An ``operator_note`` is recorded for the audit trail. It never authorizes a
    transition on its own - a note attached to still-ambiguous state leaves the
    order exactly as ambiguous as it was.
    """
    with ledger.connect() as con:
        record = ledger.get(con, client_order_id)
        if record is None:
            return OrderResolution(
                client_order_id, False, OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
                detail="no local order with that id",
            )
        if str(record.mode) != mode.value:
            # Resolving a demo order with production evidence compares two
            # different accounts.
            return OrderResolution(
                client_order_id, False, record.status,
                detail=(
                    f"order belongs to mode {record.mode}, "
                    f"evidence was fetched for {mode.value}"
                ),
            )

        evidence: list[str] = []
        for fill in remote_fills:
            if ledger.record_fill(con, fill, client_order_id=client_order_id):
                evidence.append(f"fill {fill.fill_id}: {fill.quantity}@{fill.price_cents}c")

        if remote_order is None:
            # Absence is not a fill, and it is not a cancellation either. Without
            # a venue statement there is nothing to act on.
            note = f"venue has no record; still ambiguous. {operator_note}".strip()
            ledger.record_resolution_attempt(
                con, client_order_id=client_order_id, mode=mode.value,
                resolved=False, detail=note,
            )
            return OrderResolution(
                client_order_id, False, OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
                evidence=tuple(evidence),
                detail="venue returned no order; absence is not evidence of an outcome",
            )

        target = remote_order.status
        if target is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED:
            ledger.record_resolution_attempt(
                con, client_order_id=client_order_id, mode=mode.value,
                resolved=False, detail=f"venue status unrecognised. {operator_note}".strip(),
            )
            return OrderResolution(
                client_order_id, False, OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED,
                evidence=tuple(evidence),
                detail="venue reported a status this system does not recognise",
            )

        ledger.force_status_from_evidence(
            con,
            client_order_id=client_order_id,
            status=target,
            note=f"resolved from venue evidence. {operator_note}".strip(),
            exchange_order_id=remote_order.exchange_order_id or None,
        )
        ledger.record_resolution_attempt(
            con, client_order_id=client_order_id, mode=mode.value,
            resolved=True, detail=f"venue reports {target.value}",
        )
        evidence.append(f"venue order status: {target.value}")
        return OrderResolution(
            client_order_id, True, target, evidence=tuple(evidence),
            detail=f"resolved to {target.value} on venue evidence",
        )
