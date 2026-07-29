"""What the pipeline is *configured* to do, and whether anything is doing it.

These are two different facts and the product conflated them. ``/system`` published
a table headed "Update cadence" reading "market discovery: 10 minutes, orderbook
refresh: 3 minutes, arbitrage scan: 1 minute" - on a deployment with no worker, no
scheduler and a database frozen inside the bundle. Every number was a truthful
description of ``scheduler.py`` and a false description of the running system.

Two things caused it:

1. The cadences were written down twice - once as ``IntervalTrigger`` arguments in
   the scheduler, once as display strings in the API - so they could drift apart
   with nothing to catch it.
2. Neither copy knew whether a scheduler existed. A cadence is a property of a
   *running* scheduler; printing one without that context asserts something the
   deployment cannot back up.

This module is the single definition. The scheduler builds its triggers from it and
the API reports from it, so the two cannot disagree. Every cadence is reported
alongside the deployment mode that decides whether it is active, and a cadence is
never rendered as a bare interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeploymentMode(StrEnum):
    """What this process actually is, which decides whether cadences mean anything."""

    #: A worker and scheduler are running against a read-write operational store.
    LIVE_PIPELINE = "live_pipeline"
    #: Serving a frozen, validated artefact. No scheduler, no writes, no cadence.
    READ_ONLY_SNAPSHOT = "read_only_snapshot"
    #: Seeded synthetic data for demonstrations. Never presented as real.
    SYNTHETIC_DEMO = "synthetic_demo"
    #: A developer machine, where a scheduler may or may not be running.
    LOCAL_DEVELOPMENT = "local_development"

    @property
    def runs_scheduled_jobs(self) -> bool:
        """Whether a configured cadence is capable of being active here.

        LOCAL_DEVELOPMENT is deliberately excluded: a developer may or may not have
        the scheduler up, so the answer comes from observed job runs rather than
        from the mode. Assuming "yes" here is how a laptop reports itself as a
        live pipeline.
        """
        return self is DeploymentMode.LIVE_PIPELINE


class SchedulerStatus(StrEnum):
    """Whether the schedule is being executed, as distinct from being configured."""

    #: A scheduler process is running and jobs have executed recently.
    ACTIVE = "active"
    #: This deployment has no scheduler by construction.
    NOT_DEPLOYED = "not_deployed"
    #: A scheduler should exist, but no job has run within its expected window.
    STALLED = "stalled"
    #: Cannot be determined from inside this process.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Cadence:
    """One scheduled job's configured timing.

    ``interval_seconds`` and ``cron`` are mutually exclusive: a job either repeats
    on an interval or fires at a wall-clock time.
    """

    job_name: str
    description: str
    interval_seconds: int | None = None
    cron: str | None = None
    #: How long after its due time a run may be late before the job is considered
    #: stalled. Generous relative to the interval, because a single skipped tick is
    #: normal and should not read as an outage.
    stall_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if (self.interval_seconds is None) == (self.cron is None):
            raise ValueError(
                f"{self.job_name}: exactly one of interval_seconds or cron is required"
            )

    @property
    def human(self) -> str:
        """The interval as prose, e.g. "every 3 minutes"."""
        if self.cron is not None:
            return self.cron
        seconds = self.interval_seconds or 0
        if seconds % 3600 == 0:
            hours = seconds // 3600
            return f"every {hours} hour{'s' if hours != 1 else ''}"
        minutes = seconds // 60
        return f"every {minutes} minute{'s' if minutes != 1 else ''}"

    def stall_threshold_seconds(self) -> int:
        """When a missing run stops being a skipped tick and becomes an outage."""
        if self.stall_after_seconds is not None:
            return self.stall_after_seconds
        if self.interval_seconds is not None:
            # Three missed intervals, floored at ten minutes so a one-minute job does
            # not flap into STALLED on a single slow run.
            return max(self.interval_seconds * 3, 600)
        return 26 * 3600  # a daily cron is late once it misses more than a day


#: The pipeline's schedule. The scheduler builds triggers from this and the API
#: reports from it; there is no second copy to drift.
CADENCES: tuple[Cadence, ...] = (
    Cadence(
        "ingest",
        "Market and event discovery",
        interval_seconds=600,
    ),
    Cadence(
        "orderbooks",
        "Orderbook refresh",
        interval_seconds=180,
    ),
    Cadence(
        "arbitrage",
        "Arbitrage scan",
        interval_seconds=60,
    ),
    Cadence(
        "score",
        "Independent probability scoring",
        interval_seconds=7200,
    ),
    Cadence(
        "rank",
        "Opportunity ranking",
        interval_seconds=3600,
    ),
    Cadence(
        "settle",
        "Settlement synchronisation",
        interval_seconds=1800,
    ),
    Cadence(
        "snapshot",
        "Daily recommendation snapshot",
        cron="daily at the configured snapshot hour (UTC)",
        stall_after_seconds=26 * 3600,
    ),
    Cadence(
        "backtest",
        "Walk-forward backtest",
        cron="daily, one hour after the snapshot",
        stall_after_seconds=26 * 3600,
    ),
    Cadence(
        "prune",
        "Orderbook retention",
        cron="daily at 04:30 UTC",
        stall_after_seconds=26 * 3600,
    ),
)

CADENCE_BY_JOB: dict[str, Cadence] = {c.job_name: c for c in CADENCES}

#: Shown wherever a cadence appears on a deployment that does not run one. The
#: wording has to name the cadence as *configured* and the deployment as *not
#: running it*, in that order, because the number is what the eye lands on.
INACTIVE_CADENCE_NOTICE = (
    "Configured worker cadence - inactive in this snapshot deployment"
)

INACTIVE_CADENCE_NOTICE_BY_MODE: dict[DeploymentMode, str] = {
    DeploymentMode.READ_ONLY_SNAPSHOT: INACTIVE_CADENCE_NOTICE,
    DeploymentMode.SYNTHETIC_DEMO: (
        "Configured worker cadence - inactive; this deployment serves synthetic "
        "demonstration data"
    ),
    DeploymentMode.LOCAL_DEVELOPMENT: (
        "Configured worker cadence - active only while a local scheduler is running"
    ),
}


def cadence_notice(mode: DeploymentMode) -> str | None:
    """The caveat that must accompany a cadence table, or None when it is live."""
    return INACTIVE_CADENCE_NOTICE_BY_MODE.get(mode)
