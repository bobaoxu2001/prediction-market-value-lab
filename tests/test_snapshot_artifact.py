"""Focused tests for the compressed Snapshot artefact boundary."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pmvl_shared.snapshot_artifact import (
    MAX_UNCOMPRESSED_SIZE_BYTES,
    SnapshotArtifactError,
    clear_snapshot_resolution_cache,
    compress_snapshot,
    decompress_snapshot,
    materialize_verified_compressed_snapshot,
    resolve_snapshot_path,
    verify_compressed_snapshot,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(
    path: Path,
    *,
    value: str = "AC" + "1" * 32,
    schema_revision: str = "c3d4e5f6a7b8",
    job_status: str = "success",
) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records(value) VALUES (?)", (value,))
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (schema_revision,),
        )
        connection.execute(
            "CREATE TABLE job_runs ("
            "id INTEGER PRIMARY KEY, job_name TEXT, status TEXT, started_at TEXT"
            ")"
        )
        connection.execute(
            "INSERT INTO job_runs(id, job_name, status, started_at) "
            "VALUES (1, 'ingest', ?, '2026-07-31T08:00:00Z')",
            (job_status,),
        )
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    finally:
        connection.close()
    return path


def _compressed_bundle(
    directory: Path,
    *,
    value: str = "AC" + "1" * 32,
    release_status: str = "published",
    schema_revision: str = "c3d4e5f6a7b8",
    job_status: str = "success",
) -> tuple[Path, Path, Path, dict]:
    data = directory / "data"
    data.mkdir(parents=True)
    raw = _database(
        directory / "candidate.db",
        value=value,
        schema_revision=schema_revision,
        job_status=job_status,
    )
    encoded = data / "pmvl-snapshot.db.gz"
    compress_snapshot(raw, encoded)
    manifest = {
        "artifact_format": "sqlite",
        "artifact_encoding": "gzip",
        "compression_algorithm": "gzip",
        "compression_level": 9,
        "compression_deterministic": True,
        "compressed_path": "data/pmvl-snapshot.db.gz",
        "compressed_sha256": _sha(encoded),
        "uncompressed_sha256": _sha(raw),
        "compressed_size_bytes": encoded.stat().st_size,
        "uncompressed_size_bytes": raw.stat().st_size,
        # Backward-compatible canonical identity is the SQLite, not gzip, identity.
        "sha256": _sha(raw),
        "file_size_bytes": raw.stat().st_size,
        "snapshot_id": f"snapshot-{_sha(raw)[:16]}",
        "schema_version": schema_revision,
        "schema_revision": schema_revision,
        "code_commit_sha": "1" * 12,
        "source_commit_sha": "1" * 12,
        "built_at": "2026-07-31T08:00:00Z",
        "job_statuses": {"ingest": "success"},
        "validation_status": "passed",
        "release_status": release_status,
    }
    manifest_path = data / "pmvl-snapshot.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return raw, encoded, manifest_path, manifest


@pytest.fixture(autouse=True)
def _clear_resolution_cache():  # noqa: ANN202
    clear_snapshot_resolution_cache()
    yield
    clear_snapshot_resolution_cache()


class TestDeterministicGzip:
    def test_same_input_produces_byte_identical_gzip(self, tmp_path: Path) -> None:
        raw = _database(tmp_path / "snapshot.db")
        first = compress_snapshot(raw, tmp_path / "first.db.gz")
        second = compress_snapshot(raw, tmp_path / "second.db.gz")

        assert first.read_bytes() == second.read_bytes()
        header = first.read_bytes()[:10]
        assert int.from_bytes(header[4:8], "little") == 0
        assert header[3] & 0x08 == 0, "the gzip header leaked a source filename"

    def test_round_trip_preserves_exact_sqlite_bytes(self, tmp_path: Path) -> None:
        raw = _database(tmp_path / "snapshot.db")
        encoded = compress_snapshot(raw, tmp_path / "snapshot.db.gz")
        restored = decompress_snapshot(encoded, tmp_path / "restored.db")

        assert restored.read_bytes() == raw.read_bytes()
        connection = sqlite3.connect(f"file:{restored}?mode=ro&immutable=1", uri=True)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            connection.close()

    def test_different_input_changes_both_uncompressed_and_compressed_hashes(
        self, tmp_path: Path
    ) -> None:
        first_raw = _database(tmp_path / "one.db", value="one")
        second_raw = _database(tmp_path / "two.db", value="two")
        first_gzip = compress_snapshot(first_raw, tmp_path / "one.db.gz")
        second_gzip = compress_snapshot(second_raw, tmp_path / "two.db.gz")

        assert _sha(first_raw) != _sha(second_raw)
        assert _sha(first_gzip) != _sha(second_gzip)

    def test_failed_decompression_does_not_replace_existing_target(
        self, tmp_path: Path
    ) -> None:
        corrupt = tmp_path / "corrupt.db.gz"
        corrupt.write_bytes(b"not gzip")
        target = tmp_path / "target.db"
        target.write_bytes(b"known-good")

        with pytest.raises(SnapshotArtifactError, match="decompress"):
            decompress_snapshot(corrupt, target)
        assert target.read_bytes() == b"known-good"


class TestDualIdentityVerification:
    def test_valid_bundle_checks_both_hashes_and_exact_bytes(self, tmp_path: Path) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        assert verify_compressed_snapshot(raw, encoded, manifest) == []

    def test_compressed_checksum_mismatch_fails(self, tmp_path: Path) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest["compressed_sha256"] = "0" * 64

        problems = verify_compressed_snapshot(raw, encoded, manifest)
        assert any("compressed Snapshot checksum mismatch" in problem for problem in problems)

    def test_uncompressed_checksum_mismatch_fails(self, tmp_path: Path) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest["uncompressed_sha256"] = "0" * 64
        manifest["sha256"] = "0" * 64

        problems = verify_compressed_snapshot(raw, encoded, manifest)
        assert any("uncompressed Snapshot checksum mismatch" in problem for problem in problems)

    def test_canonical_legacy_checksum_cannot_be_redefined_as_gzip(
        self, tmp_path: Path
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest["sha256"] = manifest["compressed_sha256"]
        manifest["file_size_bytes"] = manifest["compressed_size_bytes"]

        problems = verify_compressed_snapshot(raw, encoded, manifest)
        assert any("canonical uncompressed SQLite checksum" in problem for problem in problems)
        assert any("canonical uncompressed SQLite size" in problem for problem in problems)

    def test_nondeterministic_header_is_rejected_even_when_hashes_match(
        self, tmp_path: Path
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        with encoded.open("wb") as output:
            with gzip.GzipFile(
                filename="leaked-name.db",
                mode="wb",
                compresslevel=9,
                fileobj=output,
                mtime=123,
            ) as stream:
                stream.write(raw.read_bytes())
        manifest["compressed_sha256"] = _sha(encoded)
        manifest["compressed_size_bytes"] = encoded.stat().st_size

        problems = verify_compressed_snapshot(raw, encoded, manifest)
        assert any("deterministic level-9 encoding" in problem for problem in problems)

    def test_corrupt_sqlite_fails_even_when_all_hashes_match(self, tmp_path: Path) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        raw.write_bytes(b"not a sqlite database")
        compress_snapshot(raw, encoded)
        manifest.update(
            {
                "compressed_sha256": _sha(encoded),
                "uncompressed_sha256": _sha(raw),
                "sha256": _sha(raw),
                "compressed_size_bytes": encoded.stat().st_size,
                "uncompressed_size_bytes": raw.stat().st_size,
                "file_size_bytes": raw.stat().st_size,
            }
        )

        problems = verify_compressed_snapshot(raw, encoded, manifest)
        assert any("not a SQLite 3 database" in problem for problem in problems)

    def test_non_terminal_manifest_job_status_fails_closed(
        self, tmp_path: Path
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest["job_statuses"] = {"ingest": "success", "settle": "running"}

        problems = verify_compressed_snapshot(raw, encoded, manifest)

        assert any("non-terminal" in problem for problem in problems)

    def test_non_terminal_sqlite_job_cannot_be_hidden_by_the_manifest(
        self, tmp_path: Path
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(
            tmp_path,
            job_status="running",
        )

        problems = verify_compressed_snapshot(raw, encoded, manifest)

        assert any("SQLite job_runs contains non-terminal" in problem for problem in problems)
        assert any("manifest job_statuses do not match" in problem for problem in problems)

    @pytest.mark.parametrize(
        "declared",
        (
            "../outside.db.gz",
            "pmvl-snapshot.db.gz",
            "data/another.db.gz",
            "/tmp/pmvl-snapshot.db.gz",
        ),
    )
    def test_compressed_path_must_name_the_exact_publication_target(
        self,
        tmp_path: Path,
        declared: str,
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest["compressed_path"] = declared

        problems = verify_compressed_snapshot(raw, encoded, manifest)

        assert any("must be exactly" in problem for problem in problems)

    def test_schema_revision_must_match_the_database_manifest_version(
        self, tmp_path: Path
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest["schema_revision"] = "outdated"

        problems = verify_compressed_snapshot(raw, encoded, manifest)

        assert any("does not match schema_version" in problem for problem in problems)

    def test_manifest_and_database_cannot_agree_on_an_unsupported_revision(
        self, tmp_path: Path
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(
            tmp_path,
            schema_revision="outdated1234",
        )

        problems = verify_compressed_snapshot(raw, encoded, manifest)

        assert any("schema revision is unsupported" in problem for problem in problems)

    @pytest.mark.parametrize(
        "field,value",
        (
            ("code_commit_sha", "a"),
            ("source_commit_sha", "not-a-sha"),
            ("source_commit_sha", "2" * 12),
        ),
    )
    def test_commit_provenance_is_full_strength_and_consistent(
        self,
        tmp_path: Path,
        field: str,
        value: str,
    ) -> None:
        raw, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest[field] = value

        problems = verify_compressed_snapshot(raw, encoded, manifest)

        assert any(field in problem or "must equal" in problem for problem in problems)

    def test_standalone_materialization_rejects_oversize_before_writing(
        self, tmp_path: Path
    ) -> None:
        _, encoded, _, manifest = _compressed_bundle(tmp_path)
        manifest["uncompressed_size_bytes"] = MAX_UNCOMPRESSED_SIZE_BYTES + 1
        manifest["file_size_bytes"] = MAX_UNCOMPRESSED_SIZE_BYTES + 1
        target = tmp_path / "materialized.db"

        with pytest.raises(SnapshotArtifactError, match="no greater than"):
            materialize_verified_compressed_snapshot(
                encoded,
                target,
                manifest,
            )

        assert not target.exists()


class TestLegacyResolution:
    def test_existing_raw_manifest_resolves_without_writing_a_temp_file(
        self, tmp_path: Path
    ) -> None:
        raw = _database(tmp_path / "pmvl-snapshot.db")
        manifest = tmp_path / "pmvl-snapshot.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "sha256": _sha(raw),
                    "file_size_bytes": raw.stat().st_size,
                    "validation_status": "passed",
                    "release_status": "published",
                }
            )
        )
        temp_root = tmp_path / "runtime-cache"

        assert resolve_snapshot_path(manifest, raw, temp_root=temp_root) == raw
        assert not temp_root.exists(), "legacy resolution unexpectedly wrote a cache"

    def test_explicit_raw_encoding_resolves_with_dual_raw_aliases(
        self, tmp_path: Path
    ) -> None:
        raw = _database(tmp_path / "pmvl-snapshot.db")
        manifest = tmp_path / "pmvl-snapshot.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "artifact_format": "sqlite",
                    "artifact_encoding": "raw",
                    "uncompressed_sha256": _sha(raw),
                    "uncompressed_size_bytes": raw.stat().st_size,
                    "sha256": _sha(raw),
                    "file_size_bytes": raw.stat().st_size,
                    "validation_status": "passed",
                    "release_status": "published",
                }
            )
        )

        assert resolve_snapshot_path(manifest, raw) == raw

    def test_legacy_checksum_mismatch_fails_closed(self, tmp_path: Path) -> None:
        raw = _database(tmp_path / "pmvl-snapshot.db")
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "sha256": "0" * 64,
                    "file_size_bytes": raw.stat().st_size,
                    "validation_status": "passed",
                    "release_status": "published",
                }
            )
        )

        with pytest.raises(SnapshotArtifactError, match="checksum mismatch"):
            resolve_snapshot_path(manifest, raw)

    def test_gzip_fields_without_encoding_do_not_fall_back_to_raw(
        self, tmp_path: Path
    ) -> None:
        raw = _database(tmp_path / "pmvl-snapshot.db")
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "sha256": _sha(raw),
                    "file_size_bytes": raw.stat().st_size,
                    "compressed_sha256": "0" * 64,
                    "validation_status": "passed",
                    "release_status": "published",
                }
            )
        )

        with pytest.raises(SnapshotArtifactError, match="does not declare"):
            resolve_snapshot_path(manifest, raw)


class TestCompressedRuntimeResolution:
    def test_manifest_authoritatively_resolves_into_safe_temp_root(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        raw, _, manifest, data = _compressed_bundle(repository)
        legacy = repository / "data" / "pmvl-snapshot.db"
        legacy.write_bytes(b"a raw fallback must not be used")
        runtime = tmp_path / "runtime"
        before = {
            path.relative_to(repository)
            for path in repository.rglob("*")
            if path.is_file()
        }

        resolved = resolve_snapshot_path(manifest, legacy, temp_root=runtime)

        assert resolved == runtime / f"{data['uncompressed_sha256']}.db"
        assert resolved.read_bytes() == raw.read_bytes()
        assert resolved.stat().st_mode & 0o222 == 0
        after = {
            path.relative_to(repository)
            for path in repository.rglob("*")
            if path.is_file()
        }
        assert after == before, "runtime resolution wrote into the repository"
        assert not resolved.with_name(resolved.name + "-wal").exists()
        assert not resolved.with_name(resolved.name + "-shm").exists()

    def test_explicit_compressed_path_cannot_override_the_manifest(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        _, _, manifest, _ = _compressed_bundle(repository)
        legacy = repository / "data" / "pmvl-snapshot.db"
        alternate = compress_snapshot(
            _database(tmp_path / "alternate.db"),
            tmp_path / "alternate.db.gz",
        )

        with pytest.raises(SnapshotArtifactError, match="disagrees with the manifest"):
            resolve_snapshot_path(
                manifest,
                legacy,
                compressed_path=alternate,
                temp_root=tmp_path / "runtime",
            )

    def test_verified_temp_file_is_reused(self, tmp_path: Path) -> None:
        repository = tmp_path / "checkout"
        _, _, manifest, _ = _compressed_bundle(repository)
        legacy = repository / "data" / "pmvl-snapshot.db"
        runtime = tmp_path / "runtime"

        first = resolve_snapshot_path(manifest, legacy, temp_root=runtime)
        stat = first.stat()
        second = resolve_snapshot_path(manifest, legacy, temp_root=runtime)

        assert first == second
        assert second.stat().st_ino == stat.st_ino
        assert second.stat().st_mtime_ns == stat.st_mtime_ns

    def test_a_subsequent_compressed_snapshot_gets_its_own_verified_path(
        self, tmp_path: Path
    ) -> None:
        first_repository = tmp_path / "first-checkout"
        second_repository = tmp_path / "second-checkout"
        _, _, first_manifest, first_data = _compressed_bundle(
            first_repository,
            value="first",
        )
        _, _, second_manifest, second_data = _compressed_bundle(
            second_repository,
            value="second",
        )
        runtime = tmp_path / "runtime"

        first = resolve_snapshot_path(
            first_manifest,
            first_repository / "data/pmvl-snapshot.db",
            temp_root=runtime,
        )
        second = resolve_snapshot_path(
            second_manifest,
            second_repository / "data/pmvl-snapshot.db",
            temp_root=runtime,
        )

        assert first != second
        assert first.name == f"{first_data['uncompressed_sha256']}.db"
        assert second.name == f"{second_data['uncompressed_sha256']}.db"
        assert first.is_file() and second.is_file()

    def test_read_only_checkout_resolves_into_writable_external_tmp(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "read-only-checkout"
        raw, _, manifest, _ = _compressed_bundle(repository)
        legacy = repository / "data/pmvl-snapshot.db"
        for path in sorted(repository.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        repository.chmod(0o555)
        runtime = tmp_path / "runtime"

        resolved = resolve_snapshot_path(manifest, legacy, temp_root=runtime)

        assert resolved.read_bytes() == raw.read_bytes()
        assert not list(repository.rglob("*.tmp"))

    def test_concurrent_resolution_never_exposes_a_partial_database(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        raw, _, manifest, _ = _compressed_bundle(repository)
        legacy = repository / "data" / "pmvl-snapshot.db"
        runtime = tmp_path / "runtime"

        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(
                pool.map(
                    lambda _: resolve_snapshot_path(
                        manifest,
                        legacy,
                        temp_root=runtime,
                    ),
                    range(16),
                )
            )

        assert len(set(resolved)) == 1
        assert resolved[0].read_bytes() == raw.read_bytes()
        assert not list(runtime.glob("*.tmp"))
        connection = sqlite3.connect(
            f"file:{resolved[0]}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            connection.close()

    def test_missing_gzip_does_not_fall_back_to_existing_raw(self, tmp_path: Path) -> None:
        repository = tmp_path / "checkout"
        _, encoded, manifest, _ = _compressed_bundle(repository)
        encoded.unlink()
        legacy = _database(repository / "data" / "pmvl-snapshot.db")

        with pytest.raises(SnapshotArtifactError, match="compressed Snapshot is missing"):
            resolve_snapshot_path(manifest, legacy, temp_root=tmp_path / "runtime")

    def test_truncated_gzip_fails_without_installing_final_path(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        _, encoded, manifest_path, manifest = _compressed_bundle(repository)
        encoded.write_bytes(encoded.read_bytes()[:20])
        manifest["compressed_sha256"] = _sha(encoded)
        manifest["compressed_size_bytes"] = encoded.stat().st_size
        manifest_path.write_text(json.dumps(manifest))
        runtime = tmp_path / "runtime"

        with pytest.raises(SnapshotArtifactError, match="decompression failed"):
            resolve_snapshot_path(
                manifest_path,
                repository / "data" / "pmvl-snapshot.db",
                temp_root=runtime,
            )
        assert not (runtime / f"{manifest['uncompressed_sha256']}.db").exists()

    def test_uncompressed_hash_mismatch_fails_before_atomic_install(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        _, _, manifest_path, manifest = _compressed_bundle(repository)
        manifest["uncompressed_sha256"] = "0" * 64
        manifest["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        runtime = tmp_path / "runtime"

        with pytest.raises(SnapshotArtifactError, match="checksum mismatch"):
            resolve_snapshot_path(
                manifest_path,
                repository / "data" / "pmvl-snapshot.db",
                temp_root=runtime,
            )
        assert not (runtime / ("0" * 64 + ".db")).exists()

    def test_malformed_or_unpublished_manifest_fails_closed(self, tmp_path: Path) -> None:
        repository = tmp_path / "checkout"
        _, _, manifest_path, _ = _compressed_bundle(repository)
        legacy = repository / "data" / "pmvl-snapshot.db"

        manifest_path.write_text("{not-json")
        with pytest.raises(SnapshotArtifactError, match="manifest cannot be read"):
            resolve_snapshot_path(manifest_path, legacy, temp_root=tmp_path / "one")

        _, _, manifest_path, _ = _compressed_bundle(
            tmp_path / "held-checkout",
            release_status="held",
        )
        with pytest.raises(SnapshotArtifactError, match="release_status"):
            resolve_snapshot_path(
                manifest_path,
                tmp_path / "held-checkout/data/pmvl-snapshot.db",
                temp_root=tmp_path / "two",
            )

    def test_runtime_temp_root_inside_real_checkout_shape_is_rejected(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        _, _, manifest, _ = _compressed_bundle(repository)
        legacy = repository / "data" / "pmvl-snapshot.db"
        forbidden = repository / "runtime-cache"

        with pytest.raises(SnapshotArtifactError, match="outside the repository"):
            resolve_snapshot_path(
                manifest,
                legacy,
                temp_root=forbidden,
            )
        assert not forbidden.exists()

    def test_existing_temp_root_permissions_are_tightened(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        _, _, manifest, _ = _compressed_bundle(repository)
        runtime = tmp_path / "runtime"
        runtime.mkdir(mode=0o777)
        runtime.chmod(0o777)

        resolve_snapshot_path(
            manifest,
            repository / "data/pmvl-snapshot.db",
            temp_root=runtime,
        )

        assert runtime.stat().st_mode & 0o777 == 0o700

    def test_symlink_temp_root_is_rejected(self, tmp_path: Path) -> None:
        repository = tmp_path / "checkout"
        _, _, manifest, _ = _compressed_bundle(repository)
        actual = tmp_path / "actual-runtime"
        actual.mkdir()
        linked = tmp_path / "linked-runtime"
        linked.symlink_to(actual, target_is_directory=True)

        with pytest.raises(SnapshotArtifactError, match="must not be a symlink"):
            resolve_snapshot_path(
                manifest,
                repository / "data/pmvl-snapshot.db",
                temp_root=linked,
            )

    def test_symlink_cache_entry_is_rejected_even_if_target_bytes_match(
        self, tmp_path: Path
    ) -> None:
        repository = tmp_path / "checkout"
        raw, _, manifest, data = _compressed_bundle(repository)
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        external = tmp_path / "external.db"
        external.write_bytes(raw.read_bytes())
        cached = runtime / f"{data['uncompressed_sha256']}.db"
        cached.symlink_to(external)

        with pytest.raises(
            SnapshotArtifactError,
            match="cached uncompressed Snapshot must not be a symlink",
        ):
            resolve_snapshot_path(
                manifest,
                repository / "data/pmvl-snapshot.db",
                temp_root=runtime,
            )
