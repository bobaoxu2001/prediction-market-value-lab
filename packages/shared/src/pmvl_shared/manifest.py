"""What a published snapshot has to declare about itself.

The artefact was a bare 8 MB binary. Nothing travelling with it said which commit
produced it, which schema it matched, what input cutoff it saw, or whether it had
been validated - so "is the deployed snapshot the one we think it is" could only
be answered by opening it and guessing.

Three of the four production outages traced to that gap: a snapshot missing from
the bundle, one in the wrong journal mode, and one whose schema predated a
migration. Each was invisible until a request failed.

The manifest is written next to the artefact and is the thing a deploy checks. Its
checksum is over the artefact's bytes, so a truncated or partially-written file
fails verification rather than being served.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutil import iso, utcnow

#: Read in chunks so a large artefact is not loaded into memory to be hashed.
_CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class ValidationStatus:
    PASSED = "passed"
    FAILED = "failed"
    #: Written before validation runs. A manifest in this state must never be
    #: deployed - it records an artefact nobody has checked.
    PENDING = "pending"


class ReleaseStatus:
    PUBLISHED = "published"
    HELD = "held"


@dataclass
class SnapshotManifest:
    snapshot_id: str
    code_commit_sha: str
    schema_version: str
    model_version: str
    parser_version: str
    generated_at: datetime = field(default_factory=utcnow)
    source_data_cutoff: datetime | None = None
    ingest_window_start: datetime | None = None
    ingest_window_end: datetime | None = None
    freshest_quote_observed_at: datetime | None = None
    median_quote_observed_at: datetime | None = None
    oldest_quote_observed_at: datetime | None = None
    row_counts: dict[str, int] = field(default_factory=dict)
    provider_error_counts: dict[str, int] = field(default_factory=dict)
    job_statuses: dict[str, str] = field(default_factory=dict)
    file_size_bytes: int = 0
    sha256: str = ""
    validation_status: str = ValidationStatus.PENDING
    release_status: str = ReleaseStatus.HELD
    validation_failures: list[str] = field(default_factory=list)

    @property
    def deployable(self) -> bool:
        """The single question a deploy step asks.

        Both conditions are required. A validated artefact that was held back
        (because a provider failed, say) is not deployable either, and letting
        validation alone decide would publish it.
        """
        return (
            self.validation_status == ValidationStatus.PASSED
            and self.release_status == ReleaseStatus.PUBLISHED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "code_commit_sha": self.code_commit_sha,
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "parser_version": self.parser_version,
            "generated_at": iso(self.generated_at),
            "source_data_cutoff": iso(self.source_data_cutoff),
            "ingest_window_start": iso(self.ingest_window_start),
            "ingest_window_end": iso(self.ingest_window_end),
            "freshest_quote_observed_at": iso(self.freshest_quote_observed_at),
            "median_quote_observed_at": iso(self.median_quote_observed_at),
            "oldest_quote_observed_at": iso(self.oldest_quote_observed_at),
            "row_counts": self.row_counts,
            "provider_error_counts": self.provider_error_counts,
            "job_statuses": self.job_statuses,
            "file_size_bytes": self.file_size_bytes,
            "sha256": self.sha256,
            "validation_status": self.validation_status,
            "release_status": self.release_status,
            "validation_failures": self.validation_failures,
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text())


def verify_artifact(artifact: Path, manifest_path: Path) -> list[str]:
    """Check an artefact against its manifest. Empty list means it matches.

    Returns problems rather than raising: a deploy step wants to report every
    reason it refused, not the first one.
    """
    problems: list[str] = []
    if not artifact.exists():
        return [f"artifact missing: {artifact}"]
    if not manifest_path.exists():
        return [f"manifest missing: {manifest_path}"]

    manifest = SnapshotManifest.load(manifest_path)
    actual_size = artifact.stat().st_size
    if manifest.get("file_size_bytes") != actual_size:
        problems.append(
            f"size mismatch: manifest {manifest.get('file_size_bytes')} "
            f"vs actual {actual_size}"
        )
    actual_sha = sha256_of(artifact)
    if manifest.get("sha256") != actual_sha:
        problems.append(
            f"checksum mismatch: manifest {manifest.get('sha256', '')[:16]}... "
            f"vs actual {actual_sha[:16]}..."
        )
    if manifest.get("validation_status") != ValidationStatus.PASSED:
        problems.append(
            f"validation_status is {manifest.get('validation_status')!r}; "
            "an unvalidated artifact must never be deployed"
        )
    return problems
