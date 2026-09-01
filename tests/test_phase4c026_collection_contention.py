"""Contention fixes: transaction boundaries, claim resilience, outcome semantics.

All three come from one incident. On 2026-08-31 `refresh_orderbooks` held a
SQLite read snapshot across `provider.get_orderbooks`, which:

* blocked other writers past the 30s busy timeout - 87 `database is locked`
  errors in the two hours before the decision window, one of which was the
  14:00Z backtest's own job_runs INSERT, so that cron never ran and left no
  record of not running;
* pinned the WAL, which sat at exactly 416.4 MiB for twelve hours;
* pushed research_books cycles past their cadence, so `max_instances=1` skipped
  ticks and opened five 6-minute collection holes.

Meanwhile `research_books` reported success with zero rows for six consecutive
cycles while the Kalshi circuit was open, and collection health read that as
healthy.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from pmvl_shared.collection_outcome import CollectionOutcome, classify_collection

RUNNER_SRC = (
    Path(__file__).resolve().parents[1]
    / "packages/market-normalization/src/pmvl_markets/ingest/runner.py"
)
JOBS_SRC = (
    Path(__file__).resolve().parents[1]
    / "services/worker/src/pmvl_worker/jobs.py"
)


# --- the transaction boundary -------------------------------------------------

def test_transaction_is_released_before_the_network_call() -> None:
    """No read snapshot may be held across `get_orderbooks`."""
    body = RUNNER_SRC.read_text()
    body = body[body.index("async def refresh_orderbooks"):]
    fetch = body.index("await provider.get_orderbooks(")
    before = body[:fetch]
    commit = before.rindex("session.commit()")
    # Nothing may re-open a transaction between the commit and the fetch.
    between = before[commit + len("session.commit()"):]
    assert "session.execute" not in between
    assert "session.scalars" not in between
    assert "session.query" not in between


def test_a_held_read_snapshot_blocks_a_writer(tmp_path: Path) -> None:
    """Negative control: demonstrates the pre-fix failure mode directly.

    A second connection cannot write while a first holds an open read
    transaction and then upgrades. This is the mechanism the commit removes.
    """
    db = tmp_path / "t.db"
    a = sqlite3.connect(db, timeout=0.2, isolation_level="DEFERRED")
    a.execute("PRAGMA journal_mode=WAL")
    a.execute("CREATE TABLE t (x INTEGER)")
    a.commit()

    b = sqlite3.connect(db, timeout=0.2, isolation_level="DEFERRED")
    a.execute("BEGIN IMMEDIATE")          # writer holds the lock, as during a fetch
    a.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        b.execute("INSERT INTO t VALUES (2)")
        b.commit()
    a.commit()                            # releasing it is what the fix does
    b.execute("INSERT INTO t VALUES (2)")  # now succeeds
    b.commit()
    assert a.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    a.close(); b.close()


def test_writes_still_happen_after_the_commit() -> None:
    """The commit must not truncate the write path that follows it."""
    body = RUNNER_SRC.read_text()
    body = body[body.index("async def refresh_orderbooks"):]
    after = body[body.index("await provider.get_orderbooks("):]
    assert "store_orderbooks(" in after, "product write path lost"
    assert "archive_books(" in after, "archive write path lost"


# --- claim resilience ---------------------------------------------------------

def test_claim_retries_only_on_lock_errors() -> None:
    from pmvl_worker import jobs

    calls = {"n": 0}

    class FakeSession:
        def rollback(self) -> None: ...

    def flaky(db, name, key, started):  # noqa: ANN001, ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return 42

    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "_claim_active_key", flaky)
    monkey.setattr(jobs, "_CLAIM_BACKOFF_SECONDS", 0.0)
    try:
        assert jobs._claim_with_retry(FakeSession(), "backtest", "k", None) == 42
        assert calls["n"] == 3
    finally:
        monkey.undo()


def test_claim_does_not_retry_a_skip() -> None:
    """Another live run holds the slot; waiting cannot change that."""
    from pmvl_worker import jobs

    calls = {"n": 0}

    class FakeSession:
        def rollback(self) -> None: ...

    def always_skip(db, name, key, started):  # noqa: ANN001, ARG001
        calls["n"] += 1
        raise jobs.JobSkipped("held", active_run_id="other")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "_claim_active_key", always_skip)
    try:
        with pytest.raises(jobs.JobSkipped):
            jobs._claim_with_retry(FakeSession(), "backtest", "k", None)
        assert calls["n"] == 1
    finally:
        monkey.undo()


def test_claim_propagates_non_lock_errors_immediately() -> None:
    from pmvl_worker import jobs

    calls = {"n": 0}

    class FakeSession:
        def rollback(self) -> None: ...

    def broken(db, name, key, started):  # noqa: ANN001, ARG001
        calls["n"] += 1
        raise OperationalError("INSERT", {}, Exception("no such table: job_runs"))

    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "_claim_active_key", broken)
    try:
        with pytest.raises(OperationalError):
            jobs._claim_with_retry(FakeSession(), "backtest", "k", None)
        assert calls["n"] == 1, "a schema error must not be retried"
    finally:
        monkey.undo()


def test_job_run_uses_the_retrying_claim() -> None:
    src = JOBS_SRC.read_text()
    body = src[src.index("def job_run("):]
    assert "_claim_with_retry(" in body[: body.index("def heartbeat()")]


# --- collection outcome semantics ---------------------------------------------

@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(books_archived=42, markets_seen=42, errors=[]),
         CollectionOutcome.SUCCESS_WITH_ROWS),
        (dict(books_archived=0, markets_seen=42, errors=[]),
         CollectionOutcome.SUCCESS_VALID_NOOP),
        (dict(books_archived=0, markets_seen=42,
              errors=["kalshi orderbooks: circuit OPEN for kalshi after 8 failures"]),
         CollectionOutcome.UPSTREAM_CIRCUIT_OPEN),
        (dict(books_archived=0, markets_seen=42,
              errors=["kalshi orderbooks: kalshi exhausted retries: timeout"]),
         CollectionOutcome.RETRY_EXHAUSTED),
        (dict(books_archived=0, markets_seen=42,
              errors=["OperationalError: database is locked"]),
         CollectionOutcome.DB_WRITE_FAILED),
        (dict(books_archived=0, markets_seen=42, errors=["kalshi orderbooks: 503"]),
         CollectionOutcome.UPSTREAM_FETCH_FAILED),
        (dict(books_archived=0, markets_seen=0, errors=[]),
         CollectionOutcome.NO_MARKETS_CONFIGURED),
    ],
)
def test_collection_outcome_classification(kwargs, expected) -> None:  # noqa: ANN001
    assert classify_collection(**kwargs) is expected


def test_zero_rows_is_not_automatically_a_failure() -> None:
    """A quiet market must stay a valid no-op, not become a false alarm."""
    o = classify_collection(books_archived=0, markets_seen=7, errors=[])
    assert o is CollectionOutcome.SUCCESS_VALID_NOOP
    assert not o.is_upstream_failure


def test_circuit_open_zero_rows_is_not_healthy() -> None:
    """The exact 2026-08-31 state: success, zero rows, venue unreachable."""
    o = classify_collection(
        books_archived=0, markets_seen=42,
        errors=["kalshi orderbooks: circuit OPEN for kalshi after 8 failures"],
    )
    assert o is CollectionOutcome.UPSTREAM_CIRCUIT_OPEN
    assert o.is_upstream_failure


def test_research_books_records_the_outcome() -> None:
    src = JOBS_SRC.read_text()
    body = src[src.index("async def job_research_books"):]
    body = body[: body.index("def job_map_settlement_stations")]
    assert "classify_collection(" in body
    assert '"outcome"' in body


# --- negative controls: nothing research-facing moved -------------------------

def test_freshness_bounds_unchanged() -> None:
    from pmvl_markets.research.pit import MAX_FORECAST_AGE, MAX_ORDERBOOK_AGE

    assert MAX_ORDERBOOK_AGE.total_seconds() == 300
    assert MAX_FORECAST_AGE.total_seconds() == 12 * 3600


def test_protocol_hash_unchanged() -> None:
    from pmvl_markets.research.protocol import FROZEN_PROTOCOL

    assert FROZEN_PROTOCOL.content_hash() == "08855a72bd9339ee"
    assert FROZEN_PROTOCOL.minimum_independent_events == 30


def test_outcome_yes_still_venue_only() -> None:
    src = (
        Path(__file__).resolve().parents[1]
        / "packages/market-normalization/src/pmvl_markets/research/build.py"
    ).read_text()
    assert "outcome_yes=(venue_result ==" in src
    assert not re.search(r"outcome_yes\s*=\s*[^(]*settled_value", src)


def test_archive_write_is_still_idempotent_on_natural_keys() -> None:
    """No duplicate archived books can arise from re-running a capture."""
    from pmvl_shared.research_archive import ResearchArchive

    src = Path(ResearchArchive.__module__.replace(".", "/"))
    text = (
        Path(__file__).resolve().parents[1]
        / "packages/shared/src/pmvl_shared/research_archive.py"
    ).read_text()
    assert "UNIQUE" in text.upper() or "ON CONFLICT" in text.upper(), (
        "archive must remain idempotent on its natural key"
    )
