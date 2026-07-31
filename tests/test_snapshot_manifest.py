"""A published artefact must declare what it is, and be checkable against that.

The snapshot was a bare 8 MB binary. Nothing travelling with it said which commit
produced it, which schema it matched, or whether anyone had validated it - so
"is the deployed snapshot the one we think it is" could only be answered by
opening it and guessing. Three production outages traced to that gap.
"""

from __future__ import annotations

import json

import pytest

from pmvl_shared.manifest import (
    ReleaseStatus,
    SnapshotManifest,
    ValidationStatus,
    sha256_of,
    verify_artifact,
)


@pytest.fixture()
def artifact(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "snap.db"
    path.write_bytes(b"SQLite format 3\x00" + b"x" * 4096)
    return path


@pytest.fixture()
def manifest_for(artifact, tmp_path):  # noqa: ANN001, ANN201
    def build(**overrides):  # noqa: ANN202
        base = dict(
            snapshot_id="abc123-1",
            code_commit_sha="abc123def456",
            schema_version="a1b2c3d4e5f6",
            model_version="ensemble-v1.0.0",
            parser_version="unversioned",
            file_size_bytes=artifact.stat().st_size,
            sha256=sha256_of(artifact),
            validation_status=ValidationStatus.PASSED,
            release_status=ReleaseStatus.PUBLISHED,
        )
        base.update(overrides)
        manifest = SnapshotManifest(**base)
        path = tmp_path / "snap.manifest.json"
        manifest.write(path)
        return manifest, path

    return build


class TestChecksumGate:
    def test_a_matching_artifact_verifies(self, artifact, manifest_for) -> None:  # noqa: ANN001
        _, path = manifest_for()
        assert verify_artifact(artifact, path) == []

    def test_a_modified_artifact_is_rejected(self, artifact, manifest_for) -> None:  # noqa: ANN001
        """The point of the checksum: a byte changed after the manifest was written
        must not be served."""
        _, path = manifest_for()
        artifact.write_bytes(artifact.read_bytes() + b"tampered")

        problems = verify_artifact(artifact, path)
        assert problems, "a modified artifact passed verification"
        assert any("checksum" in p for p in problems)

    def test_a_truncated_artifact_is_rejected(self, artifact, manifest_for) -> None:  # noqa: ANN001
        """A partially-written file is the failure atomic publication exists to
        prevent; verification is the backstop if it ever escapes."""
        _, path = manifest_for()
        artifact.write_bytes(artifact.read_bytes()[:100])

        problems = verify_artifact(artifact, path)
        assert any("size" in p for p in problems)

    def test_a_missing_artifact_is_reported(self, artifact, manifest_for) -> None:  # noqa: ANN001
        _, path = manifest_for()
        artifact.unlink()
        assert any("missing" in p for p in verify_artifact(artifact, path))

    def test_a_missing_manifest_is_reported(self, artifact, tmp_path) -> None:  # noqa: ANN001
        problems = verify_artifact(artifact, tmp_path / "absent.json")
        assert any("manifest missing" in p for p in problems)

    def test_every_problem_is_reported_not_just_the_first(
        self, artifact, manifest_for  # noqa: ANN001
    ) -> None:
        """A deploy step wants every reason it refused, not one at a time."""
        _, path = manifest_for(validation_status=ValidationStatus.FAILED)
        artifact.write_bytes(b"different content entirely")

        problems = verify_artifact(artifact, path)
        assert len(problems) >= 2


class TestUnvalidatedArtifactsAreNeverDeployable:
    def test_pending_validation_blocks_deployment(self, artifact, manifest_for) -> None:  # noqa: ANN001
        """An artefact nobody has checked must not reach production, and the
        default state of a freshly built manifest is exactly that."""
        _, path = manifest_for(validation_status=ValidationStatus.PENDING)
        assert any("validation_status" in p for p in verify_artifact(artifact, path))

    def test_failed_validation_blocks_deployment(self, artifact, manifest_for) -> None:  # noqa: ANN001
        _, path = manifest_for(validation_status=ValidationStatus.FAILED)
        assert any("validation_status" in p for p in verify_artifact(artifact, path))

    def test_deployable_requires_both_validated_and_released(self) -> None:
        """A validated artefact that was held back - because a provider failed,
        say - is not deployable either."""
        base = dict(
            snapshot_id="s",
            code_commit_sha="c",
            schema_version="v",
            model_version="m",
            parser_version="p",
        )
        assert SnapshotManifest(
            **base,
            validation_status=ValidationStatus.PASSED,
            release_status=ReleaseStatus.PUBLISHED,
        ).deployable
        assert not SnapshotManifest(
            **base,
            validation_status=ValidationStatus.PASSED,
            release_status=ReleaseStatus.HELD,
        ).deployable
        assert not SnapshotManifest(
            **base,
            validation_status=ValidationStatus.FAILED,
            release_status=ReleaseStatus.PUBLISHED,
        ).deployable

    def test_a_fresh_manifest_defaults_to_held_and_pending(self) -> None:
        manifest = SnapshotManifest(
            snapshot_id="s",
            code_commit_sha="c",
            schema_version="v",
            model_version="m",
            parser_version="p",
        )
        assert manifest.validation_status == ValidationStatus.PENDING
        assert manifest.release_status == ReleaseStatus.HELD
        assert manifest.deployable is False


class TestTheCommittedManifest:
    """The artefact this repository actually ships."""

    def test_it_exists_and_matches_the_committed_snapshot(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        manifest = root / "data" / "pmvl-snapshot.manifest.json"
        assert manifest.exists(), "the shipped artifact has no manifest"
        data = json.loads(manifest.read_text())
        artifact = root / (
            "data/pmvl-snapshot.db.gz"
            if data.get("artifact_encoding") == "gzip"
            else "data/pmvl-snapshot.db"
        )
        if not artifact.exists():
            pytest.skip("snapshot artifact not present in this checkout")

        assert verify_artifact(artifact, manifest) == []

    def test_it_records_the_schema_it_was_built_against(self) -> None:
        """The schema-drift outage: a committed snapshot predated a migration and
        the API returned 500 on a column that did not exist."""
        from pathlib import Path

        manifest = Path(__file__).resolve().parents[1] / "data" / "pmvl-snapshot.manifest.json"
        if not manifest.exists():
            pytest.skip("no manifest in this checkout")

        data = json.loads(manifest.read_text())
        assert data["schema_version"] not in ("", "unknown", None)
        assert data["row_counts"], "a snapshot with no row counts is not auditable"
