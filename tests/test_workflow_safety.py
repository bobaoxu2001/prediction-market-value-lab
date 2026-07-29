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

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML parses the `on:` key as the boolean True.
    document["triggers"] = document.get("on", document.get(True, {}))
    return document


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
    def test_the_artifact_name_is_unique_per_run(self, pipeline: dict) -> None:
        """Never "the latest artifact": that would let a concurrent or older run's
        candidate be published under this run's authorisation."""
        name = pipeline["jobs"]["research"]["outputs"]["artifact_name"]
        assert "github.run_id" in name
        assert "github.sha" in name

    def test_publish_downloads_that_exact_artifact(self, pipeline: dict) -> None:
        steps = pipeline["jobs"]["publish"]["steps"]
        download = next(s for s in steps if "download-artifact" in str(s.get("uses")))
        assert download["with"]["name"] == "${{ needs.research.outputs.artifact_name }}"

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
            "candidate_sha256",
            "source_commit_sha",
            "pipeline_run_id",
            "artifact_name",
        ):
            assert field in outputs, f"{field} is not handed to the publish job"


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

    def test_it_revalidates_the_candidate(self, publish_code: str) -> None:
        assert "verify_candidate.py" in publish_code
        assert "validate_snapshot.py" in publish_code

    def test_it_checks_main_has_not_moved(self, publish_code: str) -> None:
        assert "git fetch origin main" in publish_code
        assert "rev-parse origin/main" in publish_code

    def test_both_files_must_change_together(self, publish_code: str) -> None:
        """A commit carrying only one leaves the public pair internally
        inconsistent - a manifest describing a database that is not there."""
        assert "--cached --name-only" in publish_code
        assert "refusing to publish a mismatched pair" in publish_code

    def test_it_verifies_the_committed_bytes(self, publish_code: str) -> None:
        """Working-tree validation is not enough: what the public serves is what
        the commit contains."""
        assert '"git", "show"' in publish_code, "the committed blobs are never read back"
        assert "verify_artifact" in publish_code

    def test_the_commit_skips_ci(self, publish_code: str) -> None:
        """A published snapshot must not trigger the next pipeline run."""
        assert "[skip ci]" in publish_code

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
