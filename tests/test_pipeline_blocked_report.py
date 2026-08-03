"""A blocked candidate must still leave a report saying why.

The scheduled pipeline failed for two days with the workflow reporting

    the pipeline produced no report; it did not run to completion

which is false in the way that matters: the run HAD completed, all nine jobs had
succeeded, and the candidate was rejected by the snapshot size ceiling. The
report that would have said so was never written, because ``PipelineError`` from
the candidate build escaped past the write.

Diagnosis therefore had to come from scraping the traceback out of raw workflow
logs, and the missing report is what made a deterministic, self-describing
failure look like an infrastructure fault.

Fail-closed is unchanged and is asserted here: the run still exits non-zero, the
disposition stays ``not_built``, and the candidate stays ineligible for
publication. Only the explanation survives.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_automated_snapshot_pipeline as pipeline  # noqa: E402
from run_automated_snapshot_pipeline import (  # noqa: E402
    CandidateDisposition,
    PipelineError,
)

CEILING_FAILURE = (
    "candidate failed validation:    quote coherence: 120 markets consistent\n"
    "   manifest: failed / held\n"
    "SNAPSHOT VALIDATION FAILED:\n"
    "   snapshot is 44.8 MB, above the 42 MB ceiling\n"
)


@pytest.fixture()
def blocked_run(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A run whose nine jobs all succeed and whose candidate is then rejected."""
    report = tmp_path / "run-report.json"

    def all_jobs_succeed(scope, outcome):  # noqa: ANN001, ANN202
        from pmvl_shared.pipeline_dag import PIPELINE

        for spec in PIPELINE:
            outcome.statuses[spec.name] = pipeline.JobStatus.SUCCESS

    def rejected(work_dir, operational, outcome):  # noqa: ANN001, ANN202
        raise PipelineError(CEILING_FAILURE)

    monkeypatch.setattr(pipeline, "initialise_operational_db", lambda w, o: w / "op.db")
    monkeypatch.setattr(pipeline, "_bind_database", lambda p: {})
    monkeypatch.setattr(pipeline, "migrate", lambda p, o: None)
    monkeypatch.setattr(pipeline, "publication_blockers", lambda statuses: [])
    monkeypatch.setattr(pipeline, "build_and_validate_candidate", rejected)

    async def _run_jobs(scope, outcome):  # noqa: ANN001, ANN202
        all_jobs_succeed(scope, outcome)

    monkeypatch.setattr(pipeline, "run_jobs", _run_jobs)

    exit_code = pipeline.main(
        ["--event", "schedule", "--work-dir", str(tmp_path), "--report", str(report)]
    )
    return exit_code, report


class TestBlockedCandidateReporting:
    def test_the_run_still_fails(self, blocked_run) -> None:  # noqa: ANN001
        """Recording the reason must not turn a rejection into a green run."""
        exit_code, _ = blocked_run
        assert exit_code == 1

    def test_the_report_exists(self, blocked_run) -> None:  # noqa: ANN001
        exit_code, report = blocked_run
        assert report.is_file(), (
            "no report was written, so the workflow can only say the pipeline "
            "'did not run to completion' - which is not what happened"
        )

    def test_the_report_names_the_validator_that_rejected_it(
        self, blocked_run
    ) -> None:  # noqa: ANN001
        _, report = blocked_run
        payload = json.loads(report.read_text())
        assert "above the 42 MB ceiling" in (payload["candidate_failure"] or "")

    def test_the_report_shows_the_jobs_had_succeeded(self, blocked_run) -> None:  # noqa: ANN001
        """The whole point: distinguishing "the jobs broke" from "the jobs were
        fine and the artefact was rejected"."""
        _, report = blocked_run
        payload = json.loads(report.read_text())
        assert set(payload["statuses"].values()) == {"success"}
        assert len(payload["statuses"]) == 9

    def test_the_candidate_remains_blocked(self, blocked_run) -> None:  # noqa: ANN001
        _, report = blocked_run
        payload = json.loads(report.read_text())
        assert payload["candidate_disposition"] == CandidateDisposition.NOT_BUILT
        assert payload["published"] is False

    def test_the_candidate_is_not_publication_eligible(self, blocked_run) -> None:  # noqa: ANN001
        """Fail-closed. A run with no candidate must never look promotable."""
        _, report = blocked_run
        payload = json.loads(report.read_text())
        assert payload["publication_eligible"] is False
        assert any(
            "candidate not built" in blocker
            for blocker in payload["publication_blockers"]
        )
