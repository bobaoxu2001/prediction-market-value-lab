"""Independently verify a committed compressed Snapshot artefact.

This is intentionally a small command-line boundary around the shared artefact
contract.  The publish job uses it on blobs read back from the commit, not merely
on working-tree files, and may additionally provide the raw candidate that
Research validated so byte identity is proved across the handoff.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/shared/src"))

from pmvl_shared.manifest import sha256_of  # noqa: E402
from pmvl_shared.snapshot_artifact import (  # noqa: E402
    materialize_verified_compressed_snapshot,
    verify_compressed_snapshot,
)


def check(
    compressed: Path,
    manifest_path: Path,
    *,
    expected_uncompressed: Path | None = None,
    expected_release: str | None = None,
) -> list[str]:
    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest cannot be read: {exc}"]

    if expected_release and manifest.get("release_status") != expected_release:
        problems.append(
            f"release_status is {manifest.get('release_status')!r}; "
            f"expected {expected_release!r}"
        )

    if expected_uncompressed is not None:
        raw = expected_uncompressed
        if not raw.is_file():
            problems.append(f"expected uncompressed candidate is missing: {raw}")
            return problems
        problems.extend(verify_compressed_snapshot(raw, compressed, manifest_path))
        return problems

    # CI may verify a committed pair without carrying the research job's raw
    # candidate. Decompress into an isolated temporary directory, then apply the
    # same dual-hash, deterministic-gzip and SQLite-integrity contract.
    with tempfile.TemporaryDirectory(prefix="pmvl-artifact-verify-") as directory:
        raw = Path(directory) / "pmvl-snapshot.db"
        try:
            materialize_verified_compressed_snapshot(
                compressed,
                raw,
                manifest_path,
            )
        except Exception as exc:  # noqa: BLE001 - report a complete refusal
            problems.append(str(exc))
            return problems
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compressed", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-uncompressed", default=None)
    parser.add_argument(
        "--expected-release",
        choices=("held", "published"),
        default=None,
    )
    args = parser.parse_args(argv)

    compressed = Path(args.compressed)
    manifest_path = Path(args.manifest)
    expected = (
        Path(args.expected_uncompressed)
        if args.expected_uncompressed
        else None
    )
    problems = check(
        compressed,
        manifest_path,
        expected_uncompressed=expected,
        expected_release=args.expected_release,
    )
    if problems:
        print("COMPRESSED SNAPSHOT REJECTED:")
        for problem in problems:
            print(f"   {problem}")
        return 1

    manifest = json.loads(manifest_path.read_text())
    print(
        "compressed Snapshot verified: "
        f"{compressed.stat().st_size} bytes / {sha256_of(compressed)}, "
        f"SQLite {manifest.get('uncompressed_size_bytes')} bytes / "
        f"{manifest.get('uncompressed_sha256')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
