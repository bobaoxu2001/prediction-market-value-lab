"""Observability of the schedule, after a 33-hour outage nothing alarmed on.

On 2026-08-29 the collector was stopped by a signal and its launchd job was left
in the user domain's *disabled* list, so `KeepAlive` never respawned it. Nothing
ran for 33 hours. Two registry defects meant the health endpoint could not have
said so:

* `prune` was declared as "daily at 04:30 UTC" with a 26-hour stall threshold
  while the scheduler had long been running it hourly, so an hourly job had to be
  dead for more than a day before it read as stalled;
* `calibrate` had no registry entry at all, so `job_status` reported UNKNOWN
  rather than STALLED and no alarm could ever fire for it.

The first two tests here are the general guards - they compare the registry
against the scheduler rather than against a hand-written list, so a future job
cannot be added on one side only.

The misfire tests cover the other half of the same outage: three daily cron jobs
were discarded because a 900-1000s macOS Maintenance Sleep pushed them past the
300s default grace.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pmvl_shared.cadence import CADENCE_BY_JOB

SCHEDULER_SRC = (
    Path(__file__).resolve().parents[1]
    / "services/worker/src/pmvl_worker/scheduler.py"
)

#: The longest macOS Maintenance Sleep observed across the 2026-08-29 window.
OBSERVED_MAINTENANCE_SLEEP_SECONDS = 1004

#: snapshot -> backtest -> calibrate are one hour apart and each reads the
#: previous one's output, so a recovered run must not be able to overtake its
#: successor.
CRON_JOB_SPACING_SECONDS = 3600


def _scheduler_jobs() -> list[tuple[str, str, str]]:
    """(job_id, trigger_kind, block source) for every add_job in the scheduler."""
    source = SCHEDULER_SRC.read_text()
    body = source[source.index("def build_scheduler"):]
    jobs = []
    for block in re.findall(r"scheduler\.add_job\((.*?)\n    \)", body, re.S):
        found = re.search(r'id="([^"]+)"', block)
        assert found is not None, f"add_job without an id: {block[:80]}"
        kind = "cron" if "_utc_cron" in block else "interval"
        jobs.append((found.group(1), kind, block))
    return jobs


def test_every_scheduled_job_has_a_cadence_entry() -> None:
    """A job the registry does not know about can never be reported stalled."""
    missing = [j for j, _, _ in _scheduler_jobs() if j not in CADENCE_BY_JOB]
    assert not missing, (
        f"scheduled but absent from CADENCE_BY_JOB: {missing}. "
        "job_status() returns UNKNOWN for these, so no alarm can fire."
    )


def test_registry_trigger_kind_matches_the_scheduler() -> None:
    """An interval job declared as a daily cron gets a daily stall threshold."""
    wrong = []
    for job_id, kind, _ in _scheduler_jobs():
        cadence = CADENCE_BY_JOB.get(job_id)
        if cadence is None:
            continue
        declared = "cron" if cadence.cron is not None else "interval"
        if declared != kind:
            wrong.append(f"{job_id}: scheduler={kind} registry={declared}")
    assert not wrong, "; ".join(wrong)


def test_prune_is_declared_hourly_not_daily() -> None:
    """The specific drift that hid a 33-hour retention outage."""
    prune = CADENCE_BY_JOB["prune"]
    assert prune.cron is None
    assert prune.interval_seconds == 3600
    assert prune.stall_threshold_seconds() <= 6 * 3600, (
        "an hourly job must not carry a daily stall threshold"
    )


def test_hourly_jobs_never_carry_a_daily_stall_threshold() -> None:
    """The general form of the prune defect, across the whole registry."""
    offenders = [
        c.job_name
        for c in CADENCE_BY_JOB.values()
        if c.interval_seconds is not None
        and c.interval_seconds <= 3600
        and c.stall_threshold_seconds() > 6 * 3600
    ]
    assert not offenders, offenders


@pytest.mark.parametrize(
    "job_id", ["snapshot", "backtest", "calibrate", "collection_health"]
)
def test_daily_cron_survives_a_maintenance_sleep(job_id: str) -> None:
    """A short host sleep must not delete a daily validation opportunity."""
    blocks = {j: block for j, _, block in _scheduler_jobs()}
    grace = re.search(r"misfire_grace_time=(\S+?),", blocks[job_id])
    assert grace is not None, f"{job_id} relies on the 300s default grace"
    from pmvl_worker import scheduler as sched

    seconds = getattr(sched, grace.group(1)) if grace.group(1).isidentifier() \
        else int(grace.group(1))
    assert seconds > OBSERVED_MAINTENANCE_SLEEP_SECONDS, (
        f"{job_id}: {seconds}s does not cover the observed "
        f"{OBSERVED_MAINTENANCE_SLEEP_SECONDS}s maintenance sleep"
    )
    assert seconds <= CRON_JOB_SPACING_SECONDS, (
        f"{job_id}: a grace above {CRON_JOB_SPACING_SECONDS}s lets a recovered "
        "run overtake the job that reads its output"
    )


def test_cron_triggers_are_utc() -> None:
    """Every cron job goes through _utc_cron, never a bare CronTrigger."""
    body = SCHEDULER_SRC.read_text()
    body = body[body.index("def build_scheduler"):]
    assert "CronTrigger(" not in body, (
        "a cron trigger is constructed directly in build_scheduler; route it "
        "through _utc_cron so it cannot inherit the host zone"
    )
