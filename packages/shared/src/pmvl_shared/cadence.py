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
    """What this deployment actually is.

    ``AUTOMATED_SNAPSHOT_PIPELINE`` and ``CONTINUOUS_LIVE_PIPELINE`` are different
    things and the distinction is the whole point of this enum. A GitHub Actions
    workflow that wakes up, ingests, computes and publishes an immutable artefact
    is not a live pipeline: between runs nothing is watching, no worker is
    resident, and no query touches current data. Calling it live would restate the
    exact overclaim - printing a configured cadence as though it were running -
    that this module exists to prevent, one level up.
    """

    #: A scheduled publisher (GitHub Actions) ingests, computes and publishes a
    #: new read-only artefact periodically. No resident worker between runs.
    AUTOMATED_SNAPSHOT_PIPELINE = "automated_snapshot_pipeline"
    #: A resident worker and scheduler against a persistent read-write store.
    #: Nothing in this repository is deployed this way today.
    CONTINUOUS_LIVE_PIPELINE = "continuous_live_pipeline"
    #: Serving a frozen, validated artefact. No scheduler, no writes, no cadence.
    READ_ONLY_SNAPSHOT = "read_only_snapshot"
    #: Seeded synthetic data for demonstrations. Never presented as real.
    SYNTHETIC_DEMO = "synthetic_demo"
    #: A developer machine, where a scheduler may or may not be running.
    LOCAL_DEVELOPMENT = "local_development"

    @property
    def runs_scheduled_jobs(self) -> bool:
        """Whether jobs execute here at all, by any mechanism.

        True for both pipeline modes. It does NOT mean a component's configured
        cadence is honoured - a 1-minute arbitrage cadence inside a 60-minute
        publisher runs hourly - which is why `active_cadence` is derived from the
        publisher, not from this flag.

        LOCAL_DEVELOPMENT is excluded deliberately: a developer may or may not have
        the scheduler up, so the answer comes from observed runs rather than from
        the mode. Assuming "yes" is how a laptop reports itself as production.
        """
        return self in (
            DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE,
            DeploymentMode.CONTINUOUS_LIVE_PIPELINE,
        )

    @property
    def has_resident_worker(self) -> bool:
        """Whether a process is watching between scheduled runs."""
        return self is DeploymentMode.CONTINUOUS_LIVE_PIPELINE

    @property
    def description(self) -> str:
        return _MODE_DESCRIPTIONS[self]


_MODE_DESCRIPTIONS: dict["DeploymentMode", str] = {
    DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE: (
        "A scheduled publisher ingests current data, runs the research jobs and "
        "publishes a validated read-only artefact. Nothing runs between publisher "
        "runs."
    ),
    DeploymentMode.CONTINUOUS_LIVE_PIPELINE: (
        "A resident worker runs the schedule continuously against a persistent "
        "read-write store."
    ),
    DeploymentMode.READ_ONLY_SNAPSHOT: (
        "Serving a frozen validated artefact. No scheduler and no writes."
    ),
    DeploymentMode.SYNTHETIC_DEMO: (
        "Seeded synthetic data for demonstration. Never real market data."
    ),
    DeploymentMode.LOCAL_DEVELOPMENT: (
        "A developer machine. Whether jobs run is an observation, not a property "
        "of the deployment."
    ),
}


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

    def stall_threshold_seconds(self, effective_seconds: int | None = None) -> int:
        """When a missing run stops being a skipped tick and becomes an outage.

        Measured against the EFFECTIVE period, not the desired one. A job asking
        for 60 seconds inside a 3600-second publisher genuinely runs hourly, and
        judging it against its own wish would report every healthy hourly run as
        stalled ten minutes after it finished.
        """
        if self.stall_after_seconds is not None:
            return self.stall_after_seconds
        period = effective_seconds if effective_seconds is not None else self.interval_seconds
        if period is not None:
            # Three missed intervals, floored at ten minutes so a fast job does not
            # flap into STALLED on a single slow run.
            return max(period * 3, 600)
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

#: How often the external publisher actually runs the whole job set. Set from the
#: workflow schedule; a component's own cadence cannot beat this, because nothing
#: executes between publisher runs.
#:
#: This is the number that makes "arbitrage: every 1 minute" false on this
#: architecture. The component still *wants* a minute; the publisher gives it an
#: hour; and the honest report is both, labelled.
PUBLISHER_INTERVAL_SECONDS = 3600

#: Publication is deliberately rarer than computation. Committing an 8 MB artefact
#: at the research cadence would add tens of GB of binary history per month, and
#: the research product does not need hourly public updates.
PUBLICATION_INTERVAL_SECONDS = 6 * 3600


def effective_cadence_seconds(cadence: Cadence, mode: "DeploymentMode") -> int | None:
    """What this job's period really is on this deployment.

    A component asking for 60 seconds inside a 3600-second publisher gets 3600.
    Reporting the component's own number would repeat the original error in a new
    place: a truthful description of the code and a false description of the
    running system.
    """
    if not mode.runs_scheduled_jobs:
        return None
    if mode is DeploymentMode.CONTINUOUS_LIVE_PIPELINE:
        return cadence.interval_seconds
    if cadence.interval_seconds is None:
        return None
    return max(cadence.interval_seconds, PUBLISHER_INTERVAL_SECONDS)


def humanize_interval(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours} hour{'s' if hours != 1 else ''}"
    minutes = seconds // 60
    return f"every {minutes} minute{'s' if minutes != 1 else ''}"


#: Shown wherever a cadence appears on a deployment that does not run one. The
#: wording has to name the cadence as *configured* and the deployment as *not
#: running it*, in that order, because the number is what the eye lands on.
INACTIVE_CADENCE_NOTICE = (
    "Configured worker cadence - inactive in this snapshot deployment"
)

INACTIVE_CADENCE_NOTICE_BY_MODE: dict[DeploymentMode, str] = {
    DeploymentMode.READ_ONLY_SNAPSHOT: INACTIVE_CADENCE_NOTICE,
    DeploymentMode.AUTOMATED_SNAPSHOT_PIPELINE: (
        "Component cadences are desired intervals. This deployment runs the whole "
        "job set on the publisher's schedule, so no component runs more often than "
        "the publisher does."
    ),
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
