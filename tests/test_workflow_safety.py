"""The workflow control plane, asserted as data.

A workflow that only fires from the default branch cannot be tested by running it,
so its safety properties are checked by reading the YAML. Three defects motivate
this file, and all three produced a workflow that looked correct:

**Job-scoped permissions.** The single job held `contents: read` and ended with a
`git push`. Setting `GH_TOKEN` on that step cannot elevate the token - permissions
are job-scoped - so the first real publication would have failed.

**`exit 78` as a disabled state.** Actions treats a nonzero exit as a failure, so
merging with the schedule variable unset would have produced a red workflow every
hour, and the `if: always()` report step would then fail again on the missing file.

**Publication reachable from the wrong events.** Every guard has to be present at
once; any single missing condition lets a scheduled or preview run publish.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML parses the `on:` key as the boolean True.
    document["triggers"] = document.get("on", document.get(True, {}))
    return document


def _publication_guard_code(pipeline: dict) -> str:
    """Extract the executable staged-change guard from the workflow heredoc."""
    step = next(
        s
        for s in pipeline["jobs"]["publish"]["steps"]
        if s.get("name") == "Commit the compressed snapshot and manifest together"
    )
    chunks = step["run"].split("python - <<'PY'\n")
    assert len(chunks) == 3, "the publication step should contain two Python heredocs"
    return chunks[2].split("\nPY", 1)[0]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_publication_repo(repo: Path, *, encoding: str | None) -> None:
    """A tiny repository whose tracked Snapshot state matches its manifest."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    data = repo / "data"
    data.mkdir()
    if encoding == "gzip":
        (data / "pmvl-snapshot.db.gz").write_bytes(b"old gzip")
    else:
        (data / "pmvl-snapshot.db").write_bytes(b"old raw")
    (data / "pmvl-snapshot.manifest.json").write_text(
        json.dumps(
            {
                "artifact_encoding": encoding,
                "compressed_sha256": "old",
                "release_status": "published",
            }
        )
    )
    _git(repo, "add", "data")
    _git(repo, "commit", "-qm", "seed")


def _stage_gzip_publication(repo: Path, *, keep_raw: bool = False) -> None:
    data = repo / "data"
    raw = data / "pmvl-snapshot.db"
    if raw.exists() and not keep_raw:
        raw.unlink()
    (data / "pmvl-snapshot.db.gz").write_bytes(b"new gzip")
    (data / "pmvl-snapshot.manifest.json").write_text(
        json.dumps(
            {
                "artifact_encoding": "gzip",
                "compressed_sha256": "new",
                "release_status": "published",
            }
        )
    )
    _git(
        repo,
        "add",
        "--",
        "data/pmvl-snapshot.db.gz",
        "data/pmvl-snapshot.manifest.json",
    )
    tracked_raw = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", "data/pmvl-snapshot.db"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if tracked_raw.returncode == 0:
        _git(repo, "add", "-u", "--", "data/pmvl-snapshot.db")


