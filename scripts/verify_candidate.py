"""Re-check a candidate in the publish job, independently of the job that built it.

The research job already validated this artefact. Checking again here is not
redundancy for its own sake: the two jobs run on different machines, the artefact
travelled through GitHub's artifact store between them, and the publish job holds
a write token the research job does not. Anything that trusts an upstream job's
word about an artefact it is about to commit has no way to detect a mismatch
between the thing that was validated and the thing that arrived.

The parent check is the subtle one. A candidate declares the published snapshot it
was computed from. If the repository's published snapshot has moved since - because
another publication landed while this run was computing - then promoting this
candidate would silently discard that intervening publication's data. Comparing
declared parent against actual parent turns a lost update into a refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/shared/src"))

from pmvl_shared.manifest import (  # noqa: E402
    ReleaseStatus,
    ValidationStatus,
    provenance_problems,
    sha256_of,
    verify_artifact,
)


def check(
    candidate: Path, manifest_path: Path, published_manifest: Path | None
) -> list[str]:
    problems: list[str] = []

    problems.extend(verify_artifact(candidate, manifest_path))
    if problems:
        # Without a verifying pair there is nothing meaningful to check further.
        return problems

    manifest = json.loads(manifest_path.read_text())

    # The artefact must arrive HELD. One already marked published would be
    # asserting a publication that has not happened.
    if manifest.get("release_status") != ReleaseStatus.HELD:
        problems.append(
            f"candidate release_status is {manifest.get('release_status')!r}; "
            f"expected {ReleaseStatus.HELD!r} before the commit"
        )
    if manifest.get("validation_status") != ValidationStatus.PASSED:
        problems.append(
            f"candidate validation_status is {manifest.get('validation_status')!r}"
        )

    problems.extend(provenance_problems(manifest))

    # Values the research job published as outputs. Comparing them here proves the
    # artefact that arrived is the artefact that was validated.
    expected = {
        "sha256": os.environ.get("EXPECTED_CANDIDATE_SHA"),
        "snapshot_id": os.environ.get("EXPECTED_CANDIDATE_ID"),
        "parent_snapshot_id": os.environ.get("EXPECTED_PARENT_ID"),
        "parent_snapshot_sha256": os.environ.get("EXPECTED_PARENT_SHA"),
        "code_commit_sha": os.environ.get("EXPECTED_COMMIT"),
    }
    actual_sha = sha256_of(candidate)
    if expected["sha256"] and expected["sha256"] != actual_sha:
        problems.append(
            f"candidate checksum {actual_sha[:16]}... does not match the research "
            f"job's declared {expected['sha256'][:16]}..."
        )
    for field in ("snapshot_id", "parent_snapshot_id", "parent_snapshot_sha256"):
        want = expected[field]
        if want and manifest.get(field) != want:
            problems.append(
                f"candidate {field} is {manifest.get(field)!r}, "
                f"research job declared {want!r}"
            )
    want_commit = expected["code_commit_sha"]
    if want_commit and not str(want_commit).startswith(str(manifest.get("code_commit_sha"))):
        problems.append(
            f"candidate was built from commit {manifest.get('code_commit_sha')!r}, "
            f"but this job checked out {want_commit[:12]!r}"
        )

    # The lost-update guard.
    if published_manifest and published_manifest.exists():
        current = json.loads(published_manifest.read_text())
        declared_parent = manifest.get("parent_snapshot_id")
        actual_parent = current.get("snapshot_id")
        if declared_parent and actual_parent and declared_parent != actual_parent:
            problems.append(
                f"parent mismatch: candidate was computed from {declared_parent!r} "
                f"but the repository now publishes {actual_parent!r}. Publishing "
                "would discard the intervening snapshot. Re-run the pipeline."
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--published", default=None)
    args = parser.parse_args(argv)

    published = Path(args.published) if args.published else None
    problems = check(Path(args.candidate), Path(args.manifest), published)

    if problems:
        print("CANDIDATE REJECTED:")
        for problem in problems:
            print(f"   {problem}")
        return 1
    print("candidate verified: checksum, provenance, parent and release status all match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
