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
import re
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
from pmvl_shared.snapshot_artifact import verify_compressed_snapshot  # noqa: E402

_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{12}|[0-9a-f]{40})$")


def check(
    candidate: Path,
    manifest_path: Path,
    published_manifest: Path | None,
    *,
    compressed_candidate: Path | None = None,
    run_report: Path | None = None,
) -> list[str]:
    problems: list[str] = []

    problems.extend(verify_artifact(candidate, manifest_path))
    if problems:
        # Without a verifying pair there is nothing meaningful to check further.
        return problems

    manifest = json.loads(manifest_path.read_text())
    if manifest.get("artifact_encoding") == "gzip":
        if compressed_candidate is None:
            compressed_candidate = candidate.with_name(candidate.name + ".gz")
        problems.extend(
            verify_compressed_snapshot(
                candidate, compressed_candidate, manifest_path
            )
        )
        if problems:
            return problems

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
    for field in ("code_commit_sha", "source_commit_sha"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _COMMIT_SHA_RE.fullmatch(value):
            problems.append(
                f"candidate {field} must be a lowercase 12- or 40-character Git SHA"
            )
    if manifest.get("source_commit_sha") != manifest.get("code_commit_sha"):
        problems.append("candidate source_commit_sha must equal code_commit_sha")

    # Values the research job published as outputs. Comparing them here proves the
    # artefact that arrived is the artefact that was validated.
    expected = {
        "uncompressed_sha256": (
            os.environ.get("EXPECTED_CANDIDATE_UNCOMPRESSED_SHA")
            or os.environ.get("EXPECTED_UNCOMPRESSED_SHA")
            or os.environ.get("EXPECTED_CANDIDATE_SHA")
        ),
        "compressed_sha256": (
            os.environ.get("EXPECTED_CANDIDATE_COMPRESSED_SHA")
            or os.environ.get("EXPECTED_COMPRESSED_SHA")
        ),
        "snapshot_id": os.environ.get("EXPECTED_CANDIDATE_ID"),
        "parent_snapshot_id": os.environ.get("EXPECTED_PARENT_ID"),
        "parent_snapshot_sha256": os.environ.get("EXPECTED_PARENT_SHA"),
        "code_commit_sha": os.environ.get("EXPECTED_COMMIT"),
        "pipeline_run_id": os.environ.get("EXPECTED_PIPELINE_RUN_ID"),
        "workflow_run_id": os.environ.get("EXPECTED_WORKFLOW_RUN_ID"),
        "workflow_run_attempt": os.environ.get("EXPECTED_WORKFLOW_RUN_ATTEMPT"),
    }
    actual_sha = sha256_of(candidate)
    if (
        expected["uncompressed_sha256"]
        and expected["uncompressed_sha256"] != actual_sha
    ):
        problems.append(
            f"candidate checksum {actual_sha[:16]}... does not match the research "
            "job's declared "
            f"{expected['uncompressed_sha256'][:16]}..."
        )
    actual_compressed_sha = (
        sha256_of(compressed_candidate)
        if compressed_candidate and compressed_candidate.exists()
        else None
    )
    if (
        expected["compressed_sha256"]
        and expected["compressed_sha256"] != actual_compressed_sha
    ):
        problems.append(
            "compressed candidate checksum "
            f"{str(actual_compressed_sha or '')[:16]}... does not match the "
            f"research job's declared {expected['compressed_sha256'][:16]}..."
        )
    for field in (
        "snapshot_id",
        "parent_snapshot_id",
        "parent_snapshot_sha256",
        "pipeline_run_id",
        "workflow_run_id",
        "workflow_run_attempt",
    ):
        want = expected[field]
        if want and manifest.get(field) != want:
            problems.append(
                f"candidate {field} is {manifest.get(field)!r}, "
                f"research job declared {want!r}"
            )
    want_commit = expected["code_commit_sha"]
    if want_commit:
        for field in ("code_commit_sha", "source_commit_sha"):
            actual_commit = str(manifest.get(field) or "")
            matches = (
                len(actual_commit) == 12
                and str(want_commit)[:12] == actual_commit
            ) or (
                len(actual_commit) == 40
                and str(want_commit) == actual_commit
            )
            if not matches:
                if field == "code_commit_sha":
                    problems.append(
                        "candidate was built from commit "
                        f"{manifest.get(field)!r}, but this job checked out "
                        f"{want_commit[:12]!r}"
                    )
                else:
                    problems.append(
                        f"candidate {field} is {manifest.get(field)!r}, but this "
                        f"job checked out {want_commit[:12]!r}"
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
        declared_parent_sha = manifest.get("parent_snapshot_sha256")
        actual_parent_sha = (
            current.get("uncompressed_sha256") or current.get("sha256")
        )
        if (
            declared_parent_sha
            and actual_parent_sha
            and declared_parent_sha != actual_parent_sha
        ):
            problems.append(
                "parent checksum mismatch: candidate was computed from "
                f"{declared_parent_sha[:16]}... but the repository now publishes "
                f"{actual_parent_sha[:16]}.... Re-run the pipeline."
            )

    if run_report and not run_report.exists():
        problems.append(f"run report is missing: {run_report}")
    elif run_report:
        report = json.loads(run_report.read_text())
        report_checks = {
            "snapshot_id": report.get("candidate_snapshot_id"),
            "uncompressed_sha256": (
                report.get("candidate_uncompressed_sha256")
                or report.get("candidate_sha256")
            ),
            "compressed_sha256": report.get("candidate_compressed_sha256"),
            "pipeline_run_id": report.get("run_id"),
            "workflow_run_id": report.get("workflow_run_id"),
            "workflow_run_attempt": report.get("workflow_run_attempt"),
        }
        actual_checks = {
            "snapshot_id": manifest.get("snapshot_id"),
            "uncompressed_sha256": (
                manifest.get("uncompressed_sha256") or manifest.get("sha256")
            ),
            "compressed_sha256": manifest.get("compressed_sha256"),
            "pipeline_run_id": manifest.get("pipeline_run_id"),
            "workflow_run_id": manifest.get("workflow_run_id"),
            "workflow_run_attempt": manifest.get("workflow_run_attempt"),
        }
        for field, want in report_checks.items():
            if want and actual_checks.get(field) != want:
                problems.append(
                    f"candidate {field} is {actual_checks.get(field)!r}, "
                    f"run report declared {want!r}"
                )
        if not report.get("publication_eligible"):
            problems.append("run report does not mark the candidate publication_eligible")
        if report.get("publication_blockers"):
            problems.append(
                "run report contains publication blockers: "
                + "; ".join(map(str, report["publication_blockers"]))
            )
        non_terminal = report.get("non_terminal_jobs_in_candidate") or []
        if non_terminal:
            problems.append(
                "run report contains non-terminal candidate jobs: "
                + ", ".join(map(str, non_terminal))
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--candidate-raw", default=None)
    parser.add_argument("--candidate-gzip", default=None)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--published", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--published-manifest", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)

    candidate_arg = args.candidate_raw or args.candidate
    if not candidate_arg:
        parser.error("--candidate-raw is required")
    published_arg = args.published_manifest or args.published
    published = Path(published_arg) if published_arg else None
    problems = check(
        Path(candidate_arg),
        Path(args.manifest),
        published,
        compressed_candidate=(
            Path(args.candidate_gzip) if args.candidate_gzip else None
        ),
        run_report=Path(args.report) if args.report else None,
    )

    if problems:
        print("CANDIDATE REJECTED:")
        for problem in problems:
            print(f"   {problem}")
        return 1
    print(
        "candidate verified: raw and gzip bytes, provenance, run, parent and "
        "release status all match"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
