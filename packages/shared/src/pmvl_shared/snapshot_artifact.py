"""Deterministic compressed Snapshot artefacts and safe runtime resolution.

The committed representation of a modern Snapshot is a deterministic gzip stream,
while SQLite and the API still consume the exact uncompressed database bytes.  This
module keeps those two identities explicit:

* ``compressed_sha256`` / ``compressed_size_bytes`` cover the committed gzip blob;
* ``uncompressed_sha256`` / ``uncompressed_size_bytes`` cover the SQLite database;
* the legacy ``sha256`` / ``file_size_bytes`` fields remain aliases for the
  uncompressed SQLite bytes.

Legacy manifests have no encoding fields and continue to resolve their raw database.
Once a manifest declares gzip, that declaration is authoritative: a missing, corrupt,
or mismatched gzip artefact fails closed and never falls back to a raw file.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import quote

_CHUNK_SIZE = 1 << 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{12}|[0-9a-f]{40})$")
_TERMINAL_JOB_STATUSES = frozenset(
    {"success", "partial_success", "failed", "skipped", "stale"}
)
_GZIP_FIELDS = frozenset(
    {
        "artifact_encoding",
        "compression_algorithm",
        "compression_level",
        "compression_deterministic",
        "compressed_path",
        "compressed_sha256",
        "uncompressed_sha256",
        "compressed_size_bytes",
        "uncompressed_size_bytes",
    }
)

# These are safety ceilings, not product limits.  They prevent a malformed manifest
# from turning one cold start into an unbounded decompression.
MAX_COMPRESSED_SIZE_BYTES = 128 * 1024 * 1024
MAX_UNCOMPRESSED_SIZE_BYTES = 512 * 1024 * 1024
CURRENT_SNAPSHOT_SCHEMA_REVISION = "c3d4e5f6a7b8"

ManifestInput: TypeAlias = Mapping[str, Any] | str | os.PathLike[str]

_resolution_lock = threading.RLock()
_resolution_cache: dict[tuple[str, str, str, str], Path] = {}


class SnapshotArtifactError(RuntimeError):
    """A Snapshot cannot be verified or safely resolved."""


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(value)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _same_bytes(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as lhs, right.open("rb") as rhs:
        while True:
            left_chunk = lhs.read(_CHUNK_SIZE)
            right_chunk = rhs.read(_CHUNK_SIZE)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _temporary_sibling(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    return Path(name)


def compress_snapshot(
    raw: str | os.PathLike[str],
    gzip_path: str | os.PathLike[str],
) -> Path:
    """Write the deterministic level-9 gzip representation of ``raw`` atomically.

    ``filename=""`` removes the source filename from the header and ``mtime=0``
    removes wall-clock time.  A temporary file is written beside the destination and
    atomically renamed only after the gzip stream is complete.
    """

    source = _path(raw)
    target = _path(gzip_path)
    if not source.is_file():
        raise SnapshotArtifactError(f"raw Snapshot is missing: {source}")
    if source.resolve() == target.resolve():
        raise SnapshotArtifactError("raw and gzip Snapshot paths must be different")

    temporary = _temporary_sibling(target)
    try:
        with temporary.open("wb") as destination:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=destination,
                mtime=0,
            ) as encoded:
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, encoded, length=_CHUNK_SIZE)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, SnapshotArtifactError):
            raise
        raise SnapshotArtifactError(f"could not compress Snapshot {source}: {exc}") from exc
    return target


def decompress_snapshot(
    gzip_path: str | os.PathLike[str],
    raw: str | os.PathLike[str],
) -> Path:
    """Decompress ``gzip_path`` to ``raw`` atomically.

    CRC and truncation errors are observed by reading through the end of the stream.
    A failed decode leaves an existing destination untouched.
    """

    source = _path(gzip_path)
    target = _path(raw)
    if not source.is_file():
        raise SnapshotArtifactError(f"compressed Snapshot is missing: {source}")
    if source.resolve() == target.resolve():
        raise SnapshotArtifactError("gzip and raw Snapshot paths must be different")

    temporary = _temporary_sibling(target)
    try:
        with temporary.open("wb") as destination:
            with source.open("rb") as compressed_handle:
                with gzip.GzipFile(
                    filename="",
                    mode="rb",
                    fileobj=compressed_handle,
                ) as decoded:
                    shutil.copyfileobj(decoded, destination, length=_CHUNK_SIZE)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise SnapshotArtifactError(
            f"could not decompress Snapshot {source}: {type(exc).__name__}: {exc}"
        ) from exc
    return target


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest field {key!r}")
        result[key] = value
    return result


def _load_manifest(manifest: ManifestInput) -> dict[str, Any]:
    if isinstance(manifest, Mapping):
        return dict(manifest)

    path = _path(manifest)
    try:
        parsed = json.loads(path.read_text(), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SnapshotArtifactError(f"manifest cannot be read at {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SnapshotArtifactError(f"manifest at {path} is not a JSON object")
    return parsed


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _valid_size(value: Any, *, maximum: int) -> bool:
    return type(value) is int and 0 < value <= maximum


def _common_manifest_problems(
    manifest: Mapping[str, Any],
    *,
    require_published: bool,
) -> list[str]:
    problems: list[str] = []
    if manifest.get("validation_status") != "passed":
        problems.append(
            f"validation_status is {manifest.get('validation_status')!r}; expected 'passed'"
        )
    release = manifest.get("release_status")
    allowed = {"published"} if require_published else {"held", "published"}
    if release not in allowed:
        expected = "'published'" if require_published else "'held' or 'published'"
        problems.append(f"release_status is {release!r}; expected {expected}")
    return problems


def _gzip_manifest_problems(
    manifest: Mapping[str, Any],
    *,
    require_published: bool,
) -> list[str]:
    problems = _common_manifest_problems(
        manifest,
        require_published=require_published,
    )
    expected_metadata = {
        "artifact_format": "sqlite",
        "artifact_encoding": "gzip",
        "compression_algorithm": "gzip",
        "compression_level": 9,
        "compression_deterministic": True,
    }
    for field, expected in expected_metadata.items():
        if manifest.get(field) != expected:
            problems.append(
                f"{field} is {manifest.get(field)!r}; expected {expected!r}"
            )
    declared_path = manifest.get("compressed_path")
    if declared_path != "data/pmvl-snapshot.db.gz":
        problems.append(
            "compressed_path must be exactly 'data/pmvl-snapshot.db.gz'"
        )

    for field in ("snapshot_id", "schema_revision", "built_at"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip() or value in {
            "unknown",
            "unversioned",
        }:
            problems.append(f"{field} must identify the built Snapshot")
    schema_revision = manifest.get("schema_revision")
    schema_version = manifest.get("schema_version")
    if schema_version and schema_revision != schema_version:
        problems.append(
            f"schema_revision {schema_revision!r} does not match "
            f"schema_version {schema_version!r}"
        )
    for field in ("code_commit_sha", "source_commit_sha"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _COMMIT_SHA_RE.fullmatch(value):
            problems.append(
                f"{field} must be a lowercase 12- or 40-character Git SHA"
            )
    if (
        _COMMIT_SHA_RE.fullmatch(str(manifest.get("code_commit_sha") or ""))
        and _COMMIT_SHA_RE.fullmatch(str(manifest.get("source_commit_sha") or ""))
        and manifest.get("code_commit_sha") != manifest.get("source_commit_sha")
    ):
        problems.append("source_commit_sha must equal code_commit_sha")
    job_statuses = manifest.get("job_statuses")
    if not isinstance(job_statuses, Mapping):
        problems.append("job_statuses must be an object")
    else:
        non_terminal = {
            str(name): status
            for name, status in job_statuses.items()
            if status not in _TERMINAL_JOB_STATUSES
        }
        if non_terminal:
            problems.append(
                "job_statuses contains non-terminal or unsupported values: "
                + ", ".join(
                    f"{name}={status!r}"
                    for name, status in sorted(non_terminal.items())
                )
            )

    for field in ("compressed_sha256", "uncompressed_sha256", "sha256"):
        if not _valid_sha(manifest.get(field)):
            problems.append(f"{field} must be a lowercase 64-character SHA-256")
    for field, maximum in (
        ("compressed_size_bytes", MAX_COMPRESSED_SIZE_BYTES),
        ("uncompressed_size_bytes", MAX_UNCOMPRESSED_SIZE_BYTES),
        ("file_size_bytes", MAX_UNCOMPRESSED_SIZE_BYTES),
    ):
        if not _valid_size(manifest.get(field), maximum=maximum):
            problems.append(f"{field} must be a positive integer no greater than {maximum}")

    uncompressed_sha = manifest.get("uncompressed_sha256")
    canonical_sha = manifest.get("sha256")
    if _valid_sha(uncompressed_sha) and _valid_sha(canonical_sha):
        if canonical_sha != uncompressed_sha:
            problems.append(
                "sha256 must remain the canonical uncompressed SQLite checksum"
            )
    uncompressed_size = manifest.get("uncompressed_size_bytes")
    canonical_size = manifest.get("file_size_bytes")
    if (
        _valid_size(uncompressed_size, maximum=MAX_UNCOMPRESSED_SIZE_BYTES)
        and _valid_size(canonical_size, maximum=MAX_UNCOMPRESSED_SIZE_BYTES)
        and canonical_size != uncompressed_size
    ):
        problems.append(
            "file_size_bytes must remain the canonical uncompressed SQLite size"
        )
    return problems


def _legacy_manifest_problems(
    manifest: Mapping[str, Any],
    *,
    require_published: bool,
) -> list[str]:
    problems = _common_manifest_problems(
        manifest,
        require_published=require_published,
    )
    if not _valid_sha(manifest.get("sha256")):
        problems.append("sha256 must be a lowercase 64-character SHA-256")
    if not _valid_size(
        manifest.get("file_size_bytes"),
        maximum=MAX_UNCOMPRESSED_SIZE_BYTES,
    ):
        problems.append(
            "file_size_bytes must be a positive integer no greater than "
            f"{MAX_UNCOMPRESSED_SIZE_BYTES}"
        )
    return problems


def _file_identity_problems(
    path: Path,
    *,
    expected_sha: Any,
    expected_size: Any,
    label: str,
) -> list[str]:
    if not path.is_file():
        return [f"{label} is missing: {path}"]
    problems: list[str] = []
    actual_size = path.stat().st_size
    if type(expected_size) is int and actual_size != expected_size:
        problems.append(
            f"{label} size mismatch: manifest {expected_size} vs actual {actual_size}"
        )
    if _valid_sha(expected_sha):
        actual_sha = _sha256_of(path)
        if actual_sha != expected_sha:
            problems.append(
                f"{label} checksum mismatch: manifest {expected_sha[:16]}... "
                f"vs actual {actual_sha[:16]}..."
            )
    return problems


def _sqlite_integrity_problems(path: Path) -> list[str]:
    """Verify SQLite without giving it permission to write journals or sidecars."""

    try:
        header = path.open("rb").read(20)
    except OSError as exc:
        return [f"uncompressed Snapshot cannot be read: {exc}"]
    if len(header) < 20:
        return ["uncompressed Snapshot header is truncated"]
    if not header.startswith(b"SQLite format 3\x00"):
        return ["uncompressed Snapshot is not a SQLite 3 database"]
    if header[18] == 2 or header[19] == 2:
        return [
            "uncompressed Snapshot is in WAL mode and is not self-contained"
        ]

    encoded_path = quote(str(path.resolve()), safe="/")
    uri = f"file:{encoded_path}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return [f"SQLite integrity_check could not run: {exc}"]

    problems: list[str] = []
    if rows != ["ok"]:
        summary = "; ".join(str(row) for row in rows[:5])
        problems.append(f"SQLite integrity_check failed: {summary or 'no result'}")
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            problems.append(f"SQLite verification found an unexpected sidecar: {sidecar}")
    return problems


def _sqlite_manifest_contract_problems(
    path: Path,
    manifest: Mapping[str, Any],
) -> list[str]:
    """Bind manifest schema and terminal job claims to the verified SQLite bytes."""

    encoded_path = quote(str(path.resolve()), safe="/")
    uri = f"file:{encoded_path}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            schema_rows = [
                str(row[0])
                for row in connection.execute(
                    "SELECT version_num FROM alembic_version"
                )
            ]
            latest_jobs = {
                str(name): str(status)
                for name, status in connection.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            job_name,
                            status,
                            ROW_NUMBER() OVER (
                                PARTITION BY job_name
                                ORDER BY started_at DESC, id DESC
                            ) AS position
                        FROM job_runs
                    )
                    SELECT job_name, status
                    FROM ranked
                    WHERE position = 1
                    ORDER BY job_name
                    """
                )
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return [f"SQLite manifest contract could not be checked: {exc}"]

    problems: list[str] = []
    if schema_rows != [CURRENT_SNAPSHOT_SCHEMA_REVISION]:
        problems.append(
            "SQLite schema revision is unsupported: "
            f"{schema_rows!r}; expected [{CURRENT_SNAPSHOT_SCHEMA_REVISION!r}]"
        )
    manifest_revision = manifest.get("schema_revision")
    if schema_rows and manifest_revision != schema_rows[0]:
        problems.append(
            f"manifest schema_revision {manifest_revision!r} does not match "
            f"SQLite alembic_version {schema_rows[0]!r}"
        )

    non_terminal = {
        name: status
        for name, status in latest_jobs.items()
        if status not in _TERMINAL_JOB_STATUSES
    }
    if non_terminal:
        problems.append(
            "SQLite job_runs contains non-terminal or unsupported latest statuses: "
            + ", ".join(
                f"{name}={status!r}"
                for name, status in sorted(non_terminal.items())
            )
        )
    manifest_jobs = manifest.get("job_statuses")
    if isinstance(manifest_jobs, Mapping):
        normalized_manifest_jobs = {
            str(name): str(status)
            for name, status in manifest_jobs.items()
        }
        if normalized_manifest_jobs != latest_jobs:
            problems.append(
                "manifest job_statuses do not match the latest SQLite job_runs: "
                f"manifest={normalized_manifest_jobs!r}, SQLite={latest_jobs!r}"
            )
    return problems


