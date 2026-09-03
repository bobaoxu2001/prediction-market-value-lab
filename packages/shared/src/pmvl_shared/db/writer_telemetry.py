"""Observe SQLite writer transactions without touching the database.

Implements Contract v2 - see docs/sqlite-writer-instrumentation-contract.md.

The point of this module is to replace "the source code strongly suggests ingest
holds SQLite open too long" with a measured statement. It answers, per natural
transaction: when did the first write actually return, and how long after that
was a commit requested?

Three things it deliberately does NOT do.

It does not observe commit *completion*. SQLAlchemy 2.0.44 fires
``ConnectionEvents.commit`` before the DBAPI commit runs - proven in
``test_engine_commit_event_precedes_dbapi_commit`` - so every terminal timestamp
here is a *request* boundary. Metric names say ``precommit`` for that reason.

It does not observe SQLite's write lock. The first DML's elapsed time bounds the
lock wait but also contains statement execution and driver overhead, so it is
named ``first_dml_wait_seconds`` and never ``lock_wait``.

It does not write to SQLite. Telemetry that contended for the lock it is
measuring would corrupt its own result, so records go to the logging path only.
"""

from __future__ import annotations

import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

from ..logging_setup import get_logger

log = get_logger(__name__)

#: Set by the job wrapper. Attribution is never inferred from statement text or
#: runtime length - a long transaction is not evidence of which job ran it.
current_writer_path: ContextVar[str] = ContextVar("pmvl_writer_path", default="unknown")

#: Key under which per-transaction state lives on the SQLAlchemy Connection.
_STATE = "pmvl_writer_txn"

#: Leading whitespace, /* */ blocks and -- line comments, then the verb.
#: SQLAlchemy emits its own comments and pretty-printing, so the verb is not
#: reliably at offset zero.
_DML = re.compile(
    r"^(?:\s|/\*.*?\*/|--[^\n]*\n)*(INSERT|UPDATE|DELETE|REPLACE)\b",
    re.IGNORECASE | re.DOTALL,
)


def _is_dml(statement: str) -> bool:
    return bool(statement) and _DML.match(statement) is not None


def _now() -> tuple[float, str]:
    """Monotonic for arithmetic, UTC wall clock for correlation only."""
    return time.monotonic(), datetime.now(timezone.utc).isoformat()


def _fresh(conn: Any) -> dict[str, Any]:
    mono, wall = _now()
    state = {
        "transaction_id": uuid.uuid4().hex[:16],
        "writer_path": current_writer_path.get(),
        "t_txn_begin": mono,
        "txn_begin_utc": wall,
        "t_first_dml_start": None,
        "t_first_dml_success": None,
        "first_dml_verb": None,
        "dml_pending": False,
    }
    conn.info[_STATE] = state
    return state


def _emit(state: dict[str, Any], terminal: str, mono: float) -> None:
    """One structured record per observed write transaction.

    Read-only transactions emit nothing: with no first DML there is no write
    window to describe, and inventing zeros would put non-writers into the
    distribution.
    """
    if state.get("t_first_dml_start") is None:
        return

    start = state["t_first_dml_start"]
    ok = state.get("t_first_dml_success")

    record: dict[str, Any] = {
        "event": "sqlite_writer_transaction",
        "transaction_id": state["transaction_id"],
        "writer_path": state["writer_path"],
        "txn_begin_utc": state["txn_begin_utc"],
        "first_dml_verb": state["first_dml_verb"],
        "terminal": terminal,
        "first_dml_succeeded": ok is not None,
    }
    if ok is not None:
        record["first_dml_wait_seconds"] = round(ok - start, 6)
        record["confirmed_precommit_write_seconds"] = round(mono - ok, 6)
    record["precommit_write_window_upper_bound_seconds"] = round(mono - start, 6)
    log.info("writer_txn %s", record)


def _clear(conn: Any) -> dict[str, Any] | None:
    """Pop state so a pooled connection cannot carry it into the next user."""
    return conn.info.pop(_STATE, None)


def install(engine: Engine) -> None:
    """Attach the listeners. Idempotent; safe to call once per engine."""
    if getattr(engine, "_pmvl_writer_telemetry", False):
        return
    engine._pmvl_writer_telemetry = True  # type: ignore[attr-defined]

    @event.listens_for(engine, "begin")
    def _begin(conn: Any) -> None:  # noqa: ANN401
        try:
            _fresh(conn)
        except Exception:  # noqa: BLE001
            pass

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        try:
            state = conn.info.get(_STATE)
            # An implicit-begin path can execute before the begin event lands.
            if state is None:
                state = _fresh(conn)
            if state["t_first_dml_start"] is not None or not _is_dml(statement):
                return
            state["t_first_dml_start"] = time.monotonic()
            state["first_dml_verb"] = _DML.match(statement).group(1).upper()
            state["dml_pending"] = True
        except Exception:  # noqa: BLE001
            pass

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        try:
            state = conn.info.get(_STATE)
            if state is None or not state.get("dml_pending"):
                return
            state["t_first_dml_success"] = time.monotonic()
            state["dml_pending"] = False
        except Exception:  # noqa: BLE001
            pass

    @event.listens_for(engine, "commit")
    def _commit(conn: Any) -> None:  # noqa: ANN401
        try:
            state = _clear(conn)
            if state is not None:
                _emit(state, "COMMIT_REQUESTED", time.monotonic())
        except Exception:  # noqa: BLE001
            pass

    @event.listens_for(engine, "rollback")
    def _rollback(conn: Any) -> None:  # noqa: ANN401
        try:
            state = _clear(conn)
            if state is not None:
                _emit(state, "ROLLBACK_REQUESTED", time.monotonic())
        except Exception:  # noqa: BLE001
            pass
