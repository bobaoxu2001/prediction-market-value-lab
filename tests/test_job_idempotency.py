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

from pmvl_worker.jobs import ACTIVE_RUN_STALE_AFTER, JobSkipped, job_run  # noqa: E402

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
