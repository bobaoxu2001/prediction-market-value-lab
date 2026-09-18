"""`publish=false` is a stateless recomputation, and must say so.

The previous canary plan claimed two `publish=false` runs proved "continuity and
idempotency". They do not. Each run seeds from the published snapshot, computes a
candidate, and drops it; the second run inherits nothing from the first - not job
history, not rule versions, not idempotency keys, not settlements. What two such
runs demonstrate is deterministic recomputation from the same parent.

That distinction matters because a stateless recomputation described as continuity
is the same class of overclaim as a configured cadence described as a running one,
which is the defect this whole branch exists to remove.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/shared/src"))

import run_automated_snapshot_pipeline as pipeline  # noqa: E402
from pmvl_shared.manifest import sha256_of  # noqa: E402
from pmvl_shared.snapshot_artifact import (  # noqa: E402
    CURRENT_SNAPSHOT_SCHEMA_REVISION,
    compress_snapshot,
)

from run_automated_snapshot_pipeline import (  # noqa: E402
    EXECUTION_MODEL,
    CandidateDisposition,
    RunOutcome,
    initialise_operational_db,
    resolve_scope,
)


@pytest.fixture()
def published(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    db = tmp_path / "pmvl-snapshot.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE job_runs (id INTEGER PRIMARY KEY, job_name TEXT);"
        "INSERT INTO job_runs VALUES (1, 'ingest');"
    )
    con.commit()
    con.close()

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


def _outcome(event: str = "workflow_dispatch") -> RunOutcome:
    return RunOutcome(
        run_id="t",
        scope=resolve_scope(
            event_name=event, input_scope=None, input_market_limit=None, input_publish=None
        ),
    )


def _compressed_candidate(
    path: Path, *, release_status: str
) -> tuple[Path, Path]:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE candidate_data (value TEXT)")
    connection.execute("INSERT INTO candidate_data VALUES ('candidate')")
    connection.execute(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
    )
    connection.execute(
        "INSERT INTO alembic_version VALUES (?)",
        (CURRENT_SNAPSHOT_SCHEMA_REVISION,),
    )
    connection.execute(
        "CREATE TABLE job_runs ("
        "id INTEGER PRIMARY KEY, job_name TEXT, status TEXT, started_at TEXT"
        ")"
    )
    connection.execute(
        "INSERT INTO job_runs VALUES "
        "(1, 'ingest', 'success', '2026-07-31T08:00:00Z')"
    )
    connection.commit()
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("VACUUM")
    connection.close()

    encoded = path.with_name(path.name + ".gz")
    compress_snapshot(path, encoded)
    manifest = path.with_suffix(".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "snapshot_id": "cand-1",
                "schema_version": CURRENT_SNAPSHOT_SCHEMA_REVISION,
                "schema_revision": CURRENT_SNAPSHOT_SCHEMA_REVISION,
                "code_commit_sha": "1" * 12,
                "source_commit_sha": "1" * 12,
                "built_at": "2026-07-31T08:00:00Z",
                "job_statuses": {"ingest": "success"},
                "artifact_format": "sqlite",
                "artifact_encoding": "gzip",
                "compression_algorithm": "gzip",
                "compression_level": 9,
                "compression_deterministic": True,
                "compressed_path": "data/pmvl-snapshot.db.gz",
                "compressed_sha256": sha256_of(encoded),
                "uncompressed_sha256": sha256_of(path),
                "compressed_size_bytes": encoded.stat().st_size,
                "uncompressed_size_bytes": path.stat().st_size,
                "sha256": sha256_of(path),
                "file_size_bytes": path.stat().st_size,
                "validation_status": "passed",
                "release_status": release_status,
            }
        )
    )
    return path, manifest


class TestTheReportStatesTheExecutionModel:
    def test_it_names_stateless_recomputation(self) -> None:
        report = _outcome().as_dict()
        assert report["execution_model"] == EXECUTION_MODEL
        assert "stateless_recompute" in report["execution_model"]

    def test_it_states_that_nothing_persists(self) -> None:
        """Stated, not implied. A reader must not have to deduce it from the
        absence of a contrary claim."""
        assert _outcome().as_dict()["state_persisted_across_runs"] is False

    def test_an_unbuilt_candidate_is_not_called_discarded(self) -> None:
        """"Discarded" implies something was computed. NOT_BUILT is the truth when
        a required job failed."""
        assert _outcome().as_dict()["candidate_disposition"] == CandidateDisposition.NOT_BUILT


class TestDispositionNeverImpliesPersistence:
    def test_discarded_is_not_publication_eligible(self) -> None:
        outcome = _outcome()
        outcome.candidate_disposition = CandidateDisposition.DISCARDED
        assert outcome.publication_eligible is False
        assert outcome.as_dict()["published"] is False

    def test_uploaded_for_publish_is_eligible_but_not_published(self) -> None:
        """The handover state. The repository is unchanged until the publish job
        commits, so `published` must stay false."""
        outcome = _outcome()
        outcome.candidate_disposition = CandidateDisposition.UPLOADED_FOR_PUBLISH
        assert outcome.publication_eligible is True
        assert outcome.published is False

    def test_a_blocked_run_is_never_eligible(self) -> None:
        outcome = _outcome()
        outcome.candidate_disposition = CandidateDisposition.UPLOADED_FOR_PUBLISH
        outcome.publication_blockers = ["settle did not run"]
        assert outcome.publication_eligible is False


class TestTwoRunsShareAParentAndNothingElse:
    def test_both_runs_report_the_same_parent(self, published, tmp_path) -> None:  # noqa: ANN001
        first, second = _outcome(), _outcome()
        initialise_operational_db(tmp_path / "one", first)
        initialise_operational_db(tmp_path / "two", second)

        assert first.parent_snapshot_id == second.parent_snapshot_id == "parent-1"
        assert first.parent_snapshot_sha256 == second.parent_snapshot_sha256

    def test_the_second_run_does_not_inherit_the_first_candidate(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        """The claim the old canary plan made and the code never supported."""
        first = _outcome()
        first_db = initialise_operational_db(tmp_path / "one", first)
        con = sqlite3.connect(first_db)
        con.execute("INSERT INTO job_runs VALUES (99, 'written-by-run-one')")
        con.commit()
        con.close()

        second = _outcome()
        second_db = initialise_operational_db(tmp_path / "two", second)
        con = sqlite3.connect(second_db)
        rows = [r[0] for r in con.execute("SELECT job_name FROM job_runs")]
        con.close()

        assert "written-by-run-one" not in rows, (
            "run two inherited run one's state, which the execution model says it "
            "does not"
        )
        assert second.operational_init_source == "published_snapshot"

    def test_both_runs_initialise_from_the_published_snapshot(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        for i in range(2):
            outcome = _outcome()
            initialise_operational_db(tmp_path / f"run{i}", outcome)
            assert outcome.operational_init_source == "published_snapshot"


class TestPublicationIsTheOnlyPersistenceBoundary:
    def test_a_non_publishing_run_leaves_the_committed_files_untouched(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        db, manifest = published
        before_db, before_manifest = db.read_bytes(), manifest.read_text()

        outcome = _outcome()
        operational = initialise_operational_db(tmp_path / "work", outcome)
        con = sqlite3.connect(operational)
        con.execute("INSERT INTO job_runs VALUES (50, 'this run')")
        con.commit()
        con.close()
        outcome.candidate_disposition = CandidateDisposition.DISCARDED

        assert db.read_bytes() == before_db
        assert manifest.read_text() == before_manifest

    def test_the_run_after_a_publication_starts_from_the_new_snapshot(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        """The one boundary that does carry state forward."""
        db, manifest = published

        # Simulate a publication replacing the pair.
        con = sqlite3.connect(db)
        con.execute("INSERT INTO job_runs VALUES (2, 'published-later')")
        con.commit()
        con.close()
        published_manifest = json.loads(manifest.read_text())
        published_manifest.update(
            {"snapshot_id": "parent-2", "sha256": sha256_of(db),
             "file_size_bytes": db.stat().st_size}
        )
        manifest.write_text(json.dumps(published_manifest))

        outcome = _outcome()
        operational = initialise_operational_db(tmp_path / "after", outcome)
        con = sqlite3.connect(operational)
        rows = [r[0] for r in con.execute("SELECT job_name FROM job_runs")]
        con.close()

        assert outcome.parent_snapshot_id == "parent-2"
        assert "published-later" in rows


class TestTheHandoverEmitsAHeldCandidate:
    def test_the_emitted_manifest_is_held_not_published(self, tmp_path) -> None:  # noqa: ANN001
        """Marking it published before the git commit would leave an artefact
        asserting a publication that had not happened."""
        candidate, _ = _compressed_candidate(
            tmp_path / "candidate.db",
            release_status="published",  # deliberately wrong on input
        )

        outcome = _outcome()
        outcome.parent_snapshot_id = "parent-1"
        out = pipeline._emit_candidate(candidate, tmp_path / "out", outcome)

        emitted = json.loads((out.with_suffix(".manifest.json")).read_text())
        assert emitted["release_status"] == "held"
        assert emitted["parent_snapshot_id"] == "parent-1"
        assert outcome.candidate_disposition == CandidateDisposition.UPLOADED_FOR_PUBLISH

    def test_the_emitted_candidate_records_its_identity(self, tmp_path) -> None:  # noqa: ANN001
        candidate, _ = _compressed_candidate(
            tmp_path / "candidate.db",
            release_status="held",
        )

        outcome = _outcome()
        pipeline._emit_candidate(candidate, tmp_path / "out", outcome)

        assert outcome.candidate_snapshot_id == "cand-1"
        assert outcome.candidate_sha256 == sha256_of(candidate)
        assert outcome.candidate_uncompressed_sha256 == sha256_of(candidate)
        assert outcome.candidate_compressed_sha256 == sha256_of(
            candidate.with_name(candidate.name + ".gz")
        )
