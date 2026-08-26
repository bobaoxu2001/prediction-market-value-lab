"""The idempotency key must be a real database-enforced guarantee.

Before this, the key was computed and serialised into the details blob while
JobRun had no such column and nothing deduplicated on it - a documented
guarantee that did not exist. The key is now a column, and a partial unique
index allows at most one RUNNING row per key, so a scheduler and a manual CLI
cannot start "the same work" twice concurrently.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

from pmvl_shared.enums import JobStatus
from pmvl_shared.job_record import idempotency_key
from pmvl_shared.timeutil import utcnow

WORKER_SRC = Path(__file__).resolve().parents[1] / "services" / "worker" / "src"
if str(WORKER_SRC) not in sys.path:
    sys.path.insert(0, str(WORKER_SRC))

from pmvl_worker.jobs import (  # noqa: E402
    ACTIVE_RUN_STALE_AFTER,
    HEARTBEAT_STALE_AFTER,
    JobSkipped,
    job_run,
)

from pmvl_markets.db_models import JobRun  # noqa: E402


@pytest.fixture()
def key():  # noqa: ANN201
    return idempotency_key("ingest", cutoff=None, params={"market_limit": 10})


class TestKeyPersisted:
    def test_the_key_is_a_queryable_column(self, clean_db, key) -> None:  # noqa: ANN001
        with job_run("ingest", cutoff=None, params={"market_limit": 10}):
            pass

        row = clean_db.query(JobRun).filter_by(job_name="ingest").one()
        assert row.idempotency_key == key
        assert row.details["run"]["idempotency_key"] == key


class TestConcurrentDedup:
    def test_a_second_identical_run_is_skipped_and_its_body_never_runs(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        body_ran = False

        with job_run("ingest", cutoff=None, params={"market_limit": 10}):
            body_ran = True
            with pytest.raises(JobSkipped) as exc_info:
                job_run("ingest", cutoff=None, params={"market_limit": 10}).__enter__()

        assert body_ran is True
        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert [r.status for r in rows] == [JobStatus.SUCCESS.value, JobStatus.SKIPPED.value]
        skipped = rows[1]
        assert skipped.details["run"]["skipped"] is True
        assert skipped.details["run"]["skipped_because_active_run_id"] == rows[0].id
        assert skipped.idempotency_key == key
        assert "already active" in str(exc_info.value)

    def test_a_retry_after_the_first_run_finished_is_not_skipped(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        with job_run("ingest", cutoff=None, params={"market_limit": 10}):
            pass
        # Sequential rerun: the first run finished, so the key is free again.
        with job_run("ingest", cutoff=None, params={"market_limit": 10}):
            pass

        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert [r.status for r in rows] == [JobStatus.SUCCESS.value, JobStatus.SUCCESS.value]

    def test_different_parameters_are_different_work(self, clean_db, key) -> None:  # noqa: ANN001
        with job_run("ingest", cutoff=None, params={"market_limit": 10}):
            with job_run("ingest", cutoff=None, params={"market_limit": 500}):
                pass

        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert [r.status for r in rows] == [JobStatus.SUCCESS.value, JobStatus.SUCCESS.value]
        assert len({r.idempotency_key for r in rows}) == 2


class TestStaleRunningRecovery:
    def test_a_dead_running_row_is_superseded_not_skipped_forever(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """A process that died mid-run leaves status='running'. Without
        recovery, every future identical run would be skipped forever."""
        stale = JobRun(
            job_name="ingest",
            status=JobStatus.RUNNING.value,
            started_at=utcnow() - ACTIVE_RUN_STALE_AFTER - timedelta(minutes=1),
            idempotency_key=key,
        )
        clean_db.add(stale)
        clean_db.commit()

        with job_run("ingest", cutoff=None, params={"market_limit": 10}):
            pass

        clean_db.expire_all()
        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert rows[0].status == JobStatus.INTERRUPTED.value
        assert "superseded" in rows[0].error
        assert rows[1].status == JobStatus.SUCCESS.value

    def test_a_fresh_running_row_wins_over_a_stale_one(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        fresh = JobRun(
            job_name="ingest",
            status=JobStatus.RUNNING.value,
            started_at=utcnow(),
            idempotency_key=key,
        )
        clean_db.add(fresh)
        clean_db.commit()

        with pytest.raises(JobSkipped):
            job_run("ingest", cutoff=None, params={"market_limit": 10}).__enter__()

        clean_db.expire_all()
        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert rows[0].status == JobStatus.RUNNING.value
        assert rows[1].status == JobStatus.SKIPPED.value


class TestStatusSemantics:
    def test_skipped_and_interrupted_are_terminal_and_not_usable(self) -> None:
        assert JobStatus.SKIPPED.is_terminal is True
        assert JobStatus.SKIPPED.produced_usable_output is False
        assert JobStatus.INTERRUPTED.is_terminal is True
        assert JobStatus.INTERRUPTED.produced_usable_output is False


class TestHeartbeatSeparatesLivenessFromAge:
    """The regression that cost a live issuance window on 2026-08-25.

    ``research_books`` holds its slot for a whole window by design, so the
    two-hour age bound could not distinguish "still working" from "died forty
    minutes ago". A worker restarted mid-window left its row marked running and
    every later tick recorded itself as skipped against it. Collection would not
    have resumed until two hours after the crash - the rest of that window and
    most of the next - and only a manual database write cleared it.
    """

    def test_a_long_running_job_with_a_live_heartbeat_is_not_evicted(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """The reason an age bound alone cannot work.

        This run started longer ago than the age bound allows and is perfectly
        healthy - which is the normal state of a window collector an hour in.
        """
        alive = JobRun(
            job_name="ingest",
            status=JobStatus.RUNNING.value,
            started_at=utcnow() - ACTIVE_RUN_STALE_AFTER - timedelta(minutes=30),
            heartbeat_at=utcnow() - timedelta(seconds=30),
            idempotency_key=key,
        )
        clean_db.add(alive)
        clean_db.commit()

        with pytest.raises(JobSkipped):
            job_run("ingest", cutoff=None, params={"market_limit": 10}).__enter__()

        clean_db.expire_all()
        assert clean_db.query(JobRun).order_by(JobRun.id).first().status == (
            JobStatus.RUNNING.value
        )

    def test_a_dead_job_is_reclaimed_in_minutes_not_hours(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """The regression case, inverted: young by age, dead by heartbeat.

        Without the heartbeat this row is only four minutes old and would be
        treated as an active competitor for another two hours.
        """
        dead = JobRun(
            job_name="ingest",
            status=JobStatus.RUNNING.value,
            started_at=utcnow() - timedelta(minutes=4),
            heartbeat_at=utcnow() - HEARTBEAT_STALE_AFTER - timedelta(seconds=1),
            idempotency_key=key,
        )
        clean_db.add(dead)
        clean_db.commit()

        with job_run("ingest", cutoff=None, params={"market_limit": 10}):
            pass

        clean_db.expire_all()
        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert rows[0].status == JobStatus.INTERRUPTED.value
        assert rows[1].status == JobStatus.SUCCESS.value

    def test_a_run_that_never_heartbeats_is_still_judged_on_age(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """Short jobs do not heartbeat, and must keep their old semantics."""
        recent = JobRun(
            job_name="ingest",
            status=JobStatus.RUNNING.value,
            started_at=utcnow() - timedelta(minutes=4),
            heartbeat_at=None,
            idempotency_key=key,
        )
        clean_db.add(recent)
        clean_db.commit()

        with pytest.raises(JobSkipped):
            job_run("ingest", cutoff=None, params={"market_limit": 10}).__enter__()

    def test_the_heartbeat_is_committed_so_another_process_can_see_it(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """A heartbeat held inside the job's own transaction proves nothing.

        The reader deciding whether this run is dead is a *different* process.
        """
        with job_run("ingest", cutoff=None, params={"market_limit": 10}) as details:
            details["heartbeat"]()
            clean_db.expire_all()
            row = clean_db.query(JobRun).filter(
                JobRun.status == JobStatus.RUNNING.value
            ).one()
            assert row.heartbeat_at is not None

    def test_a_failing_heartbeat_never_takes_down_the_job(
        self, clean_db, key, monkeypatch  # noqa: ANN001
    ) -> None:
        """Losing a heartbeat write risks eviction; raising loses the capture."""
        import pmvl_worker.jobs as jobs_module

        def boom(*_a, **_k):
            raise RuntimeError("database is locked")

        with job_run("ingest", cutoff=None, params={"market_limit": 10}) as details:
            monkeypatch.setattr(jobs_module, "session_scope", boom)
            try:
                details["heartbeat"]()  # must not raise
            finally:
                # Only the heartbeat is under test; job_run's own bookkeeping
                # still needs a working session to close the run out.
                monkeypatch.undo()


class TestWindowLoopReportsLiveness:
    def test_the_window_loop_heartbeats_every_tick(self) -> None:
        """Otherwise a run longer than the heartbeat bound evicts itself."""
        import ast
        import inspect

        from pmvl_worker import jobs

        tree = ast.parse(inspect.getsource(jobs.job_research_books))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Subscript)
            and isinstance(node.func.slice, ast.Constant)
            and node.func.slice.value == "heartbeat"
        ]
        assert calls, "the window loop no longer reports liveness"

    def test_the_heartbeat_bound_exceeds_several_capture_periods(self) -> None:
        """A bound tighter than the cadence would evict healthy runs."""
        from pmvl_markets.alpha.issuance import RESEARCH_CADENCE_SECONDS

        assert HEARTBEAT_STALE_AFTER.total_seconds() >= 5 * RESEARCH_CADENCE_SECONDS


class TestDetailsDegradePerKey:
    """One unserialisable value must not erase every other reported detail."""

    def test_a_bad_value_does_not_discard_its_siblings(self) -> None:
        from pmvl_worker.jobs import _jsonable

        out = _jsonable({"captures": 84, "handle": lambda: None})
        assert out["captures"] == 84
        assert "repr" in out["handle"]

    def test_nesting_is_preserved(self) -> None:
        from pmvl_worker.jobs import _jsonable

        out = _jsonable({"a": {"b": 1, "c": object()}, "d": [1, object()]})
        assert out["a"]["b"] == 1
        assert out["d"][0] == 1

    def test_the_run_provenance_survives_a_bad_sibling(self, clean_db, key) -> None:  # noqa: ANN001
        """The exact loss that made this visible."""
        with job_run("ingest", cutoff=None, params={"market_limit": 10}) as details:
            details["not_serialisable"] = object()

        row = clean_db.query(JobRun).filter_by(job_name="ingest").one()
        assert row.details["run"]["idempotency_key"] == key