def _gzip_matches_raw(gzip_path: Path, raw: Path) -> list[str]:
    try:
        with gzip_path.open("rb") as compressed_handle:
            with gzip.GzipFile(
                filename="",
                mode="rb",
                fileobj=compressed_handle,
            ) as decoded:
                with raw.open("rb") as expected:
                    while True:
                        decoded_chunk = decoded.read(_CHUNK_SIZE)
                        expected_chunk = expected.read(_CHUNK_SIZE)
                        if decoded_chunk != expected_chunk:
                            return [
                                "decompressed gzip bytes are not byte-identical to "
                                "the raw Snapshot"
                            ]
                        if not decoded_chunk:
                            return []
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        return [
            f"gzip decompression failed: {type(exc).__name__}: {exc}"
        ]


def _deterministic_gzip_problems(raw: Path, gzip_path: Path) -> list[str]:
    try:
        with tempfile.TemporaryDirectory(prefix="pmvl-gzip-verify-") as directory:
            reproduced = Path(directory) / "reproduced.db.gz"
            compress_snapshot(raw, reproduced)
            if not _same_bytes(reproduced, gzip_path):
                return [
                    "gzip bytes do not match the deterministic level-9 encoding "
                    "(filename='', mtime=0)"
                ]
    except (OSError, SnapshotArtifactError) as exc:
        return [f"deterministic gzip verification failed: {exc}"]
    return []


