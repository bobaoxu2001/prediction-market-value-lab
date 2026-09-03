"""Contract v2 for SQLite writer telemetry.

See docs/sqlite-writer-instrumentation-contract.md. The tests that matter most
here are the negative ones: v1 died because it specified a timestamp the stack
cannot produce, so these pin down what is actually observable and forbid the
vocabulary that would quietly re-introduce the fiction.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, text

from pmvl_shared.db import writer_telemetry as wt

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def eng():
    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}")
    md = MetaData()
    Table("t", md, Column("x", Integer), Column("s", String))
    md.create_all(engine)
    wt.install(engine)
    engine._pmvl_path = path  # type: ignore[attr-defined]
    return engine


@pytest.fixture()
def records(caplog):
    caplog.set_level(logging.INFO, logger="pmvl_shared.db.writer_telemetry")

    def parse() -> list[dict]:
        out = []
        for r in caplog.records:
            m = re.search(r"writer_txn (\{.*\})", r.getMessage())
            if m:
                out.append(eval(m.group(1)))  # noqa: S307 - our own literal dict
        return out

    return parse


def test_select_only_transaction_emits_nothing(eng, records) -> None:
    with eng.begin() as c:
        c.execute(text("SELECT COUNT(*) FROM t"))
    assert records() == [], "a read-only transaction has no write window to report"


def test_insert_emits_one_record(eng, records) -> None:
    with eng.begin() as c:
        c.execute(text("INSERT INTO t (x) VALUES (1)"))
    rs = records()
    assert len(rs) == 1
    r = rs[0]
    assert r["event"] == "sqlite_writer_transaction"
    assert r["first_dml_verb"] == "INSERT"
    assert r["terminal"] == "COMMIT_REQUESTED"
    assert r["first_dml_succeeded"] is True


@pytest.mark.parametrize(
    "stmt,verb",
    [
        ("INSERT INTO t (x) VALUES (1)", "INSERT"),
        ("UPDATE t SET x = 2", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("REPLACE INTO t (x) VALUES (3)", "REPLACE"),
    ],
)
def test_all_dml_verbs_detected(eng, records, stmt, verb) -> None:
    with eng.begin() as c:
        c.execute(text(stmt))
    assert records()[0]["first_dml_verb"] == verb


def test_select_before_insert_does_not_start_the_window(eng, records) -> None:
    with eng.begin() as c:
        c.execute(text("SELECT COUNT(*) FROM t"))
        time.sleep(0.05)
        c.execute(text("INSERT INTO t (x) VALUES (1)"))
    r = records()[0]
    assert r["first_dml_verb"] == "INSERT"
    assert r["precommit_write_window_upper_bound_seconds"] < 0.05, (
        "the leading SELECT was counted into the write window"
    )


def test_only_the_first_dml_defines_the_window(eng, records) -> None:
    with eng.begin() as c:
        c.execute(text("INSERT INTO t (x) VALUES (1)"))
        time.sleep(0.12)
        c.execute(text("INSERT INTO t (x) VALUES (2)"))
        c.execute(text("UPDATE t SET x = 9"))
    rs = records()
    assert len(rs) == 1, "one record per transaction, not per statement"
    assert rs[0]["confirmed_precommit_write_seconds"] >= 0.12, (
        "a later write reset the first-DML timestamps"
    )


def test_leading_comment_and_whitespace_still_detected(eng, records) -> None:
    with eng.begin() as c:
        c.execute(text("  /* hint */\n  INSERT INTO t (x) VALUES (1)"))
    assert records()[0]["first_dml_verb"] == "INSERT"


def test_rollback_is_classified_and_not_a_commit(eng, records) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any failure rolls back
        with eng.begin() as c:
            c.execute(text("INSERT INTO t (x) VALUES (1)"))
            raise RuntimeError("boom")
    rs = records()
    assert len(rs) == 1
    assert rs[0]["terminal"] == "ROLLBACK_REQUESTED"


def test_failed_first_dml_reports_no_success(eng, records) -> None:
    with pytest.raises(Exception):  # noqa: B017
        with eng.begin() as c:
            c.execute(text("INSERT INTO nonexistent_table (x) VALUES (1)"))
    rs = records()
    assert len(rs) == 1
    assert rs[0]["first_dml_succeeded"] is False
    assert "first_dml_wait_seconds" not in rs[0], (
        "a statement that never returned must not report a success interval"
    )
    assert "confirmed_precommit_write_seconds" not in rs[0]


def test_sequential_transactions_get_distinct_identity(eng, records) -> None:
    for i in range(3):
        with eng.begin() as c:
            c.execute(text(f"INSERT INTO t (x) VALUES ({i})"))
    rs = records()
    assert len(rs) == 3
    assert len({r["transaction_id"] for r in rs}) == 3, "transaction ids collided"


def test_state_does_not_leak_across_pooled_reuse(eng, records) -> None:
    """A write transaction must not bleed into a later read-only one."""
    with eng.begin() as c:
        c.execute(text("INSERT INTO t (x) VALUES (1)"))
    before = len(records())
    with eng.begin() as c:  # same pooled connection, read only
        c.execute(text("SELECT COUNT(*) FROM t"))
    assert len(records()) == before, "read-only transaction inherited write state"


def test_writer_path_attribution(eng, records) -> None:
    token = wt.current_writer_path.set("ingest")
    try:
        with eng.begin() as c:
            c.execute(text("INSERT INTO t (x) VALUES (1)"))
    finally:
        wt.current_writer_path.reset(token)
    assert records()[0]["writer_path"] == "ingest"
    with eng.begin() as c:
        c.execute(text("INSERT INTO t (x) VALUES (2)"))
    assert records()[1]["writer_path"] == "unknown"


def test_v2_formulas_hold(eng, records) -> None:
    with eng.begin() as c:
        c.execute(text("INSERT INTO t (x) VALUES (1)"))
        time.sleep(0.15)
    r = records()[0]
    # upper bound = wait + confirmed, within float noise
    assert r["precommit_write_window_upper_bound_seconds"] == pytest.approx(
        r["first_dml_wait_seconds"] + r["confirmed_precommit_write_seconds"], abs=1e-3
    )
    assert r["confirmed_precommit_write_seconds"] >= 0.15


def test_telemetry_performs_no_sqlite_write(eng, records) -> None:
    """The measurement must not contend for the lock it measures."""
    with eng.begin() as c:
        c.execute(text("INSERT INTO t (x) VALUES (1)"))
    assert records()
    probe = sqlite3.connect(eng._pmvl_path)
    tables = {r[0] for r in probe.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    probe.close()
    assert tables == {"t"}, f"telemetry created a table: {tables - {'t'}}"


def test_telemetry_failure_does_not_break_the_transaction(eng, records, monkeypatch) -> None:
    """Fail open: a broken sink must not take down healthy production work."""
    def boom(*_a, **_k):
        raise RuntimeError("sink down")

    monkeypatch.setattr(wt.log, "info", boom)
    with eng.begin() as c:
        c.execute(text("INSERT INTO t (x) VALUES (42)"))
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM t WHERE x = 42")).scalar() == 1


def test_engine_commit_event_precedes_dbapi_commit() -> None:
    """The architectural fact that killed contract v1. Never let it regress.

    If this ever fails, the Engine `commit` event has become a post-commit hook
    and `t_commit_end` may be reconsidered. Until then every terminal timestamp
    in this module is a *request* boundary.
    """
    path = tempfile.mktemp(suffix=".db")
    e = create_engine(f"sqlite:///{path}")
    with e.begin() as c:
        c.execute(text("CREATE TABLE t (x INTEGER)"))
    seen = {}

    @event.listens_for(e, "commit")
    def _c(conn):  # noqa: ANN001
        probe = sqlite3.connect(path, timeout=0.2)
        try:
            seen["visible"] = probe.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            probe.close()

    with e.begin() as c:
        c.execute(text("INSERT INTO t VALUES (1)"))
    assert seen["visible"] == 0, (
        "Engine.commit now fires AFTER the DBAPI commit - contract v2's premise "
        "has changed and t_commit_end may be observable again"
    )


def test_no_prohibited_vocabulary_in_active_code() -> None:
    """Contract v2 forbids naming things the implementation cannot observe."""
    banned = [
        "exact_lock_duration", "lock_acquired_at", "lock_released_at",
        "commit_completed_at", "durable_at", "exact_lock_wait", "t_commit_end",
        "confirmed_write_txn_seconds", "write_txn_upper_bound_seconds",
    ]
    src = (ROOT / "packages/shared/src/pmvl_shared/db/writer_telemetry.py").read_text()
    hits = [b for b in banned if b in src]
    assert not hits, f"active instrumentation uses retired/false vocabulary: {hits}"
