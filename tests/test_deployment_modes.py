"""A configured cadence must never be presented as a running one.

`/system` published a table headed "Update cadence" reading "arbitrage scan: 1
minute" on a deployment with no worker, no scheduler, and a database frozen inside
the bundle. Every number described `scheduler.py` correctly and the running system
not at all.

These tests fail if the cadence is ever reported without the deployment context
that decides whether it means anything.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from pmvl_shared.cadence import (
    CADENCE_BY_JOB,
    CADENCES,
    INACTIVE_CADENCE_NOTICE,
    Cadence,
    DeploymentMode,
    SchedulerStatus,
    cadence_notice,
)
from pmvl_shared.enums import JobStatus
from pmvl_shared.timeutil import utcnow

from pmvl_api.pipeline_status import job_status, pipeline_status, scheduler_status


class TestDeploymentModes:
    def test_both_pipeline_modes_run_scheduled_jobs(self) -> None:
        assert DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE.runs_scheduled_jobs
        assert not DeploymentMode.READ_ONLY_SNAPSHOT.runs_scheduled_jobs
        assert not DeploymentMode.SYNTHETIC_DEMO.runs_scheduled_jobs

    def test_local_development_does_not_assume_a_scheduler(self) -> None:
        """A laptop may or may not have the worker up.

        Assuming it does is how a developer machine reported itself as a live
        pipeline, which is the same misstatement in a different place.
        """
        assert not DeploymentMode.LOCAL_DEVELOPMENT.runs_scheduled_jobs

    @pytest.mark.parametrize(
        "mode",
        [
            DeploymentMode.READ_ONLY_SNAPSHOT,
            DeploymentMode.SYNTHETIC_DEMO,
            DeploymentMode.LOCAL_DEVELOPMENT,
        ],
    )
    def test_every_non_live_mode_carries_a_notice(self, mode: DeploymentMode) -> None:
        notice = cadence_notice(mode)
        assert notice, f"{mode} renders cadences with no caveat"
        assert "Configured worker cadence" in notice

    def test_a_continuous_live_pipeline_has_no_notice(self) -> None:
        """The caveat must not appear where it would be false.

        Only a resident worker honours a component's own cadence, so only that
        mode may print one without qualification.
        """
        assert cadence_notice(DeploymentMode.CONTINUOUS_LIVE_PIPELINE) is None

    def test_the_snapshot_publisher_carries_its_own_caveat(self) -> None:
        """Its jobs DO run, but not at the intervals the components ask for."""
        notice = cadence_notice(DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE)
        assert notice and "desired intervals" in notice

    def test_only_a_continuous_pipeline_has_a_resident_worker(self) -> None:
        assert DeploymentMode.CONTINUOUS_LIVE_PIPELINE.has_resident_worker
        assert not DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE.has_resident_worker

    def test_the_snapshot_wording_is_exact(self) -> None:
        """Pinned because this sentence is the whole correction."""
        assert (
            INACTIVE_CADENCE_NOTICE
            == "Configured worker cadence - inactive in this snapshot deployment"
        )


class TestCadenceIsDefinedOnce:
    def test_scheduler_intervals_come_from_the_shared_definition(self) -> None:
        """The interval existed twice - as a trigger and as a display string - and
        nothing kept them equal."""
        import sys
        from pathlib import Path

        worker_src = Path(__file__).resolve().parents[1] / "services/worker/src"
        if str(worker_src) not in sys.path:
            sys.path.insert(0, str(worker_src))
        from pmvl_worker.scheduler import build_scheduler

        jobs = {j.id: j for j in build_scheduler().get_jobs()}
        for cadence in CADENCES:
            if cadence.interval_seconds is None:
                continue
            trigger = jobs[cadence.job_name].trigger
            assert trigger.interval == timedelta(seconds=cadence.interval_seconds), (
                f"{cadence.job_name}: scheduler and shared cadence disagree"
            )

    def test_human_rendering_is_readable(self) -> None:
        assert CADENCE_BY_JOB["arbitrage"].human == "every 1 minute"
        assert CADENCE_BY_JOB["orderbooks"].human == "every 3 minutes"
        assert CADENCE_BY_JOB["score"].human == "every 2 hours"

    def test_a_cadence_needs_exactly_one_timing_source(self) -> None:
        with pytest.raises(ValueError):
            Cadence("x", "both", interval_seconds=60, cron="daily")
        with pytest.raises(ValueError):
            Cadence("x", "neither")


class TestActiveVersusConfigured:
    def test_snapshot_reports_configured_but_never_active(self, clean_db) -> None:  # noqa: ANN001
        status = pipeline_status(clean_db, DeploymentMode.READ_ONLY_SNAPSHOT)

        assert status["cadence_notice"] == INACTIVE_CADENCE_NOTICE
        assert status["runs_scheduled_jobs"] is False
        assert status["scheduler_status"] == SchedulerStatus.NOT_DEPLOYED.value
        for job in status["jobs"]:
            assert job["configured_cadence"], "the configured value is still reported"
            assert job["active_cadence"] is None, (
                f"{job['job_name']}: an active cadence on a deployment with no worker"
            )
            assert job["scheduler_status"] == SchedulerStatus.NOT_DEPLOYED.value

    def test_a_missing_run_on_a_snapshot_is_not_reported_as_stalled(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """STALLED means something broke. Nothing broke: there is no scheduler."""
        status = pipeline_status(clean_db, DeploymentMode.READ_ONLY_SNAPSHOT)
        assert SchedulerStatus.STALLED.value not in {
            j["scheduler_status"] for j in status["jobs"]
        }

    def test_a_recent_run_on_a_pipeline_is_active(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.db_models import JobRun

        clean_db.add(
            JobRun(
                job_name="arbitrage",
                status=JobStatus.SUCCESS.value,
                started_at=utcnow() - timedelta(seconds=30),
                finished_at=utcnow(),
            )
        )
        clean_db.flush()

        job = job_status(
            clean_db, CADENCE_BY_JOB["arbitrage"], DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE
        )
        assert job["scheduler_status"] == SchedulerStatus.ACTIVE.value
        # The component asks for a minute; the publisher runs hourly; the active
        # cadence is the one that actually happens. Reporting "every 1 minute"
        # here would repeat the original overclaim one level down.
        assert job["desired_cadence"] == "every 1 minute"
        assert job["active_cadence"] == "every 1 hour"
        assert job["effective_interval_seconds"] == 3600
        assert job["next_expected_run"] is not None

    def test_an_overdue_run_on_a_pipeline_is_stalled(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.db_models import JobRun

        clean_db.add(
            JobRun(
                job_name="arbitrage",
                status=JobStatus.SUCCESS.value,
                started_at=utcnow() - timedelta(hours=6),
                finished_at=utcnow() - timedelta(hours=6),
            )
        )
        clean_db.flush()

        job = job_status(
            clean_db, CADENCE_BY_JOB["arbitrage"], DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE
        )
        # Six hours against a three-hour tolerance derived from the hourly
        # publisher - not from the component's one-minute wish, which would call
        # every healthy hourly run stalled ten minutes after it finished.
        assert job["scheduler_status"] == SchedulerStatus.STALLED.value
        assert job["active_cadence"] is None
        assert job["next_expected_run"] is None

    def test_a_partial_success_still_counts_as_the_scheduler_running(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """A run that lost one provider is degraded output, not a dead scheduler."""
        from pmvl_markets.db_models import JobRun

        clean_db.add(
            JobRun(
                job_name="ingest",
                status=JobStatus.PARTIAL_SUCCESS.value,
                started_at=utcnow() - timedelta(minutes=2),
                finished_at=utcnow(),
            )
        )
        clean_db.flush()

        job = job_status(
            clean_db, CADENCE_BY_JOB["ingest"], DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE
        )
        assert job["scheduler_status"] == SchedulerStatus.ACTIVE.value

    def test_one_stalled_job_stalls_the_overall_verdict(self) -> None:
        jobs = [
            {"job_name": "a", "scheduler_status": SchedulerStatus.ACTIVE.value},
            {"job_name": "b", "scheduler_status": SchedulerStatus.STALLED.value},
        ]
        assert (
            scheduler_status(jobs, DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE)
            is SchedulerStatus.STALLED
        )


class TestSystemRouteWording:
    @pytest.fixture()
    def client(self, clean_db):  # noqa: ANN001, ANN201
        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        clean_db.commit()
        return TestClient(app)

    def test_update_frequencies_never_ship_without_their_notice(
        self, client, monkeypatch  # noqa: ANN001
    ) -> None:
        from pmvl_api.routers import system as system_router

        monkeypatch.setattr(system_router, "SNAPSHOT_MODE", True)
        payload = client.get("/system").json()["data"]

        assert payload["update_frequencies"], "cadences are still reported"
        assert payload["update_frequencies_notice"] == INACTIVE_CADENCE_NOTICE
        assert payload["pipeline"]["cadence_notice"] == INACTIVE_CADENCE_NOTICE

    def test_pipeline_block_separates_configured_from_active(self, client) -> None:  # noqa: ANN001
        pipeline = client.get("/system").json()["data"]["pipeline"]
        for job in pipeline["jobs"]:
            assert "configured_cadence" in job
            assert "active_cadence" in job
            assert "scheduler_status" in job
            assert "last_success_at" in job
            assert "last_failure_at" in job
            assert "next_expected_run" in job

    def test_every_scheduled_job_is_reported(self, client) -> None:  # noqa: ANN001
        reported = {j["job_name"] for j in client.get("/system").json()["data"]["pipeline"]["jobs"]}
        assert reported == {c.job_name for c in CADENCES}


class TestThreeCadencesAreDistinct:
    """Desired, effective and observed are three different numbers.

    A component asking for 60 seconds inside a 3600-second publisher runs hourly.
    Publishing the component's own figure as the active cadence would repeat, one
    level down, exactly the overclaim this module was written to remove.
    """

    def test_the_publisher_floors_a_faster_component(self) -> None:
        from pmvl_shared.cadence import effective_cadence_seconds

        assert (
            effective_cadence_seconds(
                CADENCE_BY_JOB["arbitrage"], DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE
            )
            == 3600
        )

    def test_a_slower_component_keeps_its_own_period(self) -> None:
        """The publisher is a floor, not an override: a two-hourly job does not
        become hourly just because the publisher wakes up more often."""
        from pmvl_shared.cadence import effective_cadence_seconds

        assert (
            effective_cadence_seconds(
                CADENCE_BY_JOB["score"], DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE
            )
            == 7200
        )

    def test_a_resident_worker_honours_the_component_cadence(self) -> None:
        from pmvl_shared.cadence import effective_cadence_seconds

        assert (
            effective_cadence_seconds(
                CADENCE_BY_JOB["arbitrage"], DeploymentMode.CONTINUOUS_LIVE_PIPELINE
            )
            == 60
        )

    def test_a_snapshot_has_no_effective_cadence_at_all(self) -> None:
        from pmvl_shared.cadence import effective_cadence_seconds

        assert (
            effective_cadence_seconds(
                CADENCE_BY_JOB["arbitrage"], DeploymentMode.READ_ONLY_SNAPSHOT
            )
            is None
        )

    def test_observed_interval_is_measured_from_two_real_runs(self, clean_db) -> None:  # noqa: ANN001
        """Measured, not restated. The observed gap is the only one of the three
        that is evidence rather than intent."""
        from pmvl_markets.db_models import JobRun

        now = utcnow()
        for minutes in (128, 64, 0):
            clean_db.add(
                JobRun(
                    job_name="arbitrage",
                    status=JobStatus.SUCCESS.value,
                    started_at=now - timedelta(minutes=minutes),
                    finished_at=now - timedelta(minutes=minutes),
                )
            )
        clean_db.flush()

        job = job_status(
            clean_db,
            CADENCE_BY_JOB["arbitrage"],
            DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE,
        )
        assert job["observed_interval_seconds"] == 64 * 60
        assert job["observed_cadence"] == "every 64 minutes"
        # All three coexist and disagree, which is the honest state.
        assert job["desired_cadence"] == "every 1 minute"
        assert job["effective_cadence"] == "every 1 hour"

    def test_observed_is_absent_until_there_are_two_runs(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.db_models import JobRun

        clean_db.add(
            JobRun(
                job_name="arbitrage",
                status=JobStatus.SUCCESS.value,
                started_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        clean_db.flush()
        job = job_status(
            clean_db,
            CADENCE_BY_JOB["arbitrage"],
            DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE,
        )
        assert job["observed_interval_seconds"] is None


class TestDeploymentSelfDescription:
    def test_the_four_headline_lines_are_reported(self, clean_db) -> None:  # noqa: ANN001
        status = pipeline_status(
            clean_db, DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE
        )
        assert status["pipeline_type"] == "Automated snapshot publication"
        assert status["public_serving_mode"] == "Read-only snapshot"
        assert status["publisher"] == "GitHub Actions"
        assert status["persistent_live_worker"] is False

    def test_a_snapshot_names_no_publisher(self, clean_db) -> None:  # noqa: ANN001
        status = pipeline_status(clean_db, DeploymentMode.READ_ONLY_SNAPSHOT)
        assert status["publisher"] == "None"
        assert status["persistent_live_worker"] is False
        assert status["publisher_interval_seconds"] is None

    def test_nothing_claims_a_resident_worker_that_does_not_exist(self) -> None:
        """No deployment in this repository has one, and the enum must not let a
        configuration accidentally assert otherwise."""
        from pmvl_shared.config import get_settings

        assert get_settings().deployment_mode is not DeploymentMode.CONTINUOUS_LIVE_PIPELINE