def verify_compressed_snapshot(
    raw: str | os.PathLike[str],
    gzip_path: str | os.PathLike[str],
    manifest: ManifestInput,
) -> list[str]:
    """Verify both identities and the exact raw↔gzip relationship.

    Problems are accumulated and returned so a CI or publish job can report every
    refusal in one pass.  Candidate manifests may be ``held``; runtime resolution is
    stricter and requires ``published``.
    """

    raw_path = _path(raw)
    encoded_path = _path(gzip_path)
    try:
        data = _load_manifest(manifest)
    except SnapshotArtifactError as exc:
        return [str(exc)]

    problems = _gzip_manifest_problems(data, require_published=False)
    compressed_identity = _file_identity_problems(
        encoded_path,
        expected_sha=data.get("compressed_sha256"),
        expected_size=data.get("compressed_size_bytes"),
        label="compressed Snapshot",
    )
    raw_identity = _file_identity_problems(
        raw_path,
        expected_sha=data.get("uncompressed_sha256"),
        expected_size=data.get("uncompressed_size_bytes"),
        label="uncompressed Snapshot",
    )
    problems.extend(compressed_identity)
    problems.extend(raw_identity)

    if raw_path.is_file():
        integrity_problems = _sqlite_integrity_problems(raw_path)
        problems.extend(integrity_problems)
        if not integrity_problems:
            problems.extend(_sqlite_manifest_contract_problems(raw_path, data))
    if raw_path.is_file() and encoded_path.is_file() and not compressed_identity:
        problems.extend(_gzip_matches_raw(encoded_path, raw_path))
        problems.extend(_deterministic_gzip_problems(raw_path, encoded_path))
    return problems


