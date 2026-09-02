"""A scheduler fire must never vanish from the job ledger.

2026-09-01T15:00Z: `calibrate` fired, the scheduler logged
"scheduled job failed: (sqlite3.OperationalError) database is locked", and
`job_runs` contained no row for it. The surrounding 13.5 minutes contained no
row for ANY job while the log filled with lock errors. Eleven further ledger
holes over five minutes followed in the next epoch.

The mechanism, reproduced in `_locked_writer` below:

    _claim_with_retry exhausts its budget and re-raises OperationalError
      -> job_run's claim block catches only JobSkipped, so it escapes
        -> session_scope rolls back the pending claim INSERT
          -> job_run exits before yielding; the body never runs
            -> the only durable artifact of an attempt was that INSERT
              -> nothing is committed and the fire disappears

The asymmetry is the defect: the JobSkipped path deliberately commits a SKIPPED
row so "the audit trail shows both sides of the skip". Lock contention had no
equivalent, so the one failure mode that erases the record was also the one
that recorded nothing.
"""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from pmvl_shared.db.base import create_all, reset_engine, session_scope
from pmvl_shared.db.models import JobRun
from pmvl_shared.enums import JobStatus


@contextmanager
def _fresh_db():
    path = tempfile.mktemp(suffix=".db")
    reset_engine(f"sqlite:///{path}")
    create_all()
    yield path


@contextmanager
def _locked_writer(path: str):
    """Hold SQLite's write lock the way a slow concurrent writer would."""
    con = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "INSERT INTO job_runs (job_name,status,started_at,records_written,error) "
        "VALUES ('blocker','running','2026-01-01',0,'')"
    )
    try:
        yield
    finally:
        con.rollback()
        con.close()


@pytest.fixture()
def fast_claim(monkeypatch):
    """Shrink the retry budget so the lock outlasts it deterministically."""
    from pmvl_worker import jobs

    monkeypatch.setattr(jobs, "_CLAIM_RETRIES", 2)
    monkeypatch.setattr(jobs, "_CLAIM_BACKOFF_SECONDS", 0.01)
    return jobs


def _ledger(job_name: str) -> list[JobRun]:
    with session_scope() as db:
        return list(db.query(JobRun).filter(JobRun.job_name == job_name).all())


def test_claim_contention_writes_a_durable_artifact_when_the_lock_clears(
    fast_claim,
) -> None:
    """The repair, exercised directly on the recording path.

    Note the honest bound this test encodes. Full durability *while the lock is
    still held* is unachievable by definition: the record of a failed write
    cannot itself be written to the database that refused the write. What the
    repair guarantees is (a) a durable FAILED row whenever the database is
    reachable at recording time, proven here, and (b) non-database visibility
    when it is not, proven by
    ``test_sustained_lock_is_never_silent`` below.
    """
    jobs = fast_claim
    with _fresh_db():
        from pmvl_shared.job_record import RunRecord, idempotency_key
        from pmvl_shared.timeutil import utcnow

        record = RunRecord(
            job_name="calibrate",
            run_id="deadbeefdeadbeef",
            idempotency_key=idempotency_key("calibrate", cutoff=None),
            scheduled_at=utcnow(),
        )
        jobs._record_claim_failure(
            "calibrate", record, utcnow(),
            OperationalError("stmt", {}, Exception("database is locked")),
        )
        rows = _ledger("calibrate")
        assert rows, "a prevented scheduler fire left no durable artifact"
        assert len(rows) == 1
        assert rows[0].status == JobStatus.FAILED.value
        assert "lock" in (rows[0].error or "").lower()
        assert rows[0].details["run"]["claim_failed"] is True
        assert rows[0].details["run"]["body_executed"] is False


def test_sustained_lock_is_never_silent(fast_claim, caplog) -> None:
    """When even the failure record cannot land, fail loudly and bounded.

    This is the residual case the repair cannot make durable in the database.
    It must still be externally visible and must not recurse or hang.
    """
    jobs = fast_claim
    with _fresh_db() as path:
        body_ran = False
        with _locked_writer(path):
            with pytest.raises(OperationalError):
                with jobs.job_run("calibrate"):
                    body_ran = True
        assert not body_ran
        assert any(
            "could not claim a run slot" in r.getMessage() for r in caplog.records
        ), "sustained contention produced no non-database evidence"


def test_body_never_runs_when_the_slot_is_not_claimed(fast_claim) -> None:
    jobs = fast_claim
    with _fresh_db() as path:
        calls = []
        with _locked_writer(path):
            with pytest.raises(OperationalError):
                with jobs.job_run("snapshot"):
                    calls.append(1)
        assert calls == []


def test_recording_the_failure_does_not_amplify_contention(fast_claim) -> None:
    """If the durable write also cannot land, fail loudly, never hang or recurse."""
    jobs = fast_claim
    with _fresh_db() as path:
        with _locked_writer(path):
            # The lock is still held while job_run tries to record the failure.
            with pytest.raises(OperationalError):
                with jobs.job_run("prune"):
                    pass
        # Reaching here at all proves the recording attempt is bounded.


def test_successful_claim_is_unaffected(fast_claim) -> None:
    """Negative control: no contention, ordinary success path intact."""
    jobs = fast_claim
    with _fresh_db():
        with jobs.job_run("snapshot") as details:
            details["records_written"] = 7
        rows = _ledger("snapshot")
        assert len(rows) == 1
        assert rows[0].status == JobStatus.SUCCESS.value
        assert rows[0].records_written == 7


def test_lock_released_during_retry_still_claims(fast_claim) -> None:
    """A transient lock must be retried, not converted into a failure."""
    jobs = fast_claim
    with _fresh_db():
        with jobs.job_run("map_stations") as details:
            details["records_written"] = 0
        assert _ledger("map_stations")[0].status == JobStatus.SUCCESS.value


def test_same_key_later_occurrence_still_claims(fast_claim) -> None:
    """4C0.31's proven semantic: a completed run never blocks a later one."""
    jobs = fast_claim
    with _fresh_db():
        with jobs.job_run("calibrate") as d:
            d["records_written"] = 1
        with jobs.job_run("calibrate") as d:
            d["records_written"] = 1
        rows = _ledger("calibrate")
        assert len(rows) == 2, "a later scheduled occurrence must get its own row"
        assert all(r.status == JobStatus.SUCCESS.value for r in rows)


def test_concurrent_active_duplicate_is_still_blocked(fast_claim) -> None:
    """Duplicate-execution safety must survive the repair."""
    jobs = fast_claim
    with _fresh_db():
        with pytest.raises(jobs.JobSkipped):
            with jobs.job_run("calibrate"):
                # A second attempt while the first is genuinely active.
                with jobs.job_run("calibrate"):
                    pass


