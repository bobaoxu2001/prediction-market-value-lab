"""The publish job re-checks a candidate it did not build.

Checking again is not redundancy for its own sake. The two jobs run on different
machines, the artefact travels through GitHub's artifact store between them, and
the publish job holds a write token the research job does not. Anything that
trusts an upstream job's word about an artefact it is about to commit cannot
detect a mismatch between what was validated and what arrived.

The parent check is the subtle one: if another publication landed while this run
was computing, promoting this candidate would silently discard it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/shared/src"))

from pmvl_shared.manifest import sha256_of  # noqa: E402

from verify_candidate import check  # noqa: E402


@pytest.fixture()
def candidate(tmp_path):  # noqa: ANN001, ANN201
    db = tmp_path / "candidate.db"
    db.write_bytes(b"CANDIDATE DATABASE BYTES")
    manifest = tmp_path / "candidate.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "snapshot_id": "cand-1",
                "code_commit_sha": "9c0da76be9e9",
                "schema_version": "c3d4e5f6a7b8",
                "model_version": "ensemble-v1.0.0",
                "parser_version": "1.0.0",
                "generated_at": "2026-07-29T10:00:00Z",
                "sha256": sha256_of(db),
                "file_size_bytes": db.stat().st_size,
                "validation_status": "passed",
                "release_status": "held",
                "parent_snapshot_id": "parent-1",
                "parent_snapshot_sha256": "abc123",
            }
        )
    )
    return db, manifest


@pytest.fixture()
def published(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "published.manifest.json"
    path.write_text(json.dumps({"snapshot_id": "parent-1"}))
    return path


class TestAGoodCandidatePasses:
    def test_no_problems(self, candidate, published) -> None:  # noqa: ANN001
        db, manifest = candidate
        assert check(db, manifest, published) == []


class TestTamperingIsCaught:
    def test_modified_bytes_fail_the_checksum(self, candidate, published) -> None:  # noqa: ANN001
        db, manifest = candidate
        db.write_bytes(db.read_bytes() + b"TAMPERED IN TRANSIT")

        problems = check(db, manifest, published)
        assert any("checksum" in p for p in problems)

    def test_a_mismatched_declared_checksum_is_caught(
        self, candidate, published, monkeypatch  # noqa: ANN001
    ) -> None:
        """The artefact that arrived must be the artefact that was validated."""
        db, manifest = candidate
        monkeypatch.setenv("EXPECTED_CANDIDATE_SHA", "0" * 64)

        problems = check(db, manifest, published)
        assert any("does not match the research job's declared" in p for p in problems)

    def test_a_mismatched_snapshot_id_is_caught(
        self, candidate, published, monkeypatch  # noqa: ANN001
    ) -> None:
        db, manifest = candidate
        monkeypatch.setenv("EXPECTED_CANDIDATE_ID", "some-other-candidate")

        problems = check(db, manifest, published)
        assert any("snapshot_id" in p for p in problems)


class TestReleaseStatusMustBeHeld:
    def test_an_already_published_candidate_is_rejected(
        self, candidate, published  # noqa: ANN001
    ) -> None:
        """It would be asserting a publication that has not happened."""
        db, manifest = candidate
        data = json.loads(manifest.read_text())
        data["release_status"] = "published"
        manifest.write_text(json.dumps(data))

        problems = check(db, manifest, published)
        assert any("release_status" in p for p in problems)

    def test_an_unvalidated_candidate_is_rejected(self, candidate, published) -> None:  # noqa: ANN001
        db, manifest = candidate
        data = json.loads(manifest.read_text())
        data["validation_status"] = "pending"
        manifest.write_text(json.dumps(data))

        problems = check(db, manifest, published)
        assert problems


class TestParentMismatchStopsALostUpdate:
    def test_a_moved_published_parent_is_rejected(self, candidate, tmp_path) -> None:  # noqa: ANN001
        """Another publication landed while this run computed. Promoting anyway
        would silently discard it."""
        db, manifest = candidate
        moved = tmp_path / "published.manifest.json"
        moved.write_text(json.dumps({"snapshot_id": "parent-2"}))

        problems = check(db, manifest, moved)
        assert any("parent mismatch" in p for p in problems)
        assert any("would discard the intervening snapshot" in p for p in problems)

    def test_a_matching_parent_passes(self, candidate, published) -> None:  # noqa: ANN001
        db, manifest = candidate
        assert not any("parent mismatch" in p for p in check(db, manifest, published))


class TestCommitProvenance:
    def test_a_candidate_built_from_another_commit_is_rejected(
        self, candidate, published, monkeypatch  # noqa: ANN001
    ) -> None:
        """Publishing a candidate built from different code than the job checked
        out would attribute one commit's output to another."""
        db, manifest = candidate
        monkeypatch.setenv("EXPECTED_COMMIT", "deadbeefcafe0000")

        problems = check(db, manifest, published)
        assert any("was built from commit" in p for p in problems)

    def test_the_matching_commit_prefix_passes(
        self, candidate, published, monkeypatch  # noqa: ANN001
    ) -> None:
        """The manifest stores 12 characters; the workflow passes the full SHA."""
        db, manifest = candidate
        monkeypatch.setenv("EXPECTED_COMMIT", "9c0da76be9e900fc90b6aad3c5652b44331059f2")

        assert not any("was built from commit" in p for p in check(db, manifest, published))


class TestPlaceholderProvenanceIsRejected:
    def test_an_unattributable_candidate_cannot_be_published(
        self, candidate, published  # noqa: ANN001
    ) -> None:
        db, manifest = candidate
        data = json.loads(manifest.read_text())
        data["code_commit_sha"] = "unknown"
        manifest.write_text(json.dumps(data))

        problems = check(db, manifest, published)
        assert any("code_commit_sha" in p for p in problems)

    def test_the_legacy_exemption_does_not_apply_to_candidates(
        self, candidate, published  # noqa: ANN001
    ) -> None:
        """The exemption covers the existing rollback artefact only. A newly built
        candidate carrying it would be using a documented gap as a loophole."""
        db, manifest = candidate
        data = json.loads(manifest.read_text())
        data["code_commit_sha"] = "unknown"
        data["legacy_provenance_exemption"] = "trying to sneak through"
        manifest.write_text(json.dumps(data))

        problems = check(db, manifest, published)
        assert any("code_commit_sha" in p for p in problems)


class TestMissingFiles:
    def test_a_missing_candidate_is_reported(self, tmp_path, published) -> None:  # noqa: ANN001
        problems = check(tmp_path / "absent.db", tmp_path / "absent.json", published)
        assert any("missing" in p for p in problems)
