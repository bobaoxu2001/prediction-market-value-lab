"""Make an operational database safe to copy, and prove it is complete.

A published candidate once contained `settle` still marked RUNNING while the run
report correctly recorded it as SUCCESS. The status was durably committed the
whole time; the artefact simply did not contain it.

Reproduced exactly:

    main file after checkpoint       settle: running
    source, engine still open        settle: success   <- the truth
    raw copy taken by the builder    settle: running   <- the symptom
    raw copy after engine disposed   settle: success

The pipeline holds an open WAL connection while the snapshot builder runs as a
subprocess and does ``shutil.copy2``. That copies the main ``.db`` and not the
``-wal``, so every transaction committed since the last automatic checkpoint is
absent from the copy. SQLite checkpoints roughly every 1000 pages, so which
writes survive depends on how much happened to be written afterwards - the worst
possible property for an artefact people are asked to trust.

This module closes that window before anything reads the file: quiesce the ORM,
dispose the engine, checkpoint the WAL into the main file, verify integrity, and
confirm the run's own jobs reached terminal states. Only then may a candidate be
built from it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Statuses a job may legitimately end on. RUNNING and PENDING are not here: a
#: pipeline that has finished cannot still be running one of its own jobs, and an
#: artefact saying otherwise is describing a run that never completed.
TERMINAL_STATUSES = frozenset({"success", "partial_success", "failed", "skipped", "stale"})
NON_TERMINAL_STATUSES = frozenset({"running", "pending"})


class FinalizationError(RuntimeError):
    """The operational database is not in a state anything may be built from."""


@dataclass
class FinalizationReport:
    journal_mode_before: str = ""
    journal_mode_after: str = ""
    checkpoint_result: tuple[int, int, int] | None = None
    wal_removed: bool = False
    integrity: str = ""
    non_terminal_jobs: list[str] = field(default_factory=list)
    job_statuses: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "journal_mode_before": self.journal_mode_before,
            "journal_mode_after": self.journal_mode_after,
            "checkpoint_result": list(self.checkpoint_result) if self.checkpoint_result else None,
            "wal_removed": self.wal_removed,
            "integrity": self.integrity,
            "non_terminal_jobs": self.non_terminal_jobs,
            "job_statuses": self.job_statuses,
        }


def _quiesce_orm() -> None:
    """Close scoped sessions and dispose the engine.

    Disposing matters for a reason beyond tidiness: SQLite checkpoints the WAL
    when the last connection closes, and an engine holding a pooled connection
    prevents that. Leaving it open is what let a stale copy look plausible.
    """
    from sqlalchemy.orm import close_all_sessions

    from pmvl_shared.db import reset_engine

    # close_all_sessions is the supported API; sessionmaker.close_all() was removed
    # in SQLAlchemy 1.4. Calling the wrong one would raise into a bare except and
    # leave sessions open while appearing to have closed them.
    close_all_sessions()
    reset_engine()


def finalize_operational_database(
    db_path: Path, *, run_job_names: set[str] | None = None
) -> FinalizationReport:
    """Quiesce, checkpoint, verify. Raises rather than returning a bad database.

    ``run_job_names`` scopes the terminal-state check to this run's jobs. Rows
    inherited from the parent snapshot are not this run's business, and failing on
    them would make every run depend on the tidiness of its ancestor.
    """
    report = FinalizationReport()
    _quiesce_orm()

    if not db_path.exists():
        raise FinalizationError(f"operational database missing: {db_path}")

    con = sqlite3.connect(db_path)
    try:
        report.journal_mode_before = con.execute("PRAGMA journal_mode").fetchone()[0]

        if report.journal_mode_before.lower() == "wal":
            # TRUNCATE rather than FULL: FULL flushes the WAL into the database but
            # leaves the -wal file behind, and the published artefact must be a
            # single self-contained file. TRUNCATE does both.
            report.checkpoint_result = tuple(
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            )
            busy, _, _ = report.checkpoint_result
            if busy:
                # A non-zero first column means a reader blocked the checkpoint, so
                # some frames are still only in the WAL. Copying now would lose them.
                raise FinalizationError(
                    f"WAL checkpoint blocked ({report.checkpoint_result}); a "
                    "connection is still open and the copy would be incomplete"
                )

        report.integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if report.integrity != "ok":
            raise FinalizationError(f"integrity_check returned {report.integrity!r}")

        report.job_statuses, report.non_terminal_jobs = _job_states(con, run_job_names)
        report.journal_mode_after = con.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        con.close()

    wal = db_path.with_name(db_path.name + "-wal")
    report.wal_removed = not wal.exists()

    if report.non_terminal_jobs:
        raise FinalizationError(
            "these jobs from this run are still non-terminal after the pipeline "
            f"finished: {', '.join(report.non_terminal_jobs)}. The database is not "
            "safe to publish from."
        )
    return report


def _job_states(
    con: sqlite3.Connection, run_job_names: set[str] | None
) -> tuple[dict[str, str], list[str]]:
    """Latest status per job name, and which of this run's jobs are non-terminal."""
    try:
        rows = list(
            con.execute(
                "SELECT job_name, status FROM job_runs WHERE (job_name, started_at) IN "
                "(SELECT job_name, MAX(started_at) FROM job_runs GROUP BY job_name)"
            )
        )
    except sqlite3.Error:
        return {}, []

    statuses = {name: status for name, status in rows}
    scope = run_job_names if run_job_names is not None else set(statuses)
    non_terminal = sorted(
        name
        for name, status in statuses.items()
        if name in scope and status in NON_TERMINAL_STATUSES
    )
    return statuses, non_terminal


def job_states_in(db_path: Path, run_job_names: set[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Read job states from a closed database, for comparing candidate to source."""
    if not db_path.exists():
        return {}, []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return _job_states(con, run_job_names)
    finally:
        con.close()
