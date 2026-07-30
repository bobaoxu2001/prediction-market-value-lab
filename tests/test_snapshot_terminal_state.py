"""A candidate must contain the run's final job statuses, not a stale copy of them.

A published candidate contained `settle` marked RUNNING while the run report
correctly recorded SUCCESS. The status was durably committed the whole time; the
artefact simply did not contain it.

Reproduced exactly, and pinned by `test_a_raw_copy_loses_post_checkpoint_writes`:

    main file after checkpoint       settle: running
    source, engine still open        settle: success   <- the truth
    raw copy taken by the builder    settle: running   <- the symptom
    raw copy after engine disposed   settle: success

The pipeline held an open WAL connection while the builder ran as a subprocess
and did `shutil.copy2`, which takes the main .db and leaves the -wal behind. Which
writes survived depended on how many pages happened to be written afterwards —
the worst possible property for an artefact people are asked to trust.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/shared/src"))

from pmvl_shared.config import get_settings  # noqa: E402
from pmvl_shared.db import get_engine, reset_engine, session_scope  # noqa: E402
from pmvl_shared.db.base import Base  # noqa: E402
from pmvl_shared.db.finalize import (  # noqa: E402
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    FinalizationError,
    finalize_operational_database,
    job_states_in,
)
from pmvl_shared.timeutil import utcnow  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_binding():  # noqa: ANN202
    """These tests rebind a process-wide engine; put it back afterwards."""
    import os

    original = os.environ.get("DATABASE_URL")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        get_settings.cache_clear()
        reset_engine()


def _operational(tmp_path: Path, *, status: str = "running") -> Path:
    """A WAL database with one job row, bound through the real ORM."""
    import os

    path = tmp_path / "operational.db"
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{path}"
    get_settings.cache_clear()
    reset_engine()

    import pmvl_markets.db_models  # noqa: F401 - registers the tables
    from pmvl_markets.db_models import JobRun

    Base.metadata.create_all(get_engine())
    with session_scope() as db:
        db.add(JobRun(job_name="settle", status=status, started_at=utcnow()))
    return path


def _finish(job_name: str = "settle", status: str = "success") -> None:
    from pmvl_markets.db_models import JobRun

    with session_scope() as db:
        row = db.query(JobRun).filter_by(job_name=job_name).one()
        row.status = status
        row.finished_at = utcnow()


def _read(path: Path) -> dict[str, str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT job_name, status FROM job_runs"))
    finally:
        con.close()


class TestTheOriginalDefect:
    def test_a_raw_copy_loses_post_checkpoint_writes(self, tmp_path) -> None:  # noqa: ANN001
        """The incident, reproduced. This is what `shutil.copy2` did."""
        path = _operational(tmp_path)

        # Checkpoint so the main file is valid and holds RUNNING - what a long run
        # does naturally, since SQLite auto-checkpoints around 1000 pages.
        with get_engine().connect() as con:
            con.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        assert _read(path) == {"settle": "running"}

        _finish()
        assert _read(path) == {"settle": "success"}, "the source holds the truth"

        stale = tmp_path / "stale.db"
        shutil.copy2(path, stale)
        assert _read(stale) == {"settle": "running"}, (
            "expected the raw copy to be stale; if this now passes cleanly the "
            "WAL behaviour has changed and the fix should be re-examined"
        )

    def test_finalisation_makes_the_same_copy_correct(self, tmp_path) -> None:  # noqa: ANN001
        """The fix, against the identical setup."""
        path = _operational(tmp_path)
        with get_engine().connect() as con:
            con.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        _finish()

        finalize_operational_database(path, run_job_names={"settle"})

        good = tmp_path / "good.db"
        shutil.copy2(path, good)
        assert _read(good) == {"settle": "success"}


class TestFinalizationSequence:
    def test_it_disposes_the_engine_and_checkpoints(self, tmp_path) -> None:  # noqa: ANN001
        path = _operational(tmp_path)
        _finish()

        report = finalize_operational_database(path, run_job_names={"settle"})

        assert report.journal_mode_before.lower() == "wal"
        assert report.checkpoint_result is not None
        assert report.checkpoint_result[0] == 0, "checkpoint was blocked by a reader"
        assert report.integrity == "ok"

    def test_the_wal_sidecar_is_gone_afterwards(self, tmp_path) -> None:  # noqa: ANN001
        """The published artefact must be one self-contained file, not three."""
        path = _operational(tmp_path)
        _finish()
        finalize_operational_database(path, run_job_names={"settle"})

        assert not path.with_name(path.name + "-wal").exists()
        assert not path.with_name(path.name + "-shm").exists()

    def test_the_candidate_opens_without_sidecars(self, tmp_path) -> None:  # noqa: ANN001
        path = _operational(tmp_path)
        _finish()
        finalize_operational_database(path, run_job_names={"settle"})

        moved = tmp_path / "alone" / "candidate.db"
        moved.parent.mkdir()
        shutil.copy2(path, moved)
        assert _read(moved) == {"settle": "success"}

    def test_an_open_session_does_not_hide_the_final_status(self, tmp_path) -> None:  # noqa: ANN001
        """close_all_sessions is called for a reason; sessionmaker.close_all() was
        removed in SQLAlchemy 1.4 and calling it would raise into a bare except."""
        path = _operational(tmp_path)
        _finish()

        from sqlalchemy import text

        from pmvl_shared.db.base import get_session_factory

        # A session left open with an active read, exactly the condition that
        # would block a WAL checkpoint.
        lingering = get_session_factory()()
        lingering.execute(text("SELECT COUNT(*) FROM job_runs")).scalar()

        report = finalize_operational_database(path, run_job_names={"settle"})
        assert report.job_statuses["settle"] == "success"
        assert report.checkpoint_result[0] == 0, (
            "the lingering session blocked the checkpoint; close_all_sessions "
            "did not do its job"
        )

    def test_a_missing_database_raises(self, tmp_path) -> None:  # noqa: ANN001
        with pytest.raises(FinalizationError, match="missing"):
            finalize_operational_database(tmp_path / "absent.db")


class TestTerminalStateEnforcement:
    def test_a_running_job_from_this_run_blocks(self, tmp_path) -> None:  # noqa: ANN001
        """Not a warning. A pipeline that has finished cannot still be running one
        of its own jobs, and an artefact saying otherwise describes a run that
        never completed."""
        path = _operational(tmp_path, status="running")

        with pytest.raises(FinalizationError, match="non-terminal"):
            finalize_operational_database(path, run_job_names={"settle"})

    def test_a_running_job_from_the_PARENT_does_not_block(self, tmp_path) -> None:  # noqa: ANN001
        """Rows inherited from the parent snapshot are not this run's business;
        failing on them would make every run depend on its ancestor's tidiness."""
        path = _operational(tmp_path, status="running")
        report = finalize_operational_database(path, run_job_names={"ingest"})
        assert report.non_terminal_jobs == []

    @pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
    def test_every_terminal_status_is_accepted(self, tmp_path, status) -> None:  # noqa: ANN001
        path = _operational(tmp_path, status=status)
        report = finalize_operational_database(path, run_job_names={"settle"})
        assert report.non_terminal_jobs == []

    @pytest.mark.parametrize("status", sorted(NON_TERMINAL_STATUSES))
    def test_every_non_terminal_status_is_rejected(self, tmp_path, status) -> None:  # noqa: ANN001
        path = _operational(tmp_path, status=status)
        with pytest.raises(FinalizationError):
            finalize_operational_database(path, run_job_names={"settle"})

    def test_a_failed_job_is_not_rewritten_to_success(self, tmp_path) -> None:  # noqa: ANN001
        """Finalisation makes state durable; it must never improve it."""
        path = _operational(tmp_path, status="failed")
        report = finalize_operational_database(path, run_job_names={"settle"})
        assert report.job_statuses["settle"] == "failed"

    def test_a_partial_success_stays_partial(self, tmp_path) -> None:  # noqa: ANN001
        path = _operational(tmp_path, status="partial_success")
        report = finalize_operational_database(path, run_job_names={"settle"})
        assert report.job_statuses["settle"] == "partial_success"


