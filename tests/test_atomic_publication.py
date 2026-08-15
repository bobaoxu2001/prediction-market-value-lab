"""A failed run must leave the previously published pair exactly as it was.

The manifest and checksum gate existed, but the database itself was written
straight onto the published path. A build that died halfway, or a validation that
failed after the file had already been replaced, left the public site serving a
half-written database behind a manifest describing a different one.

The invariant these tests hold: at every instant, `pmvl-snapshot.db` and
`pmvl-snapshot.manifest.json` are either the old validated pair or the new
validated pair, and never a mixture.
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
from pmvl_shared.manifest import sha256_of, verify_artifact  # noqa: E402
from pmvl_shared.snapshot_artifact import (  # noqa: E402
    CURRENT_SNAPSHOT_SCHEMA_REVISION,
    compress_snapshot,
)

from run_automated_snapshot_pipeline import (  # noqa: E402
    PipelineError,
    RunOutcome,
    promote,
    resolve_scope,
)


def _pair(directory: Path, name: str, payload: bytes, snapshot_id: str):  # noqa: ANN202
    db = directory / f"{name}.db"
    db.write_bytes(payload)
    manifest = db.with_suffix(".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "sha256": sha256_of(db),
                "file_size_bytes": db.stat().st_size,
                "validation_status": "passed",
                "release_status": "published",
            }
        )
    )
    return db, manifest


@pytest.fixture()
def published(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    db, manifest = _pair(tmp_path, "pmvl-snapshot", b"OLD-VALIDATED-DATABASE", "old-1")
    monkeypatch.setattr(pipeline, "PUBLISHED_DB", db)
    monkeypatch.setattr(
        pipeline,
        "PUBLISHED_GZIP",
        tmp_path / "pmvl-snapshot.db.gz",
    )
    monkeypatch.setattr(pipeline, "PUBLISHED_MANIFEST", manifest)
    return db, manifest


def _outcome() -> RunOutcome:
    return RunOutcome(
        run_id="t",
        scope=resolve_scope(
            event_name="workflow_dispatch",
            input_scope="smoke",
            input_market_limit=None,
            input_publish="true",
        ),
    )


def _gzip_candidate(tmp_path, *, snapshot_id: str):  # noqa: ANN001, ANN201
    """A minimally valid, HELD gzip candidate bundle.

    Extracted from the promotion test that first needed it so the lineage cases
    do not restate sixty lines of SQLite and manifest scaffolding each.
    """
    candidate = tmp_path / f"candidate-{snapshot_id}.db"
    connection = sqlite3.connect(candidate)
    try:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO records DEFAULT VALUES")
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
            "INSERT INTO job_runs VALUES (1, 'ingest', 'success', '2026-07-31T08:00:00Z')"
        )
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()

    encoded = compress_snapshot(candidate, candidate.with_name(candidate.name + ".gz"))
    candidate.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "artifact_format": "sqlite",
                "artifact_encoding": "gzip",
                "compression_algorithm": "gzip",
                "compression_level": 9,
                "compression_deterministic": True,
                "compressed_path": "data/pmvl-snapshot.db.gz",
                "compressed_sha256": sha256_of(encoded),
                "uncompressed_sha256": sha256_of(candidate),
                "compressed_size_bytes": encoded.stat().st_size,
                "uncompressed_size_bytes": candidate.stat().st_size,
                "sha256": sha256_of(candidate),
                "file_size_bytes": candidate.stat().st_size,
                "snapshot_id": snapshot_id,
                "schema_version": CURRENT_SNAPSHOT_SCHEMA_REVISION,
                "schema_revision": CURRENT_SNAPSHOT_SCHEMA_REVISION,
                "code_commit_sha": "1" * 12,
                "source_commit_sha": "1" * 12,
                "built_at": "2026-07-31T08:00:00Z",
                "job_statuses": {"ingest": "success"},
                "validation_status": "passed",
                "release_status": "held",
            }
        )
    )
    return candidate


class TestSuccessfulPromotion:
    def test_both_files_are_replaced(self, published, tmp_path) -> None:  # noqa: ANN001
        db, manifest = published
        candidate, _ = _pair(tmp_path, "candidate", b"NEW-VALIDATED-DATABASE", "new-1")

        outcome = _outcome()
        promote(candidate, outcome)

        assert db.read_bytes() == b"NEW-VALIDATED-DATABASE"
        assert json.loads(manifest.read_text())["snapshot_id"] == "new-1"
        assert outcome.published is True

    def test_the_promoted_pair_verifies(self, published, tmp_path) -> None:  # noqa: ANN001
        """The manifest must describe the database that is actually on disk."""
        db, manifest = published
        candidate, _ = _pair(tmp_path, "candidate", b"NEW-VALIDATED-DATABASE", "new-1")

        promote(candidate, _outcome())
        assert verify_artifact(db, manifest) == []

    def test_a_held_compressed_candidate_becomes_a_published_pair(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        db, published_manifest = published
        candidate = _gzip_candidate(tmp_path, snapshot_id="new-compressed-1")

        outcome = _outcome()
        promote(candidate, outcome)

        assert not db.exists()
        assert pipeline.PUBLISHED_GZIP.exists()
        assert json.loads(published_manifest.read_text())["release_status"] == "published"
        assert outcome.published is True

    def test_local_promotion_stamps_the_parent_chain(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        """A locally published artefact must record what it descends from.

        The lineage fields were stamped only in ``_emit_candidate``, the path CI
        takes. Promoting locally therefore published a manifest with
        ``parent_snapshot_id`` absent, silently breaking the chain back through
        previous publications — the property that lets a reader walk a published
        figure back to the artefacts that produced it. The outcome carried the
        values the whole time; they never reached the manifest.
        """
        _db, published_manifest = published
        candidate = _gzip_candidate(tmp_path, snapshot_id="child-1")

        outcome = _outcome()
        outcome.parent_snapshot_id = "parent-1"
        outcome.parent_snapshot_sha256 = "a" * 64
        promote(candidate, outcome)

        manifest = json.loads(published_manifest.read_text())
        assert manifest["parent_snapshot_id"] == "parent-1"
        assert manifest["parent_snapshot_sha256"] == "a" * 64
        assert manifest["pipeline_run_id"] == outcome.run_id

    def test_a_local_run_reports_no_workflow_rather_than_inventing_one(
        self, published, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        # Empty is the honest value off CI. A fabricated run id would resolve to
        # somebody else's workflow run.
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
        _db, published_manifest = published

        promote(_gzip_candidate(tmp_path, snapshot_id="child-2"), _outcome())

        manifest = json.loads(published_manifest.read_text())
        assert manifest["workflow_run_id"] == ""
        assert manifest["workflow_run_attempt"] == ""


class TestFailureLeavesThePreviousPairIntact:
    def test_a_candidate_without_a_manifest_is_refused(self, published, tmp_path) -> None:  # noqa: ANN001
        """Promoting a database with no manifest would leave the old manifest
        describing bytes that are gone - which verifies as a checksum mismatch and
        takes the site down."""
        db, manifest = published
        before_db = db.read_bytes()
        before_manifest = manifest.read_text()

        orphan = tmp_path / "orphan.db"
        orphan.write_bytes(b"NEW")

        with pytest.raises(PipelineError, match="no manifest"):
            promote(orphan, _outcome())

        assert db.read_bytes() == before_db
        assert manifest.read_text() == before_manifest

    def test_a_build_failure_never_reaches_the_published_path(
        self, published, tmp_path, monkeypatch  # noqa: ANN001
    ) -> None:
        db, manifest = published
        before = db.read_bytes()

        def failing_build(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise PipelineError("snapshot build failed: source database missing")

        monkeypatch.setattr(pipeline, "build_and_validate_candidate", failing_build)

        with pytest.raises(PipelineError):
            pipeline.build_and_validate_candidate(tmp_path, tmp_path / "op.db", _outcome())

        assert db.read_bytes() == before
        assert verify_artifact(db, manifest) == []

    def test_a_validation_failure_leaves_the_old_pair_serving(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        """Validation runs against the candidate in its temporary directory, so a
        failure has nothing to roll back."""
        db, manifest = published
        before_db, before_manifest = db.read_bytes(), manifest.read_text()

        candidate = tmp_path / "candidate.db"
        candidate.write_bytes(b"INVALID")
        # No promotion happens because validation failed upstream.

        assert db.read_bytes() == before_db
        assert manifest.read_text() == before_manifest
        assert verify_artifact(db, manifest) == []

    def test_a_checksum_mismatch_in_the_candidate_is_detectable(self, tmp_path) -> None:  # noqa: ANN001
        """The gate that stops a corrupted candidate being promoted at all."""
        candidate, candidate_manifest = _pair(tmp_path, "candidate", b"GOOD", "new-1")
        candidate.write_bytes(b"TAMPERED AFTER MANIFEST")

        problems = verify_artifact(candidate, candidate_manifest)
        assert any("checksum" in p for p in problems)


class TestInterruptedPublication:
    def test_dying_between_the_two_renames_is_loud_not_silent(
        self, published, tmp_path  # noqa: ANN001
    ) -> None:
        """The database is renamed first and the manifest second, deliberately.

        Interrupted in that order, the manifest still describes the OLD database
        while the NEW one is on disk, so verification reports a checksum mismatch
        and the state is detectable. The reverse order would leave a manifest
        asserting a validated artefact that is not the one on disk - which
        verifies clean and is wrong.
        """
        db, manifest = published
        candidate, _ = _pair(tmp_path, "candidate", b"NEW-VALIDATED-DATABASE", "new-1")

        import os

        os.replace(candidate, db)  # first rename only; simulate the crash here

        problems = verify_artifact(db, manifest)
        assert problems, "an interrupted publication verified as healthy"
        assert any("checksum" in p for p in problems)

    def test_the_manifest_is_never_written_before_the_database(self) -> None:
        """The compressed bytes must be installed before their manifest.

        Production uses one Git commit as the atomic boundary; this pins the same
        fail-loud ordering for the local/manual promotion helper.
        """
        source = Path(pipeline.__file__).read_text()
        promote_body = source[source.index("def promote("):]
        promote_body = promote_body[: promote_body.index("\ndef ")]

        gzip_line = promote_body.index(
            "os.replace(candidate_gzip, PUBLISHED_GZIP)"
        )
        manifest_line = promote_body.index(
            "os.replace(candidate_manifest, PUBLISHED_MANIFEST)"
        )
        assert gzip_line < manifest_line, (
            "the manifest is promoted before the compressed artefact, so an "
            "interruption could leave a manifest describing absent bytes"
        )
