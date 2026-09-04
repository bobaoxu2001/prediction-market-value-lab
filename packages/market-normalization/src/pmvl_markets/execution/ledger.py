"""Durable execution ledger, in its own SQLite file.

Why not ``pmvl.db``
-------------------
Three reasons, in order of weight.

1. **That database is under an open contention investigation.** Phase 4C measured
   write transactions on ``pmvl.db`` holding for 40-55 seconds after the Family C
   repair, and 348 seconds before it, with organic ``database is locked`` victims
   at roughly 0.68/hour. A write that can be starved for a minute is an
   unacceptable home for "did I just send an order to a venue".
2. **A lost ledger row is worse here than anywhere else in the repo.** Phase 4C
   already found the case where an ``OperationalError`` deleted a ``job_runs``
   row and the run became invisible. The same failure against an order record
   means the system does not know it has an open position. Isolating the file
   removes the shared-lock coupling that caused it.
3. **The consistency argument does not require sharing.** An order's durability
   has nothing to do with a research snapshot's durability; there is no invariant
   spanning both that would need one transaction. They are separate facts about
   separate systems.

There is precedent: ``research-archive.db`` is already a second store with its
own lifecycle, and this follows its shape.

Recovery semantics
------------------
The ledger is the local half of the truth; the venue is the other half. On
restart nothing is re-submitted from the ledger - see
:mod:`pmvl_markets.execution.reconcile`. The ledger says what we *believe*, the
venue says what *is*, and disagreement is surfaced rather than resolved by
trading.

``FILLED`` is never written without fill evidence. :meth:`ExecutionLedger.record_fill`
is the only path to a filled quantity, and it is additive and idempotent by fill
id, so replaying the same fills converges instead of double-counting.

No secret ever enters this file. There is no column for a key, and
:meth:`record_intent` takes an intent, which has no credential field.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Sequence

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .models import (
    Fill,
    OrderIntent,
    OrderStatus,
    is_valid_transition,
)

log = get_logger(__name__)

__all__ = [
    "ExecutionLedger",
    "LedgerRecord",
    "LedgerError",
    "InvalidTransition",
    "default_execution_db_path",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1


class LedgerError(RuntimeError):
    """Execution ledger integrity failure."""


class InvalidTransition(LedgerError):
    """A status change the state machine does not permit."""


def default_execution_db_path(root: Path | str = ".") -> Path:
    return Path(root) / "data" / "execution.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per logical order. `client_order_id` is UNIQUE: that constraint is
-- the local half of the duplicate-order guarantee, and it holds even if the
-- venue's own idempotency were to fail.
CREATE TABLE IF NOT EXISTS execution_orders (
    client_order_id      TEXT PRIMARY KEY,
    intent_id            TEXT NOT NULL,
    mode                 TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    outcome              TEXT NOT NULL,
    action               TEXT NOT NULL,
    requested_price_cents INTEGER NOT NULL,
    requested_quantity   INTEGER NOT NULL,
    status               TEXT NOT NULL,
    exchange_order_id    TEXT,
    filled_quantity      INTEGER NOT NULL DEFAULT 0,
    average_fill_price_cents TEXT,
    fees_usd             TEXT,
    strategy             TEXT NOT NULL DEFAULT '',
    strategy_version     TEXT NOT NULL DEFAULT '',
    decision_epoch       TEXT NOT NULL DEFAULT '',
    source_signal_id     TEXT NOT NULL DEFAULT '',
    revision             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    submitted_at         TEXT,
    last_reconciled_at   TEXT,
    error                TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_exec_orders_status ON execution_orders (status);
CREATE INDEX IF NOT EXISTS ix_exec_orders_ticker ON execution_orders (ticker);
CREATE UNIQUE INDEX IF NOT EXISTS ux_exec_orders_exchange_id
    ON execution_orders (exchange_order_id)
    WHERE exchange_order_id IS NOT NULL;

-- Append-only audit of every status change, so a disputed order history can be
-- reconstructed without trusting the current row.
CREATE TABLE IF NOT EXISTS execution_transitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id  TEXT NOT NULL,
    from_status      TEXT NOT NULL,
    to_status        TEXT NOT NULL,
    occurred_at      TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (client_order_id) REFERENCES execution_orders (client_order_id)
);

-- `fill_id` PRIMARY KEY makes fill ingestion idempotent: replaying the venue's
-- fill list converges rather than double-counting.
CREATE TABLE IF NOT EXISTS execution_fills (
    fill_id          TEXT PRIMARY KEY,
    client_order_id  TEXT,
    exchange_order_id TEXT,
    ticker           TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    action           TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    price_cents      INTEGER NOT NULL,
    fee_usd          TEXT,
    filled_at        TEXT,
    recorded_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_exec_fills_order ON execution_fills (client_order_id);

CREATE TABLE IF NOT EXISTS blocking_discrepancies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raised_at     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    ticker        TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    cleared_at    TEXT
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    client_order_id: str
    intent_id: str
    mode: str
    ticker: str
    outcome: str
    action: str
    requested_price_cents: int
    requested_quantity: int
    status: OrderStatus
    exchange_order_id: str | None
    filled_quantity: int
    average_fill_price_cents: Decimal | None
    fees_usd: Decimal | None
    created_at: str
    submitted_at: str | None
    last_reconciled_at: str | None
    error: str

    @property
    def remaining_quantity(self) -> int:
        return max(self.requested_quantity - self.filled_quantity, 0)

    @property
    def is_open(self) -> bool:
        from .models import OPEN_STATUSES

        return self.status in OPEN_STATUSES


class ExecutionLedger:
    """Durable local record of every intent, order, transition and fill."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_execution_db_path()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self._path)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            # WAL so a reconciliation read never blocks an order write. This is
            # its own file, so the setting cannot affect pmvl.db.
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA busy_timeout = 5000")
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO execution_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            yield con
            con.commit()
        finally:
            con.close()

    # ---------------------------------------------------------------- writes
    def record_intent(
        self,
        con: sqlite3.Connection,
        intent: OrderIntent,
        client_order_id: str,
        *,
        status: OrderStatus = OrderStatus.INTENDED,
        error: str = "",
    ) -> bool:
        """Persist an intent. Returns False if this logical order already exists.

        The ``INSERT OR IGNORE`` plus the ``client_order_id`` primary key is the
        local duplicate guard: a second call for the same logical order changes
        nothing and reports that it changed nothing.
        """
        now = _iso(utcnow())
        cur = con.execute(
            """
            INSERT OR IGNORE INTO execution_orders (
                client_order_id, intent_id, mode, ticker, outcome, action,
                requested_price_cents, requested_quantity, status,
                strategy, strategy_version, decision_epoch, source_signal_id,
                revision, created_at, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                client_order_id,
                intent.intent_id,
                intent.mode.value,
                intent.ticker,
                intent.outcome.value,
                intent.action.value,
                intent.limit_price_cents,
                intent.quantity,
                status.value,
                intent.strategy,
                intent.strategy_version,
                intent.decision_epoch,
                intent.source_signal_id,
                intent.revision,
                now,
                error,
            ),
        )
        inserted = cur.rowcount > 0
        if inserted:
            con.execute(
                """
                INSERT INTO execution_transitions
                    (client_order_id, from_status, to_status, occurred_at, note)
                VALUES (?,?,?,?,?)
                """,
                (client_order_id, "", status.value, now, "intent recorded"),
            )
        return inserted

    def transition(
        self,
        con: sqlite3.Connection,
        client_order_id: str,
        new_status: OrderStatus,
        *,
        exchange_order_id: str | None = None,
        error: str = "",
        note: str = "",
        submitted: bool = False,
    ) -> None:
        """Move an order to a new status, refusing transitions the machine forbids."""
        row = con.execute(
            "SELECT status FROM execution_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown client_order_id {client_order_id}")

        old = OrderStatus(row["status"])
        if not is_valid_transition(old, new_status):
            raise InvalidTransition(
                f"{client_order_id}: {old.value} -> {new_status.value} "
                "is not a permitted transition"
            )

        now = _iso(utcnow())
        sets = ["status = ?"]
        params: list[Any] = [new_status.value]
        if exchange_order_id is not None:
            sets.append("exchange_order_id = ?")
            params.append(exchange_order_id)
        if error:
            sets.append("error = ?")
            params.append(error)
        if submitted:
            sets.append("submitted_at = ?")
            params.append(now)
        params.append(client_order_id)

        con.execute(
            f"UPDATE execution_orders SET {', '.join(sets)} WHERE client_order_id = ?",
            params,
        )
        if old != new_status:
            con.execute(
                """
                INSERT INTO execution_transitions
                    (client_order_id, from_status, to_status, occurred_at, note)
                VALUES (?,?,?,?,?)
                """,
                (client_order_id, old.value, new_status.value, now, note),
            )

    def record_fill(
        self,
        con: sqlite3.Connection,
        fill: Fill,
        *,
        client_order_id: str | None = None,
    ) -> bool:
        """Record one fill. Idempotent by ``fill_id``.

        Returns True when this fill was new. Recomputes the order's filled
        quantity and average price from the fills table rather than incrementing,
        so a replay cannot drift.
        """
        now = _iso(utcnow())
        cur = con.execute(
            """
            INSERT OR IGNORE INTO execution_fills (
                fill_id, client_order_id, exchange_order_id, ticker, outcome,
                action, quantity, price_cents, fee_usd, filled_at, recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                fill.fill_id,
                client_order_id,
                fill.exchange_order_id,
                fill.ticker,
                fill.outcome.value,
                fill.action.value,
                fill.quantity,
                fill.price_cents,
                _text(fill.fee_usd),
                _iso(fill.filled_at),
                now,
            ),
        )
        is_new = cur.rowcount > 0
        if client_order_id:
            self._recompute_fill_state(con, client_order_id)
        return is_new

    def _recompute_fill_state(
        self, con: sqlite3.Connection, client_order_id: str
    ) -> None:
        rows = con.execute(
            """
            SELECT quantity, price_cents, fee_usd FROM execution_fills
            WHERE client_order_id = ?
            """,
            (client_order_id,),
        ).fetchall()
        total_qty = sum(int(r["quantity"]) for r in rows)
        if total_qty <= 0:
            return
        notional = sum(int(r["quantity"]) * int(r["price_cents"]) for r in rows)
        avg = Decimal(notional) / Decimal(total_qty)

        fees_present = [r["fee_usd"] for r in rows if r["fee_usd"] not in (None, "")]
        # If any fill did not report a fee, total fees are unknown. Summing the
        # ones that did would understate cost, so it stays NULL.
        fees = (
            str(sum((Decimal(f) for f in fees_present), start=Decimal("0")))
            if len(fees_present) == len(rows) and rows
            else None
        )

        con.execute(
            """
            UPDATE execution_orders
               SET filled_quantity = ?, average_fill_price_cents = ?, fees_usd = ?
             WHERE client_order_id = ?
            """,
            (total_qty, str(avg), fees, client_order_id),
        )

    def mark_reconciled(
        self, con: sqlite3.Connection, client_order_id: str
    ) -> None:
        con.execute(
            "UPDATE execution_orders SET last_reconciled_at = ? WHERE client_order_id = ?",
            (_iso(utcnow()), client_order_id),
        )

    def record_reconciliation(
        self, con: sqlite3.Connection, mode: str, summary: str
    ) -> None:
        con.execute(
            "INSERT INTO reconciliation_runs (ran_at, mode, summary) VALUES (?,?,?)",
            (_iso(utcnow()), mode, summary),
        )

    # ----------------------------------------------------------------- reads
    def get(
        self, con: sqlite3.Connection, client_order_id: str
    ) -> LedgerRecord | None:
        row = con.execute(
            "SELECT * FROM execution_orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return _record(row) if row else None

    def open_orders(self, con: sqlite3.Connection) -> list[LedgerRecord]:
        from .models import OPEN_STATUSES

        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        rows = con.execute(
            f"SELECT * FROM execution_orders WHERE status IN ({placeholders})",
            tuple(s.value for s in OPEN_STATUSES),
        ).fetchall()
        return [_record(r) for r in rows]

    def unresolved_orders(self, con: sqlite3.Connection) -> list[LedgerRecord]:
        rows = con.execute(
            "SELECT * FROM execution_orders WHERE status = ?",
            (OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED.value,),
        ).fetchall()
        return [_record(r) for r in rows]

    def spend_today_usd(self, con: sqlite3.Connection, *, today: str) -> Decimal:
        """Cash committed to orders submitted today, from the ledger itself.

        The venue has no "what did this strategy spend today" endpoint, and the
        daily-spend cap is worthless if nothing measures it. The ledger is the
        right source: it records every order this system submitted, which is
        exactly the population the cap is about.
        """
        rows = con.execute(
            "SELECT requested_price_cents, requested_quantity, filled_quantity "
            "FROM execution_orders WHERE submitted_at IS NOT NULL "
            "AND substr(submitted_at, 1, 10) = ?",
            (today,),
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            price = row["requested_price_cents"]
            if price is None:
                continue
            # Count what was actually committed: filled contracts if any have
            # filled, otherwise the full requested size still working.
            qty = row["filled_quantity"] or row["requested_quantity"] or 0
            total += (Decimal(price) / Decimal(100)) * Decimal(qty)
        return total

    def record_blocking_discrepancy(
        self,
        con: sqlite3.Connection,
        *,
        kind: str,
        ticker: str = "",
        detail: str = "",
    ) -> None:
        """Persist a discrepancy that has no local order row to attach to.

        REMOTE_ONLY and POSITION_MISMATCH are exactly the two kinds that mean
        "the venue holds exposure this system does not know about", and they are
        the two with nothing local to mark. Without a durable home they lived
        only on an in-memory report that the CLI printed and discarded, so the
        risk gate never saw them and the next order sailed past a ghost position.
        """
        con.execute(
            "INSERT INTO blocking_discrepancies (raised_at, kind, ticker, detail) "
            "VALUES (?,?,?,?)",
            (_iso(utcnow()), kind, ticker, detail[:500]),
        )

    def open_blocking_discrepancies(
        self, con: sqlite3.Connection
    ) -> list[sqlite3.Row]:
        return con.execute(
            "SELECT * FROM blocking_discrepancies WHERE cleared_at IS NULL "
            "ORDER BY raised_at"
        ).fetchall()

    def clear_blocking_discrepancies(self, con: sqlite3.Connection) -> int:
        cur = con.execute(
            "UPDATE blocking_discrepancies SET cleared_at = ? "
            "WHERE cleared_at IS NULL",
            (_iso(utcnow()),),
        )
        return cur.rowcount or 0

    def all_orders(self, con: sqlite3.Connection) -> list[LedgerRecord]:
        rows = con.execute(
            "SELECT * FROM execution_orders ORDER BY created_at"
        ).fetchall()
        return [_record(r) for r in rows]

    def fills_for(
        self, con: sqlite3.Connection, client_order_id: str
    ) -> list[sqlite3.Row]:
        return con.execute(
            "SELECT * FROM execution_fills WHERE client_order_id = ? ORDER BY recorded_at",
            (client_order_id,),
        ).fetchall()

    def transitions_for(
        self, con: sqlite3.Connection, client_order_id: str
    ) -> Sequence[sqlite3.Row]:
        return con.execute(
            "SELECT * FROM execution_transitions WHERE client_order_id = ? ORDER BY id",
            (client_order_id,),
        ).fetchall()


def _record(row: sqlite3.Row) -> LedgerRecord:
    return LedgerRecord(
        client_order_id=row["client_order_id"],
        intent_id=row["intent_id"],
        mode=row["mode"],
        ticker=row["ticker"],
        outcome=row["outcome"],
        action=row["action"],
        requested_price_cents=int(row["requested_price_cents"]),
        requested_quantity=int(row["requested_quantity"]),
        status=OrderStatus(row["status"]),
        exchange_order_id=row["exchange_order_id"],
        filled_quantity=int(row["filled_quantity"] or 0),
        average_fill_price_cents=(
            Decimal(row["average_fill_price_cents"])
            if row["average_fill_price_cents"]
            else None
        ),
        fees_usd=Decimal(row["fees_usd"]) if row["fees_usd"] else None,
        created_at=row["created_at"],
        submitted_at=row["submitted_at"],
        last_reconciled_at=row["last_reconciled_at"],
        error=row["error"] or "",
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