def _manifest_declares_gzip(manifest: Mapping[str, Any]) -> bool:
    encoding = manifest.get("artifact_encoding")
    if encoding == "gzip":
        return True
    if encoding in {"raw", "identity", "none"}:
        contradictory = {
            field: manifest.get(field)
            for field in (
                "compression_algorithm",
                "compression_level",
                "compression_deterministic",
                "compressed_path",
                "compressed_sha256",
                "compressed_size_bytes",
            )
            if manifest.get(field) not in (None, "", 0, False)
        }
        if contradictory:
            fields = ", ".join(sorted(contradictory))
            raise SnapshotArtifactError(
                f"raw Snapshot manifest contains contradictory compression fields: {fields}"
            )
        return False
    if encoding is not None:
        raise SnapshotArtifactError(
            f"unsupported artifact_encoding {encoding!r}; expected 'gzip', 'raw', "
            "'identity', 'none', or a legacy manifest with no encoding fields"
        )
    declared = sorted(field for field in _GZIP_FIELDS - {"artifact_encoding"} if field in manifest)
    if declared:
        raise SnapshotArtifactError(
            "manifest contains gzip fields but does not declare "
            f"artifact_encoding='gzip': {', '.join(declared)}"
        )
    return False


def _resolve_declared_compressed_path(
    manifest_path: Path,
    legacy_raw_path: Path,
    declared: Any,
) -> Path:
    if not isinstance(declared, str) or not declared.strip():
        raise SnapshotArtifactError("compressed_path must be a non-empty relative path")
    relative = Path(declared)
    if relative.is_absolute():
        raise SnapshotArtifactError("compressed_path must be relative to the repository")

    if relative.parts and relative.parts[0] == legacy_raw_path.parent.name:
        anchor = legacy_raw_path.parent.parent
    else:
        anchor = manifest_path.parent
    resolved_anchor = anchor.resolve()
    resolved = (anchor / relative).resolve()
    try:
        resolved.relative_to(resolved_anchor)
    except ValueError as exc:
        raise SnapshotArtifactError(
            f"compressed_path escapes its allowed root: {declared!r}"
        ) from exc
    return resolved


