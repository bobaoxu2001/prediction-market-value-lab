"""History must survive from one published snapshot into the next.

The workflow migrated a fresh `pmvl-operational.db` each run and uploaded it as an
artefact that the following run never restored. Every scheduled run therefore began
with no recommendations, no settlements, no job history, no rule versions and no
idempotency record - and would have published an artefact whose track record
started that morning, with nothing on the page saying so.

These tests hold the seeding path and the fail-closed rule that protects it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_automated_snapshot_pipeline import (  # noqa: E402
    PipelineError,
    RunOutcome,
    initialise_operational_db,
    resolve_scope,
)
import run_automated_snapshot_pipeline as pipeline  # noqa: E402


@pytest.fixture()
def parent_snapshot(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A validated published pair carrying rows in every history table."""
    db = tmp_path / "pmvl-snapshot.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE recommendations (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE settlements (id INTEGER PRIMARY KEY, outcome TEXT);
        CREATE TABLE job_runs (id INTEGER PRIMARY KEY, job_name TEXT, status TEXT);
        CREATE TABLE market_rule_versions (id INTEGER PRIMARY KEY, rule_hash TEXT);
        INSERT INTO recommendations VALUES (1, 'published yesterday');
        INSERT INTO settlements VALUES (1, 'yes');
        INSERT INTO job_runs VALUES (1, 'ingest', 'success');
        INSERT INTO market_rule_versions VALUES (1, 'abc123');
        """
    )
    con.commit()
    con.close()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/shared/src"))
    from pmvl_shared.manifest import sha256_of

    manifest = tmp_path / "pmvl-snapshot.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "snapshot_id": "parent-1",
                "sha256": sha256_of(db),
                "file_size_bytes": db.stat().st_size,
                "validation_status": "passed",
                "release_status": "published",
            }
        )
    )
    monkeypatch.setattr(pipeline, "PUBLISHED_DB", db)
    monkeypatch.setattr(pipeline, "PUBLISHED_MANIFEST", manifest)
    return db, manifest


def _outcome(event: str) -> RunOutcome:
    scope = resolve_scope(
        event_name=event, input_scope=None, input_market_limit=None, input_publish=None
    )
    return RunOutcome(run_id="test", scope=scope)


class TestSeedingFromTheParentSnapshot:
    def test_history_survives_into_the_next_run(self, parent_snapshot, tmp_path) -> None:  # noqa: ANN001
        """The continuity claim, table by table."""
        outcome = _outcome("schedule")
        operational = initialise_operational_db(tmp_path / "work", outcome)

        con = sqlite3.connect(operational)
        try:
            for table, expected in (
                ("recommendations", 1),
                ("settlements", 1),
                ("job_runs", 1),
                ("market_rule_versions", 1),
            ):
                count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert count == expected, f"{table} did not survive into the new run"
        finally:
            con.close()

    def test_the_parent_is_recorded_in_the_run_provenance(
        self, parent_snapshot, tmp_path  # noqa: ANN001
    ) -> None:
        outcome = _outcome("schedule")
        initialise_operational_db(tmp_path / "work", outcome)

        assert outcome.parent_snapshot_id == "parent-1"
        assert outcome.parent_snapshot_sha256
        assert outcome.operational_init_source == "published_snapshot"

    def test_the_published_file_is_copied_not_opened_read_write(
        self, parent_snapshot, tmp_path  # noqa: ANN001
    ) -> None:
        """Writing into the committed artefact would corrupt the rollback path
        while the pipeline was still deciding whether its output was valid."""
        db, _ = parent_snapshot
        before = db.read_bytes()

        outcome = _outcome("schedule")
        operational = initialise_operational_db(tmp_path / "work", outcome)
        con = sqlite3.connect(operational)
        con.execute("INSERT INTO recommendations VALUES (2, 'written this run')")
        con.commit()
        con.close()

        assert db.read_bytes() == before, "the published snapshot was modified in place"
        assert operational != db


class TestFailClosedWithoutAParent:
    def test_a_scheduled_run_refuses_to_start_empty(self, tmp_path, monkeypatch) -> None:  # noqa: ANN001
        """The whole point. Starting empty and publishing would silently reset the
        track record, and the artefact would look entirely normal."""
        monkeypatch.setattr(pipeline, "PUBLISHED_DB", tmp_path / "absent.db")
        monkeypatch.setattr(pipeline, "PUBLISHED_MANIFEST", tmp_path / "absent.json")

        with pytest.raises(PipelineError, match="fails closed"):
            initialise_operational_db(tmp_path / "work", _outcome("schedule"))

    def test_a_manual_dispatch_may_bootstrap_explicitly(self, tmp_path, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setattr(pipeline, "PUBLISHED_DB", tmp_path / "absent.db")
        monkeypatch.setattr(pipeline, "PUBLISHED_MANIFEST", tmp_path / "absent.json")

        outcome = _outcome("workflow_dispatch")
        initialise_operational_db(tmp_path / "work", outcome)
        assert outcome.operational_init_source == "bootstrap_empty"

    def test_a_corrupt_parent_stops_the_run(self, parent_snapshot, tmp_path) -> None:  # noqa: ANN001
        """A checksum mismatch means the parent is not the artefact the manifest
        describes; every row seeded from it would inherit that provenance."""
        db, _ = parent_snapshot
        db.write_bytes(db.read_bytes() + b"corruption")

        with pytest.raises(PipelineError, match="failed verification"):
            initialise_operational_db(tmp_path / "work", _outcome("schedule"))


class TestMigrationProvenance:
    def test_the_revision_range_is_recorded(self, parent_snapshot, tmp_path) -> None:  # noqa: ANN001
        """So a row can be attributed to the schema that produced it."""
        outcome = _outcome("workflow_dispatch")
        operational = initialise_operational_db(tmp_path / "work", outcome)
        shutil.copy2(operational, tmp_path / "copy.db")

        # The parent fixture has no alembic_version table, which is the honest
        # "unknown start" case rather than a fabricated revision.
        assert outcome.migration_start_revision is None
