"""Whether the configured schedule is actually being executed.

The API used to publish the cadence table alone. A reader saw "arbitrage scan: 1
minute" and had no way to learn that no arbitrage scan had run since the artefact
was built, because both facts render identically when only one is printed.

Every cadence reported here carries its observed state: when it last succeeded,
when it last failed, when the next run is due, and whether the deployment runs a
scheduler at all. A cadence with no observed runs on a deployment that cannot run
one is reported as ``not_deployed`` - not as "stalled", which would imply something
broke, and not silently, which is what happened before.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.cadence import (
    CADENCES,
    PUBLICATION_INTERVAL_SECONDS,
    PUBLISHER_INTERVAL_SECONDS,
    Cadence,
    DeploymentMode,
    SchedulerStatus,
    cadence_notice,
    effective_cadence_seconds,
    humanize_interval,
)
from pmvl_shared.enums import JobStatus
from pmvl_shared.timeutil import age_seconds, ensure_utc

from pmvl_markets.db_models import JobRun


def _latest(session: Session, job_name: str, *, statuses: tuple[str, ...]) -> JobRun | None:
    return session.scalar(
        select(JobRun)
        .where(JobRun.job_name == job_name, JobRun.status.in_(statuses))
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )


#: Statuses that mean the job produced usable output. PARTIAL_SUCCESS counts: a run
#: that lost one provider but wrote the rest is not a stalled scheduler.
_OK_STATUSES = (JobStatus.SUCCESS.value, JobStatus.PARTIAL_SUCCESS.value)


def job_status(session: Session, cadence: Cadence, mode: DeploymentMode) -> dict[str, Any]:
    """Observed state of one scheduled job, next to its configured cadence."""
    last_ok = _latest(session, cadence.job_name, statuses=_OK_STATUSES)
    last_failed = _latest(session, cadence.job_name, statuses=(JobStatus.FAILED.value,))

    # The previous successful run, so the OBSERVED interval is a measurement rather
    # than a restatement of the configured one.
    previous_ok = None
    if last_ok is not None:
        previous_ok = session.scalar(
            select(JobRun)
            .where(
                JobRun.job_name == cadence.job_name,
                JobRun.status.in_(_OK_STATUSES),
                JobRun.started_at < last_ok.started_at,
            )
            .order_by(JobRun.started_at.desc())
            .limit(1)
        )
    previous_ok_at = ensure_utc(previous_ok.started_at) if previous_ok else None

    # SQLite returns naive datetimes; ensure_utc is the repo's existing normaliser.
    last_ok_at = ensure_utc(last_ok.started_at) if last_ok else None
    last_failed_at = ensure_utc(last_failed.started_at) if last_failed else None

    effective_seconds = effective_cadence_seconds(cadence, mode)

    next_expected = None
    if last_ok_at is not None and effective_seconds is not None:
        next_expected = last_ok_at + timedelta(seconds=effective_seconds)

    if not mode.runs_scheduled_jobs:
        # No scheduler exists here by construction. Reporting STALLED would claim a
        # failure; reporting ACTIVE would be a lie. The distinction is the point.
        status = SchedulerStatus.NOT_DEPLOYED
    elif last_ok_at is None:
        status = SchedulerStatus.STALLED
    else:
        age = age_seconds(last_ok_at) or 0.0
        status = (
            SchedulerStatus.ACTIVE
            if age <= cadence.stall_threshold_seconds(effective_seconds)
            else SchedulerStatus.STALLED
        )

    # Three different numbers, none of which may stand in for another:
    #   desired   - what the component asks for
    #   effective - what the publisher's schedule actually permits
    #   observed  - the gap between the last two real runs
    # A 1-minute arbitrage cadence inside a 60-minute publisher is hourly, and
    # reporting the component's own figure as "active" would repeat the original
    # overclaim in a new place.
    observed_seconds = None
    if last_ok_at is not None and previous_ok_at is not None:
        observed_seconds = int((last_ok_at - previous_ok_at).total_seconds())

    return {
        "job_name": cadence.job_name,
        "description": cadence.description,
        "desired_cadence": cadence.human,
        "desired_interval_seconds": cadence.interval_seconds,
        "effective_cadence": humanize_interval(effective_seconds),
        "effective_interval_seconds": effective_seconds,
        "observed_interval_seconds": observed_seconds,
        "observed_cadence": humanize_interval(observed_seconds),
        # Retained under the old names for existing clients. `configured_cadence`
        # is the component's desire, never the running period.
        "configured_cadence": cadence.human,
        "configured_interval_seconds": cadence.interval_seconds,
        # `active_cadence` is the EFFECTIVE period, and None whenever nothing is
        # executing the schedule.
        "active_cadence": (
            humanize_interval(effective_seconds)
            if status is SchedulerStatus.ACTIVE
            else None
        ),
        "scheduler_status": status.value,
        "last_success_at": last_ok_at,
        "last_failure_at": last_failed_at,
        "last_error": (last_failed.error[:300] if last_failed and last_failed.error else ""),
        "next_expected_run": next_expected if status is SchedulerStatus.ACTIVE else None,
        "stall_threshold_seconds": cadence.stall_threshold_seconds(effective_seconds),
    }


def scheduler_status(jobs: list[dict[str, Any]], mode: DeploymentMode) -> SchedulerStatus:
    """One overall verdict from the per-job states."""
    if not mode.runs_scheduled_jobs:
        return SchedulerStatus.NOT_DEPLOYED
    if not jobs:
        return SchedulerStatus.UNKNOWN
    states = {j["scheduler_status"] for j in jobs}
    if SchedulerStatus.STALLED.value in states:
        return SchedulerStatus.STALLED
    if states == {SchedulerStatus.ACTIVE.value}:
        return SchedulerStatus.ACTIVE
    return SchedulerStatus.UNKNOWN


def pipeline_status(session: Session, mode: DeploymentMode) -> dict[str, Any]:
    """The configured schedule and what is actually running it."""
    jobs = [job_status(session, c, mode) for c in CADENCES]
    overall = scheduler_status(jobs, mode)
    return {
        "deployment_mode": mode.value,
        "deployment_mode_description": mode.description,
        # The four lines a reader needs to understand what this deployment is.
        "pipeline_type": (
            "Automated snapshot publication"
            if mode is DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE
            else "Continuous live pipeline"
            if mode is DeploymentMode.CONTINUOUS_LIVE_PIPELINE
            else "None"
        ),
        "public_serving_mode": "Read-only snapshot",
        "publisher": (
            "GitHub Actions" if mode.runs_scheduled_jobs else "None"
        ),
        "persistent_live_worker": mode.has_resident_worker,
        "publisher_interval_seconds": (
            PUBLISHER_INTERVAL_SECONDS if mode.runs_scheduled_jobs else None
        ),
        "publication_interval_seconds": (
            PUBLICATION_INTERVAL_SECONDS if mode.runs_scheduled_jobs else None
        ),
        "runs_scheduled_jobs": mode.runs_scheduled_jobs,
        "scheduler_status": overall.value,
        # Present and non-null on every deployment that is not executing the
        # schedule. A client that renders the cadence table must render this too.
        "cadence_notice": cadence_notice(mode),
        "jobs": jobs,
    }
