"""Scope resolution, the job DAG, and what a degraded upstream means downstream.

Two production-shaped bugs motivate this file.

The workflow passed `--market-limit "${{ inputs.market_limit || '50' }}"`. A
scheduled event has no `inputs`, so the `||` fell through and every scheduled run
silently capped at 50 of roughly 2,300 markets - a pipeline that looked healthy
and scanned 2% of the universe.

And the workflow ran ingest, rank and arbitrage: three of nine jobs. No orderbook
refresh, no settlement sync, no scoring, no immutable daily record, no backtest.
An artefact built from that is missing the settlement results its track record
grades against, and nothing about it looks wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pmvl_shared.enums import JobStatus
from pmvl_shared.pipeline_dag import (
    JOB_BY_NAME,
    PIPELINE,
    blocking_reason,
    execution_order,
    publication_blockers,
)

from run_automated_snapshot_pipeline import (  # noqa: E402
    CI_SMOKE_MARKET_LIMIT,
    SMOKE_MARKET_LIMIT,
    PipelineError,
    resolve_scope,
)


class TestScopeResolution:
    def test_a_scheduled_run_scans_the_full_universe(self) -> None:
        """The bug: `inputs.market_limit || '50'` on an event with no inputs.

        A scheduled research run capped at 50 markets is not a smaller version of
        the product; it is a different product that looks the same.
        """
        scope = resolve_scope(
            event_name="schedule",
            input_scope=None,
            input_market_limit=None,
            input_publish=None,
        )
        assert scope.market_limit is None, "scheduled runs must not be capped"

    def test_a_manual_dispatch_defaults_to_a_smoke_limit(self) -> None:
        scope = resolve_scope(
            event_name="workflow_dispatch",
            input_scope=None,
            input_market_limit=None,
            input_publish=None,
        )
        assert scope.market_limit == SMOKE_MARKET_LIMIT

    def test_a_manual_dispatch_honours_a_custom_limit(self) -> None:
        scope = resolve_scope(
            event_name="workflow_dispatch",
            input_scope="smoke",
            input_market_limit="200",
            input_publish=None,
        )
        assert scope.market_limit == 200

    def test_a_manual_full_dispatch_is_uncapped(self) -> None:
        scope = resolve_scope(
            event_name="workflow_dispatch",
            input_scope="full",
            input_market_limit=None,
            input_publish=None,
        )
        assert scope.market_limit is None

    def test_pull_request_ci_is_the_smallest_safe_run(self) -> None:
        scope = resolve_scope(
            event_name="pull_request",
            input_scope=None,
            input_market_limit=None,
            input_publish=None,
        )
        assert scope.market_limit == CI_SMOKE_MARKET_LIMIT
        assert scope.publish is False

    def test_an_unknown_scope_is_rejected_rather_than_defaulted(self) -> None:
        """Defaulting an unrecognised scope is how a typo becomes a 50-market
        production run."""
        with pytest.raises(PipelineError):
            resolve_scope(
                event_name="workflow_dispatch",
                input_scope="everything",
                input_market_limit=None,
                input_publish=None,
            )

    def test_the_resolved_scope_is_printable_before_ingestion(self) -> None:
        scope = resolve_scope(
            event_name="schedule", input_scope=None, input_market_limit=None, input_publish=None
        )
        assert "market_limit=unlimited" in scope.label
        assert "event=schedule" in scope.label


class TestPublicationAuthority:
    @pytest.mark.parametrize("event", ["schedule", "pull_request"])
    def test_only_a_manual_dispatch_may_publish(self, event: str) -> None:
        """Publication is a lower-frequency, explicitly authorised act. A research
        refresh that published on its own would commit an 8 MB binary per run."""
        scope = resolve_scope(
            event_name=event, input_scope=None, input_market_limit=None, input_publish="true"
        )
        assert scope.publish is False

    def test_a_manual_dispatch_must_ask_to_publish(self) -> None:
        assert (
            resolve_scope(
                event_name="workflow_dispatch",
                input_scope="full",
                input_market_limit=None,
                input_publish=None,
            ).publish
            is False
        )


class TestBootstrapAuthority:
    def test_a_scheduled_run_may_not_start_from_an_empty_database(self) -> None:
        """The continuity failure: a run that cannot find its parent snapshot must
        stop, not silently rebuild history from nothing and publish it."""
        scope = resolve_scope(
            event_name="schedule", input_scope=None, input_market_limit=None, input_publish=None
        )
        assert scope.bootstrap_allowed is False

    def test_a_human_dispatch_may_bootstrap(self) -> None:
        scope = resolve_scope(
            event_name="workflow_dispatch",
            input_scope="smoke",
            input_market_limit=None,
            input_publish=None,
        )
        assert scope.bootstrap_allowed is True


class TestJobDag:
    def test_every_job_in_the_dag_has_an_implementation(self) -> None:
        import run_automated_snapshot_pipeline as pipeline

        source = Path(pipeline.__file__).read_text()
        for spec in PIPELINE:
            assert f'"{spec.name}"' in source, f"{spec.name} has no branch in _invoke"

    def test_the_order_respects_dependencies(self) -> None:
        order = execution_order()
        position = {name: i for i, name in enumerate(order)}
        for spec in PIPELINE:
            for dependency in spec.depends_on:
                assert position[dependency] < position[spec.name], (
                    f"{spec.name} is ordered before its dependency {dependency}"
                )

    def test_all_nine_jobs_run_not_three(self) -> None:
        """The workflow ran ingest, rank and arbitrage and published the result."""
        order = set(execution_order())
        assert order >= {
            "ingest",
            "orderbooks",
            "settle",
            "score",
            "rank",
            "arbitrage",
            "snapshot",
            "backtest",
            "prune",
        }

    def test_a_cycle_is_detected_rather_than_looped_on(self) -> None:
        from pmvl_shared import pipeline_dag

        original = pipeline_dag.PIPELINE
        try:
            pipeline_dag.PIPELINE = (
                pipeline_dag.JobSpec("a", "", depends_on=("b",)),
                pipeline_dag.JobSpec("b", "", depends_on=("a",)),
            )
            with pytest.raises(ValueError):
                pipeline_dag.execution_order()
        finally:
            pipeline_dag.PIPELINE = original


class TestDegradedUpstreams:
    def test_ranking_tolerates_a_partial_score(self) -> None:
        """Fewer markets ranked is a smaller list, which is honest."""
        assert (
            blocking_reason(
                JOB_BY_NAME["rank"], {"score": JobStatus.PARTIAL_SUCCESS}
            )
            is None
        )

    def test_settlement_does_not_tolerate_a_partial_ingest(self) -> None:
        """Grading against a feed that half-failed writes a track record wrong in
        a direction nobody can see afterwards."""
        reason = blocking_reason(
            JOB_BY_NAME["settle"], {"ingest": JobStatus.PARTIAL_SUCCESS}
        )
        assert reason and "requires complete upstream output" in reason

    def test_the_daily_snapshot_does_not_tolerate_a_partial_rank(self) -> None:
        """It is a permanent record and is never revised."""
        assert blocking_reason(
            JOB_BY_NAME["snapshot"], {"rank": JobStatus.PARTIAL_SUCCESS}
        )

    def test_a_missing_upstream_blocks_with_a_named_reason(self) -> None:
        reason = blocking_reason(JOB_BY_NAME["rank"], {})
        assert reason == "score did not run"

    def test_a_failed_upstream_blocks(self) -> None:
        reason = blocking_reason(JOB_BY_NAME["rank"], {"score": JobStatus.FAILED})
        assert reason and "failed" in reason


class TestPublicationGate:
    def _all(self, status: JobStatus) -> dict[str, JobStatus]:
        return {spec.name: status for spec in PIPELINE}

    def test_a_clean_run_may_publish(self) -> None:
        assert publication_blockers(self._all(JobStatus.SUCCESS)) == []

    def test_a_job_that_never_ran_blocks_publication(self) -> None:
        """Publishing an artefact whose settlement sync silently did not execute
        produces a track record that grades yesterday's calls against nothing."""
        statuses = self._all(JobStatus.SUCCESS)
        del statuses["settle"]
        assert "settle did not run" in publication_blockers(statuses)

    def test_a_failed_required_job_blocks_publication(self) -> None:
        statuses = self._all(JobStatus.SUCCESS)
        statuses["rank"] = JobStatus.FAILED
        assert any("rank" in b for b in publication_blockers(statuses))

    def test_a_partial_ingest_does_not_block_publication(self) -> None:
        """Ingest is declared partial-tolerant: a shorter market list is a real,
        honest result."""
        statuses = self._all(JobStatus.SUCCESS)
        statuses["ingest"] = JobStatus.PARTIAL_SUCCESS
        assert publication_blockers(statuses) == []

    def test_a_partial_rank_does_block_publication(self) -> None:
        statuses = self._all(JobStatus.SUCCESS)
        statuses["rank"] = JobStatus.PARTIAL_SUCCESS
        assert any("rank" in b for b in publication_blockers(statuses))

    def test_a_failed_backtest_does_not_block_publication(self) -> None:
        """A stale research figure is not a corrupt artefact."""
        statuses = self._all(JobStatus.SUCCESS)
        statuses["backtest"] = JobStatus.FAILED
        assert publication_blockers(statuses) == []
