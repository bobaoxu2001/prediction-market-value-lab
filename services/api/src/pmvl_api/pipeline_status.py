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
    Cadence,
    DeploymentMode,
    SchedulerStatus,
    cadence_notice,
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

    # SQLite returns naive datetimes; ensure_utc is the repo's existing normaliser.
    last_ok_at = ensure_utc(last_ok.started_at) if last_ok else None
    last_failed_at = ensure_utc(last_failed.started_at) if last_failed else None

    next_expected = None
    if last_ok_at is not None and cadence.interval_seconds is not None:
        next_expected = last_ok_at + timedelta(seconds=cadence.interval_seconds)

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
            if age <= cadence.stall_threshold_seconds()
            else SchedulerStatus.STALLED
        )

    return {
        "job_name": cadence.job_name,
        "description": cadence.description,
        "configured_cadence": cadence.human,
        "configured_interval_seconds": cadence.interval_seconds,
        # `active_cadence` is None whenever nothing is executing the schedule. It is
        # a separate field from `configured_cadence` so a client cannot render one
        # while meaning the other.
        "active_cadence": cadence.human if status is SchedulerStatus.ACTIVE else None,
        "scheduler_status": status.value,
        "last_success_at": last_ok_at,
        "last_failure_at": last_failed_at,
        "last_error": (last_failed.error[:300] if last_failed and last_failed.error else ""),
        "next_expected_run": next_expected if status is SchedulerStatus.ACTIVE else None,
        "stall_threshold_seconds": cadence.stall_threshold_seconds(),
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
        "runs_scheduled_jobs": mode.runs_scheduled_jobs,
        "scheduler_status": overall.value,
        # Present and non-null on every deployment that is not executing the
        # schedule. A client that renders the cadence table must render this too.
        "cadence_notice": cadence_notice(mode),
        "jobs": jobs,
    }