def _runtime_temp_root(
    requested: str | os.PathLike[str] | None,
    *,
    legacy_raw_path: Path,
) -> Path:
    requested_root = (
        _path(requested)
        if requested is not None
        else Path(tempfile.gettempdir()) / "pmvl-snapshots"
    )
    if requested_root.is_symlink():
        raise SnapshotArtifactError(
            f"runtime Snapshot temp root must not be a symlink: {requested_root}"
        )
    root = requested_root.resolve()

    # Reject a checkout-contained target before mkdir/chmod. "Never write into the
    # repository" includes refusing to create an otherwise empty cache directory.
    if legacy_raw_path.parent.name == "data":
        repository_root = legacy_raw_path.parent.parent.resolve()
        try:
            root.relative_to(repository_root)
        except ValueError:
            pass
        else:
            raise SnapshotArtifactError(
                f"runtime Snapshot temp root must be outside the repository: {root}"
            )
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise SnapshotArtifactError(
            f"runtime Snapshot temp root cannot be created: {root}: {exc}"
        ) from exc
    try:
        root_stat = root.stat()
    except OSError as exc:
        raise SnapshotArtifactError(
            f"runtime Snapshot temp root cannot be inspected: {root}: {exc}"
        ) from exc
    if not root.is_dir():
        raise SnapshotArtifactError(
            f"runtime Snapshot temp root is not a directory: {root}"
        )
    if hasattr(os, "getuid") and root_stat.st_uid != os.getuid():
        raise SnapshotArtifactError(
            f"runtime Snapshot temp root is not owned by this process user: {root}"
        )
    try:
        os.chmod(root, 0o700)
    except OSError as exc:
        raise SnapshotArtifactError(
            f"runtime Snapshot temp root permissions cannot be secured: {root}: {exc}"
        ) from exc

    return root


