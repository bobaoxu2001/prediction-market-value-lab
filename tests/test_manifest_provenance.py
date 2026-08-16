"""A manifest whose job is provenance must not itself be unattributable.

The committed artefact carries `code_commit_sha: unknown`, `parser_version:
unversioned` and `snapshot_id: unknown-1785289933`. That is tolerable for a
rollback artefact built before either was recorded, and intolerable for anything
generated from now on: a manifest exists to say which code produced which bytes,
and one that cannot is a reassuring-looking null.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmvl_shared.manifest import REQUIRED_PROVENANCE, provenance_problems
from pmvl_shared.snapshot_artifact import CURRENT_SNAPSHOT_SCHEMA_REVISION


def _good() -> dict:
    return {
        "code_commit_sha": "43811bb6df9a",
        "schema_version": CURRENT_SNAPSHOT_SCHEMA_REVISION,
        "model_version": "ensemble-v1.0.0",
        "parser_version": "1.0.0",
        "snapshot_id": "43811bb6df9a-2026-07-28T08:07:03",
        "generated_at": "2026-07-28T12:00:00Z",
    }


class TestNewArtifactsMustBeAttributable:
    def test_a_complete_manifest_passes(self) -> None:
        assert provenance_problems(_good()) == []

    @pytest.mark.parametrize("field", REQUIRED_PROVENANCE)
    def test_every_required_field_is_enforced(self, field: str) -> None:
        manifest = _good()
        manifest[field] = "unknown"
        problems = provenance_problems(manifest)
        assert any(field in p for p in problems), f"{field} is not enforced"

    @pytest.mark.parametrize("placeholder", ["", "unknown", "unversioned", None, "  UNKNOWN  "])
    def test_placeholders_are_rejected_in_any_casing(self, placeholder) -> None:  # noqa: ANN001
        manifest = _good()
        manifest["parser_version"] = placeholder
        assert provenance_problems(manifest)

    def test_a_snapshot_id_derived_from_an_unknown_commit_is_rejected(self) -> None:
        """It looks populated, which is worse than being blank."""
        manifest = _good()
        manifest["snapshot_id"] = "unknown-1785289933"
        assert any("snapshot_id" in p for p in provenance_problems(manifest))


class TestTheLegacyExemption:
    def test_an_exempt_manifest_skips_the_gate(self) -> None:
        manifest = {"code_commit_sha": "unknown", "parser_version": "unversioned"}
        assert provenance_problems(manifest, legacy_exempt=True) == []

    def test_the_committed_artifact_carries_a_documented_exemption(self) -> None:
        """If the exemption is ever removed, the artefact must be rebuilt rather
        than quietly relabelled."""
        path = Path(__file__).resolve().parents[1] / "data" / "pmvl-snapshot.manifest.json"
        if not path.exists():
            pytest.skip("no manifest in this checkout")

        manifest = json.loads(path.read_text())
        if provenance_problems(manifest):
            reason = manifest.get("legacy_provenance_exemption", "")
            assert reason, "the artifact has placeholder provenance and no exemption"
            assert "rollback" in reason.lower(), "the exemption does not say why"

    def test_the_exemption_is_not_granted_by_default(self) -> None:
        assert provenance_problems({"code_commit_sha": "unknown"})


class TestDeterministicSnapshotId:
    def test_the_builder_derives_the_id_from_commit_and_cutoff(self) -> None:
        """Two artefacts built from identical inputs must get the same id; a
        wall-clock component would make them look different for no reason."""
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "build_snapshot.py"
        ).read_text()
        assert 'snapshot_id = f"{commit}-{(freshest or ' in source
        assert "st_mtime" not in source.split("snapshot_id =")[1][:200]