def _run_publication_guard(repo: Path, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def pipeline() -> dict:
    return _load("pipeline.yml")


@pytest.fixture(scope="module")
def ci() -> dict:
    return _load("ci.yml")


class TestPermissionsMatrix:
    def test_the_workflow_default_is_read_only(self, pipeline: dict) -> None:
        assert pipeline["permissions"] == {"contents": "read"}

    def test_research_is_read_only(self, pipeline: dict) -> None:
        assert pipeline["jobs"]["research"]["permissions"] == {"contents": "read"}

    def test_publish_has_write(self, pipeline: dict) -> None:
        assert pipeline["jobs"]["publish"]["permissions"] == {"contents": "write"}

    def test_no_other_job_has_write(self, pipeline: dict) -> None:
        for name, job in pipeline["jobs"].items():
            if name == "publish":
                continue
            assert job.get("permissions", {}).get("contents") != "write", (
                f"{name} holds a write token it does not need"
            )

    def test_the_read_only_job_never_pushes(self, pipeline: dict) -> None:
        """The original defect. A push inside a read-token job fails at runtime,
        which is the worst place to discover it."""
        for step in pipeline["jobs"]["research"]["steps"]:
            body = step.get("run") or ""
            assert "git push" not in body, (
                f"research step {step.get('name')!r} pushes with a read-only token"
            )
            assert "git commit" not in body, (
                f"research step {step.get('name')!r} commits with a read-only token"
            )

    def test_ci_grants_no_write_anywhere(self, ci: dict) -> None:
        assert ci["permissions"] == {"contents": "read"}
        for name, job in ci["jobs"].items():
            assert job.get("permissions", {}).get("contents") != "write", name


class TestPublishEligibility:
    @pytest.fixture()
    def condition(self, pipeline: dict) -> str:
        return " ".join(pipeline["jobs"]["publish"]["if"].split())

    def test_it_requires_manual_dispatch(self, condition: str) -> None:
        assert "github.event_name == 'workflow_dispatch'" in condition

    def test_it_requires_the_explicit_input(self, condition: str) -> None:
        assert "inputs.publish == true" in condition

    def test_it_requires_the_repository_variable(self, condition: str) -> None:
        """Neither a mistaken checkbox nor a forgotten variable may publish alone."""
        assert "vars.PMVL_SCHEDULE_PUBLISH_ENABLED == 'true'" in condition

    def test_it_requires_main(self, condition: str) -> None:
        assert "github.ref == 'refs/heads/main'" in condition

    def test_it_requires_a_successful_research_job(self, condition: str) -> None:
        assert "needs.research.result == 'success'" in condition

    def test_it_requires_a_validated_candidate(self, condition: str) -> None:
        """A candidate that failed validation must not be publishable even if
        every other condition is satisfied."""
        assert "needs.research.outputs.publication_eligible == 'true'" in condition

    def test_a_scheduled_event_cannot_satisfy_it(self, condition: str) -> None:
        """`schedule` fails the first clause, and there is no `||` to escape through."""
        assert "||" not in condition, (
            "an alternation in the publish condition could let a scheduled run "
            "satisfy it through a different branch"
        )

    def test_publish_depends_on_research(self, pipeline: dict) -> None:
        assert pipeline["jobs"]["publish"]["needs"] == "research"

    def test_pull_request_is_not_a_trigger_at_all(self, pipeline: dict) -> None:
        """The strongest form of "a PR cannot publish": it cannot start this
        workflow."""
        assert "pull_request" not in pipeline["triggers"]


class TestDisabledScheduleSkipsRatherThanFails:
    @pytest.fixture()
    def condition(self, pipeline: dict) -> str:
        return " ".join(pipeline["jobs"]["research"]["if"].split())

    def test_the_gate_is_a_job_level_if(self, condition: str) -> None:
        """A job-level `if` produces a SKIPPED job. A shell `exit 78` produces a
        failed one, and merging with the variable unset would have painted the
        repository red every hour."""
        assert "vars.PMVL_SCHEDULE_ENABLED == 'true'" in condition

    def test_no_step_exits_with_a_status_code_to_disable_itself(
        self, pipeline: dict
    ) -> None:
        for job in pipeline["jobs"].values():
            for step in job.get("steps", []):
                assert "exit 78" not in (step.get("run") or ""), (
                    "exit 78 is a failure, not a skip"
                )

    def test_manual_dispatch_is_unaffected_by_the_schedule_variable(
        self, condition: str
    ) -> None:
        """The variable governs scheduled computation only. A human dispatching a
        run must not have to enable the schedule first."""
        assert "github.event_name != 'schedule'" in condition

    def test_the_two_variables_are_not_coupled(self, pipeline: dict) -> None:
        """Computation and publication are separate decisions; a single switch for
        both would force enabling publication to get scheduled research."""
        research_if = pipeline["jobs"]["research"]["if"]
        publish_if = pipeline["jobs"]["publish"]["if"]
        assert "PMVL_SCHEDULE_PUBLISH_ENABLED" not in research_if
        assert "PMVL_SCHEDULE_ENABLED" not in publish_if

    def test_no_report_step_runs_unconditionally_after_a_skip(
        self, pipeline: dict
    ) -> None:
        """The second half of the original failure: an `if: always()` step that
        then failed on a file the skipped run never produced. Steps inside a
        skipped job do not run at all, so the guard must be at job level - which
        the first test asserts - and any `always()` step must tolerate absence."""
        for step in pipeline["jobs"]["research"]["steps"]:
            if "always()" in str(step.get("if", "")):
                body = step.get("run") or ""
                uses = step.get("uses") or ""
                assert (
                    "if-no-files-found: ignore" in str(step.get("with", ""))
                    or "-f run-report.json" in body
                    or "upload-artifact" in uses
                ), f"{step.get('name')!r} runs always() but assumes its inputs exist"


class TestCandidateHandover:
    def test_the_artifact_name_is_unique_per_run_attempt(self, pipeline: dict) -> None:
        """Never "the latest artifact": that would let a concurrent or older run's
        candidate be published under this run's authorisation. A rerun retains the
        run id and SHA, so the attempt number is part of the identity too."""
        name = pipeline["jobs"]["research"]["outputs"]["artifact_name"]
        assert "github.run_id" in name
        assert "github.run_attempt" in name
        assert "github.sha" in name

    def test_publish_downloads_that_exact_artifact(self, pipeline: dict) -> None:
        steps = pipeline["jobs"]["publish"]["steps"]
        download = next(s for s in steps if "download-artifact" in str(s.get("uses")))
        assert download["with"]["name"] == "${{ needs.research.outputs.artifact_name }}"

    def test_research_uploads_the_complete_candidate_bundle(self, pipeline: dict) -> None:
        steps = pipeline["jobs"]["research"]["steps"]
        upload = next(
            s
            for s in steps
            if "upload-artifact" in str(s.get("uses"))
            and "candidate" in str(s.get("with", {}).get("path", ""))
        )
        paths = {
            line.strip()
            for line in upload["with"]["path"].splitlines()
            if line.strip()
        }
        assert paths == {
            "candidate/pmvl-snapshot.db",
            "candidate/pmvl-snapshot.db.gz",
            "candidate/pmvl-snapshot.manifest.json",
            "run-report.json",
        }
        assert upload["with"]["name"] == pipeline["jobs"]["research"]["outputs"]["artifact_name"]

    def test_a_partial_candidate_bundle_is_not_called_eligible(self, pipeline: dict) -> None:
        summarise = next(
            s
            for s in pipeline["jobs"]["research"]["steps"]
            if s.get("name") == "Summarise the run"
        )["run"]
        for path in (
            "candidate/pmvl-snapshot.db",
            "candidate/pmvl-snapshot.db.gz",
            "candidate/pmvl-snapshot.manifest.json",
            "run-report.json",
        ):
            assert path in summarise
        assert "missing_candidate_files" in summarise
        assert "missing_candidate_metadata" in summarise
        for field in (
            "candidate_snapshot_id",
            "candidate_uncompressed_sha256",
            "candidate_compressed_sha256",
            "parent_snapshot_id",
            "parent_snapshot_sha256",
            "run_id",
        ):
            assert f'"{field}"' in summarise
        assert "invalid SHA-256" in summarise
        assert "publication-eligible run did not produce" in summarise
        assert "publication-eligible run has no valid" in summarise

    def test_a_failed_candidate_is_never_uploaded(self, pipeline: dict) -> None:
        steps = pipeline["jobs"]["research"]["steps"]
        upload = next(
            s
            for s in steps
            if "upload-artifact" in str(s.get("uses"))
            and "candidate" in str(s.get("with", {}).get("path", ""))
        )
        assert "publication_eligible == 'true'" in upload["if"]

    def test_publish_checks_out_the_research_commit(self, pipeline: dict) -> None:
        """Checking out a moved main would publish a candidate built from
        different code."""
        checkout = pipeline["jobs"]["publish"]["steps"][0]
        assert checkout["with"]["ref"] == "${{ needs.research.outputs.source_commit_sha }}"

    def test_research_exposes_every_handover_output(self, pipeline: dict) -> None:
        outputs = pipeline["jobs"]["research"]["outputs"]
        for field in (
            "candidate_exists",
            "publication_eligible",
            "parent_snapshot_id",
            "parent_snapshot_sha256",
            "candidate_snapshot_id",
            "candidate_uncompressed_sha256",
            "candidate_compressed_sha256",
            "source_commit_sha",
            "pipeline_run_id",
            "artifact_name",
        ):
            assert field in outputs, f"{field} is not handed to the publish job"
        assert "candidate_sha256" not in outputs, (
            "an unqualified checksum is ambiguous once the committed bytes are gzip"
        )

    def test_publish_binds_both_hashes_and_both_run_ids(
        self, pipeline: dict
    ) -> None:
        step = next(
            s
            for s in pipeline["jobs"]["publish"]["steps"]
            if s.get("name") == "Revalidate the candidate independently"
        )
        assert step["env"]["EXPECTED_UNCOMPRESSED_SHA"] == (
            "${{ needs.research.outputs.candidate_uncompressed_sha256 }}"
        )
        assert step["env"]["EXPECTED_COMPRESSED_SHA"] == (
            "${{ needs.research.outputs.candidate_compressed_sha256 }}"
        )
        assert step["env"]["EXPECTED_PIPELINE_RUN_ID"] == (
            "${{ needs.research.outputs.pipeline_run_id }}"
        )
        assert step["env"]["EXPECTED_WORKFLOW_RUN_ID"] == "${{ github.run_id }}"
        assert step["env"]["EXPECTED_WORKFLOW_RUN_ATTEMPT"] == "${{ github.run_attempt }}"
        body = step["run"]
        for argument in (
            "--candidate-raw incoming/candidate/pmvl-snapshot.db",
            "--candidate-gzip incoming/candidate/pmvl-snapshot.db.gz",
            "--manifest incoming/candidate/pmvl-snapshot.manifest.json",
            "--report incoming/run-report.json",
            "--published-manifest data/pmvl-snapshot.manifest.json",
        ):
            assert argument in body


class TestPublicationSafety:
    @pytest.fixture()
    def publish_script(self, pipeline: dict) -> str:
        return "\n".join(
            (s.get("run") or "") for s in pipeline["jobs"]["publish"]["steps"]
        )

    @pytest.fixture()
    def publish_code(self, publish_script: str) -> str:
        """The script with comment lines removed.

        Matching raw text would let a commented-out check satisfy an assertion,
        and would also trip over a comment that merely NAMES the thing it forbids
        (`# Never --force`). Both failure modes are silent.
        """
        return "\n".join(
            line
            for line in publish_script.splitlines()
            if not line.strip().startswith("#")
        )

    def test_it_never_force_pushes(self, publish_code: str) -> None:
        for forbidden in ("--force", "push -f", "+HEAD:", "+refs/"):
            assert forbidden not in publish_code, f"publish uses {forbidden}"

    def test_it_contains_no_push_protection_bypass(self, pipeline: dict) -> None:
        publish = pipeline["jobs"]["publish"]
        assert not publish.get("continue-on-error")
        for step in publish["steps"]:
            assert not step.get("continue-on-error")
            code = step.get("run") or ""
            for forbidden in (
                "gh api",
                "curl ",
                "--push-option",
                "secret-scanning/alerts/",
                "secret_scanning_alert",
            ):
                assert forbidden not in code, (
                    f"publish step {step.get('name')!r} may bypass Push Protection "
                    f"through {forbidden!r}"
                )

    def test_it_revalidates_the_candidate(self, publish_code: str) -> None:
        assert "verify_candidate.py" in publish_code
        assert "validate_snapshot.py" in publish_code

    def test_it_checks_main_has_not_moved(self, publish_code: str) -> None:
        assert "git fetch origin main" in publish_code
        assert "rev-parse origin/main" in publish_code

    def test_the_guard_inspects_exact_statuses_across_the_entire_index(
        self, publish_code: str
    ) -> None:
        """A scoped count accepts the wrong statuses and cannot detect an extra
        staged source file. Name and status are checked without a pathspec."""
        command = (
            '"diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD"'
        )
        assert command in publish_code
        assert "git diff --cached --name-only --" not in publish_code
        assert "actual != expected" in publish_code

    def test_raw_deletion_is_staged_only_while_raw_is_tracked(
        self, publish_code: str
    ) -> None:
        """Naming an absent raw path in one `git add -A` command fails after the
        migration, before the gzip-to-gzip guard can run."""
        assert (
            "git ls-files --error-unmatch -- data/pmvl-snapshot.db"
            in publish_code
        )
        assert "git add -u -- data/pmvl-snapshot.db" in publish_code
        assert "git add -A -- \\\n  data/pmvl-snapshot.db" not in publish_code

    def test_initial_raw_to_gzip_set_is_exact(self, publish_code: str) -> None:
        for change in (
            '("D", RAW)',
            '("A", GZIP)',
            '("M", MANIFEST)',
        ):
            assert change in publish_code

    def test_future_gzip_to_gzip_set_is_exact(self, publish_code: str) -> None:
        expected_block = publish_code[
            publish_code.index('if previous_encoding == "gzip"') :
            publish_code.index("elif previous_encoding")
        ]
        assert '("M", GZIP)' in expected_block
        assert '("M", MANIFEST)' in expected_block
        assert '("D", RAW)' not in expected_block
        assert '("A", GZIP)' not in expected_block

    def test_manifest_decides_the_transition_not_file_guessing(
        self, publish_code: str
    ) -> None:
        assert 'previous.get("artifact_encoding")' in publish_code
        assert 'previous_encoding == "gzip"' in publish_code
        assert "unsupported previous artifact_encoding" in publish_code

    def test_the_resulting_index_has_only_gzip_and_manifest(
        self, publish_code: str
    ) -> None:
        assert 'tree_has(f":{RAW}")' in publish_code
        assert 'tree_has(f":{GZIP}")' in publish_code
        assert 'tree_has(f":{MANIFEST}")' in publish_code
        assert "raw and gzip snapshots would both remain" in publish_code
        assert "the staged gzip/manifest pair is incomplete" in publish_code
        assert 'staged_manifest.get("artifact_encoding") != "gzip"' in publish_code

    def test_it_verifies_the_committed_bytes(self, publish_code: str) -> None:
        """Working-tree validation is not enough: what the public serves is what
        the commit contains."""
        assert '"git", "show"' in publish_code, "the committed blobs are never read back"
        assert '"data/pmvl-snapshot.db.gz"' in publish_code
        assert '"data/pmvl-snapshot.manifest.json"' in publish_code
        assert 'f"HEAD:{name}"' in publish_code
        assert "scripts/verify_artifact.py" in publish_code
        assert "--expected-uncompressed incoming/candidate/pmvl-snapshot.db" in publish_code
        assert "--expected-release published" in publish_code

    def test_publish_copies_research_gzip_without_recompressing(
        self, publish_code: str
    ) -> None:
        assert 'src / "pmvl-snapshot.db.gz"' in publish_code
        assert 'Path("data/pmvl-snapshot.db.gz")' in publish_code
        assert "gzip.compress" not in publish_code
        assert "GzipFile" not in publish_code
        assert 'Path("data/pmvl-snapshot.db").unlink(missing_ok=True)' in publish_code

    def test_the_commit_does_not_skip_ci(self, publish_code: str) -> None:
        """Inverted deliberately.

        [skip ci] was here to stop a publication triggering the next pipeline.
        That protection now comes from the trigger list - the pipeline runs only
        on workflow_dispatch and schedule, never on push - so skipping CI bought
        nothing and cost everything: it made the commit that changes what the
        public site serves the only unverified commit in the repository.
        """
        commit_lines = [
            line for line in publish_code.splitlines() if "git commit" in line
        ]
        assert commit_lines
        assert all("[skip ci]" not in line for line in commit_lines)

    def test_release_status_becomes_published_only_at_the_commit(
        self, publish_code: str
    ) -> None:
        """Marking it published earlier would leave an artefact asserting a
        publication that had not happened, and a later failure would leave that
        assertion standing."""
        published_at = publish_code.index('"release_status"] = "published"')
        commit_at = publish_code.index("git commit -m")
        assert published_at < commit_at
        assert "git push" not in publish_code[:published_at]


class TestPushFailureDiagnosis:
    @pytest.fixture()
    def push_step(self, pipeline: dict) -> str:
        return next(
            s["run"]
            for s in pipeline["jobs"]["publish"]["steps"]
            if s.get("name") == "Push"
        )

    def test_it_captures_the_complete_push_diagnostic(self, push_step: str) -> None:
        assert "git push origin HEAD:main >push.log 2>&1" in push_step
        assert "cat push.log" in push_step

    def test_push_protection_is_classified_before_non_fast_forward(
        self, push_step: str
    ) -> None:
        protection = push_step.index(
            'grep -qiE "GH013|push protection|repository rule violations"'
        )
        race = push_step.index(
            'grep -qiE "non-fast-forward|fetch first|rejected.*behind"'
        )
        assert protection < race
        assert "BLOCKED by GitHub Push Protection" in push_step

    def test_the_diagnostic_forbids_security_bypass(self, push_step: str) -> None:
        assert "do NOT allowlist the finding" in push_step
        assert "bypass the rule" in push_step
        assert "disable secret scanning" in push_step

    def test_unknown_failures_stay_unknown(self, push_step: str) -> None:
        assert "push failed for a reason this step does not recognise" in push_step
        assert "NOT force-pushing" in push_step


class TestPublicationChangedFileGuardExecution:
    """Execute the workflow's real guard against representative Git indexes."""

    def test_initial_raw_to_gzip_transition_is_accepted(
        self, pipeline: dict, tmp_path: Path
    ) -> None:
        _seed_publication_repo(tmp_path, encoding=None)
        _stage_gzip_publication(tmp_path)

        result = _run_publication_guard(
            tmp_path, _publication_guard_code(pipeline)
        )
        assert result.returncode == 0, result.stderr + result.stdout

    def test_future_gzip_to_gzip_transition_is_accepted(
        self, pipeline: dict, tmp_path: Path
    ) -> None:
        _seed_publication_repo(tmp_path, encoding="gzip")
        _stage_gzip_publication(tmp_path)

        result = _run_publication_guard(
            tmp_path, _publication_guard_code(pipeline)
        )
        assert result.returncode == 0, result.stderr + result.stdout

    def test_an_extra_staged_source_file_is_rejected(
        self, pipeline: dict, tmp_path: Path
    ) -> None:
        _seed_publication_repo(tmp_path, encoding=None)
        _stage_gzip_publication(tmp_path)
        (tmp_path / "unexpected.py").write_text("print('must not publish')\n")
        _git(tmp_path, "add", "unexpected.py")

        result = _run_publication_guard(
            tmp_path, _publication_guard_code(pipeline)
        )
        assert result.returncode != 0
        assert "wrong exact change set" in result.stderr

    def test_raw_and_gzip_together_after_publication_are_rejected(
        self, pipeline: dict, tmp_path: Path
    ) -> None:
        _seed_publication_repo(tmp_path, encoding=None)
        _stage_gzip_publication(tmp_path, keep_raw=True)

        result = _run_publication_guard(
            tmp_path, _publication_guard_code(pipeline)
        )
        assert result.returncode != 0
        assert "wrong exact change set" in result.stderr


def _evaluate(expression: str, context: dict) -> bool:
    """Evaluate the subset of GitHub expression syntax these conditions use.

    Reading the YAML and asserting substrings proves a clause is *present*. It
    does not prove the clauses combine to the intended result - an `||` in the
    wrong place satisfies every substring assertion while opening a path a
    scheduled run can take. Evaluating the whole expression against real event
    contexts is what closes that gap.
    """
    import re

    rendered = " ".join(expression.split())
    for key, value in context.items():
        rendered = rendered.replace(key, repr(value) if isinstance(value, str) else str(value))
    rendered = rendered.replace("&&", " and ").replace("||", " or ")
    # GitHub's boolean literals are lowercase; Python's are not.
    rendered = re.sub(r"\btrue\b", "True", rendered)
    rendered = re.sub(r"\bfalse\b", "False", rendered)
    return bool(eval(rendered))  # noqa: S307 - a fixed expression from our own workflow


def _context(
    event: str,
    *,
    schedule_enabled: str = "",
    publish_enabled: str = "",
    publish_input: bool = False,
    ref: str = "refs/heads/main",
    research_result: str = "success",
    eligible: str = "true",
) -> dict:
    return {
        "github.event_name": event,
        "vars.PMVL_SCHEDULE_ENABLED": schedule_enabled,
        "vars.PMVL_SCHEDULE_PUBLISH_ENABLED": publish_enabled,
        "inputs.publish": publish_input,
        "github.ref": ref,
        "needs.research.result": research_result,
        "needs.research.outputs.publication_eligible": eligible,
    }


class TestGatingEvaluatedNotJustInspected:
    """The conditions are evaluated end to end against real event contexts."""

    @pytest.fixture()
    def research_if(self, pipeline: dict) -> str:
        return pipeline["jobs"]["research"]["if"]

    @pytest.fixture()
    def publish_if(self, pipeline: dict) -> str:
        return pipeline["jobs"]["publish"]["if"]

    @pytest.mark.parametrize("value", ["", "false", "FALSE", "0"])
    def test_schedule_is_skipped_when_the_variable_is_not_true(
        self, research_if: str, value: str
    ) -> None:
        """Absent, false, or any other value defaults safely to disabled."""
        assert not _evaluate(research_if, _context("schedule", schedule_enabled=value))

    def test_schedule_runs_when_enabled(self, research_if: str) -> None:
        assert _evaluate(research_if, _context("schedule", schedule_enabled="true"))

    @pytest.mark.parametrize("value", ["", "false"])
    def test_manual_dispatch_runs_regardless_of_the_schedule_variable(
        self, research_if: str, value: str
    ) -> None:
        """A human dispatching a run must not have to enable the schedule first."""
        assert _evaluate(research_if, _context("workflow_dispatch", schedule_enabled=value))

    def test_only_one_scenario_publishes(self, research_if: str, publish_if: str) -> None:
        """The whole matrix, evaluated. Exactly one combination may publish."""
        scenarios = {
            "schedule, var unset": _context("schedule"),
            "schedule enabled": _context("schedule", schedule_enabled="true"),
            "schedule with publish var": _context(
                "schedule", schedule_enabled="true", publish_enabled="true", publish_input=True
            ),
            "dispatch without publish": _context("workflow_dispatch"),
            "dispatch publish, no var": _context("workflow_dispatch", publish_input=True),
            "dispatch publish on a branch": _context(
                "workflow_dispatch", publish_enabled="true", publish_input=True,
                ref="refs/heads/feature",
            ),
            "dispatch publish, candidate invalid": _context(
                "workflow_dispatch", publish_enabled="true", publish_input=True,
                eligible="false",
            ),
            "dispatch publish, research failed": _context(
                "workflow_dispatch", publish_enabled="true", publish_input=True,
                research_result="failure",
            ),
            "THE ONE": _context(
                "workflow_dispatch", publish_enabled="true", publish_input=True
            ),
        }
        publishing = [
            name
            for name, ctx in scenarios.items()
            if _evaluate(research_if, ctx) and _evaluate(publish_if, ctx)
        ]
        assert publishing == ["THE ONE"], f"unexpected publishers: {publishing}"

    def test_a_scheduled_run_cannot_publish_under_any_variable_combination(
        self, publish_if: str
    ) -> None:
        for schedule in ("", "true"):
            for publish_var in ("", "true"):
                for publish_input in (False, True):
                    ctx = _context(
                        "schedule",
                        schedule_enabled=schedule,
                        publish_enabled=publish_var,
                        publish_input=publish_input,
                    )
                    assert not _evaluate(publish_if, ctx), (
                        f"schedule published with sched={schedule!r} "
                        f"var={publish_var!r} input={publish_input}"
                    )


class TestPublicationCommitsAreVerified:
    """A publication changes what the public site serves. Skipping CI on it made
    the riskiest commit in the repository the only unverified one."""

    @pytest.fixture()
    def publish_script(self, pipeline: dict) -> str:
        return "\n".join(
            (s.get("run") or "") for s in pipeline["jobs"]["publish"]["steps"]
        )

    def test_the_publication_commit_carries_no_skip_marker(self, publish_script: str) -> None:
        commit_lines = [
            line for line in publish_script.splitlines()
            if "git commit" in line and not line.strip().startswith("#")
        ]
        assert commit_lines, "no commit step found"
        for line in commit_lines:
            for marker in ("[skip ci]", "[ci skip]", "[no ci]", "***NO_CI***"):
                assert marker not in line, f"publication commit skips CI: {line.strip()}"

    def test_the_pipeline_cannot_be_triggered_by_a_push(self, pipeline: dict) -> None:
        """This is what makes removing [skip ci] safe: a publication commit
        triggers verification, but cannot start another pipeline."""
        assert "push" not in pipeline["triggers"]
        assert set(pipeline["triggers"]) <= {"workflow_dispatch", "schedule"}

    def test_no_recursive_publication_loop_exists(self, pipeline: dict) -> None:
        """The publish job is the only thing that commits, and only a dispatch or
        schedule can reach it. Neither is caused by a commit."""
        triggers = set(pipeline["triggers"])
        assert not (triggers & {"push", "create", "repository_dispatch"}), (
            "a commit-driven trigger would let a publication start the next "
            "publication"
        )


class TestPostDeploySmokeWorkflow:
    """A green build says the code compiled. It says nothing about whether the
    deployment answers."""

    @pytest.fixture(scope="class")
    def smoke(self) -> dict:
        return _load("postdeploy-smoke.yml")

    def test_it_runs_on_pushes_to_main(self, smoke: dict) -> None:
        assert "main" in smoke["triggers"]["push"]["branches"]

    def test_it_is_read_only(self, smoke: dict) -> None:
        """It observes production; it must never change it."""
        assert smoke["permissions"] == {"contents": "read"}
        for job in smoke["jobs"].values():
            assert job.get("permissions", {}).get("contents") != "write"

    def test_it_does_not_start_the_pipeline(self, smoke: dict, pipeline: dict) -> None:
        """Triggering data work from a push is how a publication would recurse."""
        body = "\n".join(
            (s.get("run") or "") for j in smoke["jobs"].values() for s in j.get("steps", [])
        )
        assert "run_automated_snapshot_pipeline" not in body
        assert "workflow run pipeline" not in body

    def test_it_checks_the_pushed_commit(self, smoke: dict) -> None:
        body = "\n".join(
            (s.get("run") or "") for j in smoke["jobs"].values() for s in j.get("steps", [])
        )
        assert "--commit" in body and "GITHUB_SHA" in body

    def test_it_uploads_a_report_even_on_failure(self, smoke: dict) -> None:
        steps = smoke["jobs"]["smoke"]["steps"]
        upload = next(s for s in steps if "upload-artifact" in str(s.get("uses")))
        assert "always()" in str(upload.get("if"))