def _decompress_verified_to_staging(
    gzip_path: Path,
    staging: Path,
    *,
    expected_sha: str,
    expected_size: int,
) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        with gzip_path.open("rb") as compressed_handle:
            with gzip.GzipFile(
                filename="",
                mode="rb",
                fileobj=compressed_handle,
            ) as decoded:
                with staging.open("wb") as output:
                    while chunk := decoded.read(_CHUNK_SIZE):
                        total += len(chunk)
                        if total > expected_size:
                            raise SnapshotArtifactError(
                                "decompressed Snapshot exceeds uncompressed_size_bytes"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
    except SnapshotArtifactError:
        raise
    except Exception as exc:
        raise SnapshotArtifactError(
            f"gzip decompression failed: {type(exc).__name__}: {exc}"
        ) from exc

    if total != expected_size:
        raise SnapshotArtifactError(
            f"uncompressed Snapshot size mismatch: manifest {expected_size} vs actual {total}"
        )
    actual_sha = digest.hexdigest()
    if actual_sha != expected_sha:
        raise SnapshotArtifactError(
            f"uncompressed Snapshot checksum mismatch: manifest "
            f"{expected_sha[:16]}... vs actual {actual_sha[:16]}..."
        )


def _raise_for(problems: list[str], *, prefix: str) -> None:
    if problems:
        raise SnapshotArtifactError(prefix + ": " + "; ".join(problems))


def materialize_verified_compressed_snapshot(
    gzip_path: str | os.PathLike[str],
    raw_target: str | os.PathLike[str],
    manifest: ManifestInput,
) -> Path:
    """Boundedly materialize and verify a held or published gzip Snapshot.

    This is the standalone verifier's safe boundary. Manifest ceilings and the
    compressed identity are checked before decompression, and the stream cannot
    write more than the declared uncompressed size.
    """

    encoded_path = _path(gzip_path)
    target = _path(raw_target)
    data = _load_manifest(manifest)
    contract_problems = _gzip_manifest_problems(data, require_published=False)
    _raise_for(contract_problems, prefix="compressed Snapshot manifest is invalid")
    compressed_problems = _file_identity_problems(
        encoded_path,
        expected_sha=data["compressed_sha256"],
        expected_size=data["compressed_size_bytes"],
        label="compressed Snapshot",
    )
    _raise_for(compressed_problems, prefix="compressed Snapshot verification failed")

    temporary = _temporary_sibling(target)
    try:
        _decompress_verified_to_staging(
            encoded_path,
            temporary,
            expected_sha=data["uncompressed_sha256"],
            expected_size=data["uncompressed_size_bytes"],
        )
        verification_problems = _sqlite_integrity_problems(temporary)
        verification_problems.extend(
            _sqlite_manifest_contract_problems(temporary, data)
        )
        verification_problems.extend(
            _deterministic_gzip_problems(temporary, encoded_path)
        )
        _raise_for(
            verification_problems,
            prefix="decompressed Snapshot verification failed",
        )
        os.chmod(temporary, 0o400)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def resolve_snapshot_path(
    manifest_path: str | os.PathLike[str],
    legacy_raw_path: str | os.PathLike[str],
    compressed_path: str | os.PathLike[str] | None = None,
    temp_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve a published Snapshot to a verified read-only SQLite path.

    Legacy manifests return their checked raw path.  Gzip manifests are verified,
    decompressed to ``<temp_root>/<uncompressed_sha256>.db`` through a unique staging
    file, integrity-checked read-only, and atomically installed.  Concurrent cold
    starts may do duplicate work, but no caller can observe a partial final file.
    """

    manifest_file = _path(manifest_path)
    legacy_path = _path(legacy_raw_path)
    data = _load_manifest(manifest_file)

    if not _manifest_declares_gzip(data):
        problems = _legacy_manifest_problems(data, require_published=True)
        problems.extend(
            _file_identity_problems(
                legacy_path,
                expected_sha=data.get("sha256"),
                expected_size=data.get("file_size_bytes"),
                label="legacy raw Snapshot",
            )
        )
        if legacy_path.is_file():
            problems.extend(_sqlite_integrity_problems(legacy_path))
        _raise_for(problems, prefix="legacy Snapshot verification failed")
        return legacy_path

    contract_problems = _gzip_manifest_problems(data, require_published=True)
    _raise_for(contract_problems, prefix="compressed Snapshot manifest is invalid")

    declared_encoded_path = _resolve_declared_compressed_path(
        manifest_file,
        legacy_path,
        data.get("compressed_path"),
    )
    encoded_path = declared_encoded_path
    if compressed_path is not None:
        supplied_encoded_path = _path(compressed_path).resolve()
        if supplied_encoded_path != declared_encoded_path:
            raise SnapshotArtifactError(
                "explicit compressed Snapshot path disagrees with the manifest: "
                f"{supplied_encoded_path} vs {declared_encoded_path}"
            )
        encoded_path = supplied_encoded_path
    compressed_problems = _file_identity_problems(
        encoded_path,
        expected_sha=data["compressed_sha256"],
        expected_size=data["compressed_size_bytes"],
        label="compressed Snapshot",
    )
    _raise_for(compressed_problems, prefix="compressed Snapshot verification failed")

    cache_root = _runtime_temp_root(temp_root, legacy_raw_path=legacy_path)
    final_path = cache_root / f"{data['uncompressed_sha256']}.db"
    key = (
        str(manifest_file.resolve()),
        data["compressed_sha256"],
        data["uncompressed_sha256"],
        str(cache_root),
    )

    with _resolution_lock:
        cached = _resolution_cache.get(key)
        candidate = cached if cached == final_path else final_path
        if candidate.is_symlink():
            raise SnapshotArtifactError(
                f"cached uncompressed Snapshot must not be a symlink: {candidate}"
            )
        if candidate.is_file():
            if candidate.stat().st_nlink != 1:
                raise SnapshotArtifactError(
                    "cached uncompressed Snapshot must not have multiple hard links: "
                    f"{candidate}"
                )
            existing_problems = _file_identity_problems(
                candidate,
                expected_sha=data["uncompressed_sha256"],
                expected_size=data["uncompressed_size_bytes"],
                label="cached uncompressed Snapshot",
            )
            existing_problems.extend(_sqlite_integrity_problems(candidate))
            existing_problems.extend(
                _sqlite_manifest_contract_problems(candidate, data)
            )
            if not existing_problems:
                os.chmod(candidate, 0o400)
                _resolution_cache[key] = candidate
                return candidate

        descriptor, staging_name = tempfile.mkstemp(
            prefix=f".{data['uncompressed_sha256']}.",
            suffix=".tmp",
            dir=cache_root,
        )
        os.close(descriptor)
        staging = Path(staging_name)
        try:
            _decompress_verified_to_staging(
                encoded_path,
                staging,
                expected_sha=data["uncompressed_sha256"],
                expected_size=data["uncompressed_size_bytes"],
            )
            integrity_problems = _sqlite_integrity_problems(staging)
            integrity_problems.extend(
                _sqlite_manifest_contract_problems(staging, data)
            )
            _raise_for(
                integrity_problems,
                prefix="decompressed Snapshot verification failed",
            )
            os.chmod(staging, 0o400)
            os.replace(staging, final_path)
        finally:
            staging.unlink(missing_ok=True)

        _resolution_cache[key] = final_path
        return final_path


def clear_snapshot_resolution_cache() -> None:
    """Forget process-local resolution results without deleting verified temp files."""

    with _resolution_lock:
        _resolution_cache.clear()


__all__ = [
    "CURRENT_SNAPSHOT_SCHEMA_REVISION",
    "MAX_COMPRESSED_SIZE_BYTES",
    "MAX_UNCOMPRESSED_SIZE_BYTES",
    "SnapshotArtifactError",
    "clear_snapshot_resolution_cache",
    "compress_snapshot",
    "decompress_snapshot",
    "materialize_verified_compressed_snapshot",
    "resolve_snapshot_path",
    "verify_compressed_snapshot",
]
