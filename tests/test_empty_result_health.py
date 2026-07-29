"""An empty list must say which of three different things happened.

"0 opportunities" rendered identically whether the scan ran and found nothing,
never ran at all, or ran against stale data. That is how a broken scanner looks
like a quiet market.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from pmvl_shared.cadence import DeploymentMode
from pmvl_shared.enums import JobStatus
from pmvl_shared.freshness import DataType
from pmvl_shared.timeutil import utcnow

from pmvl_api.health import EmptyResultCause, HealthLevel, classify_empty_result


def _run(db, *, job_name="arbitrage", minutes_ago=1, status=JobStatus.SUCCESS):  # noqa: ANN001, ANN003
    from pmvl_markets.db_models import JobRun

    db.add(
        JobRun(
            job_name=job_name,
            status=status.value,
            started_at=utcnow() - timedelta(minutes=minutes_ago),
            finished_at=utcnow() - timedelta(minutes=minutes_ago),
        )
    )
    db.flush()


class TestTheThreeCauses:
    def test_a_recent_scan_on_fresh_data_is_a_real_finding(self, clean_db) -> None:  # noqa: ANN001
        _run(clean_db, minutes_ago=1)
        result = classify_empty_result(
            clean_db, DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE, input_age_seconds=60
        )
        assert result["cause"] == EmptyResultCause.ZERO_OPPORTUNITIES.value
        assert result["level"] == HealthLevel.OK.value
        assert result["empty_result_is_a_finding"] is True

    def test_no_recent_scan_is_an_outage_not_a_finding(self, clean_db) -> None:  # noqa: ANN001
        """Nothing looked, so the empty list says nothing about the market."""
        _run(clean_db, minutes_ago=600)
        result = classify_empty_result(
            clean_db, DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE, input_age_seconds=60
        )
        assert result["cause"] == EmptyResultCause.PIPELINE_DID_NOT_RUN.value
        assert result["level"] == HealthLevel.FAILING.value
        assert result["empty_result_is_a_finding"] is False

    def test_a_scan_on_stale_data_is_a_coverage_statement(self, clean_db) -> None:  # noqa: ANN001
        _run(clean_db, minutes_ago=1)
        result = classify_empty_result(
            clean_db, DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE, input_age_seconds=99999
        )
        assert (
            result["cause"]
            == EmptyResultCause.PIPELINE_RAN_WITH_STALE_OR_INCOMPLETE_DATA.value
        )
        assert result["level"] == HealthLevel.DEGRADED.value
        assert result["empty_result_is_a_finding"] is False

    def test_never_observed_input_is_treated_as_stale(self, clean_db) -> None:  # noqa: ANN001
        _run(clean_db, minutes_ago=1)
        result = classify_empty_result(
            clean_db, DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE, input_age_seconds=None
        )
        assert (
            result["cause"]
            == EmptyResultCause.PIPELINE_RAN_WITH_STALE_OR_INCOMPLETE_DATA.value
        )


class TestSnapshotIsNotAnOutage:
    def test_a_frozen_deployment_reports_its_own_cause(self, clean_db) -> None:  # noqa: ANN001
        """A snapshot never had a scanner. Reporting PIPELINE_DID_NOT_RUN would
        describe a deployment behaving correctly as a failure."""
        result = classify_empty_result(clean_db, DeploymentMode.READ_ONLY_SNAPSHOT)
        assert result["cause"] == EmptyResultCause.SERVING_FROZEN_SNAPSHOT.value
        assert result["level"] == HealthLevel.NOT_APPLICABLE.value
        assert result["empty_result_is_a_finding"] is True

    def test_a_snapshot_with_no_job_rows_is_still_not_failing(self, clean_db) -> None:  # noqa: ANN001
        result = classify_empty_result(clean_db, DeploymentMode.READ_ONLY_SNAPSHOT)
        assert result["level"] != HealthLevel.FAILING.value


class TestEveryCauseIsExplained:
    @pytest.mark.parametrize("cause", list(EmptyResultCause))
    def test_each_has_a_human_sentence(self, cause: EmptyResultCause) -> None:
        from pmvl_api.health import EXPLANATIONS

        assert EXPLANATIONS[cause].strip()
        assert len(EXPLANATIONS[cause]) > 40, "explanations must be actionable prose"
