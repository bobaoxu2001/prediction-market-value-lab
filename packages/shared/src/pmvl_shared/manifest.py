"""What a published snapshot has to declare about itself.

The artefact was a bare 8 MB binary. Nothing travelling with it said which commit
produced it, which schema it matched, what input cutoff it saw, or whether it had
been validated - so "is the deployed snapshot the one we think it is" could only
be answered by opening it and guessing.

Three of the four production outages traced to that gap: a snapshot missing from
the bundle, one in the wrong journal mode, and one whose schema predated a
migration. Each was invisible until a request failed.

The manifest is written next to the artefact and is the thing a deploy checks. It
records both the canonical SQLite identity and, for compressed publication, the
gzip transport identity, so a truncated or partially-written file fails
verification rather than being served.
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
    # ``sha256`` and ``file_size_bytes`` remain the canonical identity of the
    # uncompressed SQLite bytes for backward compatibility.  The fields below
    # make the committed transport artefact explicit without changing snapshot
    # identity merely because gzip implementation details change.
    artifact_format: str = "sqlite"
    artifact_encoding: str = "raw"
    compression_algorithm: str = ""
    compression_level: int = 0
    compression_deterministic: bool = False
    compressed_path: str = ""
    compressed_sha256: str = ""
    uncompressed_sha256: str = ""
    compressed_size_bytes: int = 0
    uncompressed_size_bytes: int = 0
    schema_revision: str = ""
    source_commit_sha: str = ""
    pipeline_run_id: str = ""
    workflow_run_id: str = ""
    workflow_run_attempt: str = ""
    built_at: datetime | None = None

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
            "artifact_format": self.artifact_format,
            "artifact_encoding": self.artifact_encoding,
            "compression_algorithm": self.compression_algorithm,
            "compression_level": self.compression_level,
            "compression_deterministic": self.compression_deterministic,
            "compressed_path": self.compressed_path,
            "compressed_sha256": self.compressed_sha256,
            "uncompressed_sha256": self.uncompressed_sha256 or self.sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "uncompressed_size_bytes": self.uncompressed_size_bytes
            or self.file_size_bytes,
            "schema_revision": self.schema_revision or self.schema_version,
            "source_commit_sha": self.source_commit_sha or self.code_commit_sha,
            "pipeline_run_id": self.pipeline_run_id,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_attempt": self.workflow_run_attempt,
            "built_at": iso(self.built_at or self.generated_at),
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text())


#: Values that mean "nobody recorded this". Acceptable on the legacy artefact that
#: predates manifests; never on anything newly generated, because a manifest whose
#: whole job is provenance must not itself be unattributable.
PLACEHOLDERS = frozenset({"", "unknown", "unversioned", None})

#: Fields a newly generated artefact must have real values for.
REQUIRED_PROVENANCE = (
    "code_commit_sha",
    "schema_version",
    "model_version",
    "parser_version",
    "snapshot_id",
    "generated_at",
)


def provenance_problems(manifest: dict[str, Any], *, legacy_exempt: bool = False) -> list[str]:
    """Fields a newly generated manifest may not leave unattributed.

    The committed artefact carries ``code_commit_sha: unknown`` and
    ``parser_version: unversioned`` because it was built before either was
    recorded. That is tolerable for a rollback artefact whose provenance gap is
    documented, and intolerable for anything generated from now on: a manifest
    exists to say which code produced which bytes, and one that cannot is a
    reassuring-looking null.
    """
    if legacy_exempt:
        return []
    problems = []
    for name in REQUIRED_PROVENANCE:
        value = manifest.get(name)
        if value in PLACEHOLDERS or (
            isinstance(value, str) and value.strip().lower() in PLACEHOLDERS
        ):
            problems.append(f"{name} is {value!r}; newly generated artifacts need a real value")
        elif name == "snapshot_id" and str(value).startswith("unknown-"):
            problems.append(
                f"snapshot_id {value!r} is derived from an unknown commit"
            )
    return problems


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
    compressed = (
        manifest.get("artifact_encoding") == "gzip" and artifact.suffix == ".gz"
    )
    expected_size = (
        manifest.get("compressed_size_bytes")
        if compressed
        else manifest.get("uncompressed_size_bytes", manifest.get("file_size_bytes"))
    )
    if expected_size in (None, 0) and not compressed:
        expected_size = manifest.get("file_size_bytes")
    actual_size = artifact.stat().st_size
    if expected_size != actual_size:
        problems.append(
            f"size mismatch: manifest {expected_size} "
            f"vs actual {actual_size}"
        )
    actual_sha = sha256_of(artifact)
    expected_sha = (
        manifest.get("compressed_sha256")
        if compressed
        else manifest.get("uncompressed_sha256") or manifest.get("sha256")
    )
    if expected_sha != actual_sha:
        problems.append(
            f"checksum mismatch: manifest {str(expected_sha or '')[:16]}... "
            f"vs actual {actual_sha[:16]}..."
        )
    if manifest.get("validation_status") != ValidationStatus.PASSED:
        problems.append(
            f"validation_status is {manifest.get('validation_status')!r}; "
            "an unvalidated artifact must never be deployed"
        )
    return problems