class TestJobStatesIn:
    def test_it_reads_a_closed_candidate(self, tmp_path) -> None:  # noqa: ANN001
        path = _operational(tmp_path)
        _finish()
        finalize_operational_database(path, run_job_names={"settle"})

        statuses, non_terminal = job_states_in(path, {"settle"})
        assert statuses == {"settle": "success"}
        assert non_terminal == []

    def test_it_flags_a_non_terminal_candidate_row(self, tmp_path) -> None:  # noqa: ANN001
        path = _operational(tmp_path, status="running")
        reset_engine()
        statuses, non_terminal = job_states_in(path, {"settle"})
        assert statuses == {"settle": "running"}
        assert non_terminal == ["settle"]

    def test_a_missing_database_is_empty_not_an_error(self, tmp_path) -> None:  # noqa: ANN001
        assert job_states_in(tmp_path / "absent.db") == ({}, [])


class TestTheDefaultDatabaseIsUntouched:
    def test_finalising_a_temp_db_does_not_touch_the_repository_default(
        self, tmp_path  # noqa: ANN001
    ) -> None:
        default = Path(__file__).resolve().parents[1] / "data" / "pmvl.db"
        before = default.stat().st_mtime if default.exists() else None

        path = _operational(tmp_path)
        _finish()
        finalize_operational_database(path, run_job_names={"settle"})

        after = default.stat().st_mtime if default.exists() else None
        assert after == before
