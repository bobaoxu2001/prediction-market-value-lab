"""APScheduler-based recurring jobs.

Cadences come from :mod:`pmvl_shared.cadence`, which the API also reports from.
They were previously written here as trigger arguments and again in the API as
display strings; two copies of the same number drift, and the reader has no way to
tell which one is real. Two deliberate choices:

* ``max_instances=1`` and ``coalesce=True`` on every job. If an ingest overruns its
  interval, the next tick is skipped rather than queued - two concurrent ingests
  would double the API load exactly when the venue is already slow.
* ``misfire_grace_time`` is generous for slow jobs so a brief pause does not silently
  drop a scheduled run.

APScheduler was chosen over Celery/Airflow because the workload is a handful of
periodic tasks against one database. A broker and worker pool would be infrastructure
without a corresponding benefit.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any, Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from pmvl_shared.cadence import CADENCE_BY_JOB
from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import get_logger, setup_logging

from . import jobs

log = get_logger(__name__)


def _sync(coro_factory: Callable[[], Any]) -> Callable[[], None]:
    """Adapt an async job to APScheduler's synchronous executor."""

    def runner() -> None:
        try:
            asyncio.run(coro_factory())
        except jobs.JobSkipped as exc:  # a skip is not a failure
            log.info("%s", exc)
        except Exception as exc:  # noqa: BLE001 - a failed job must not kill the scheduler
            log.error("scheduled job failed: %s", exc)

    return runner


def _guard(fn: Callable[[], Any]) -> Callable[[], None]:
    def runner() -> None:
        try:
            fn()
        except jobs.JobSkipped as exc:
            log.info("%s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("scheduled job failed: %s", exc)

    return runner


def _utc_cron(**fields: int) -> CronTrigger:
    """A cron trigger that actually fires at the UTC time it names.

    ``CronTrigger(hour=5)`` binds ``tzlocal.get_localzone()`` at construction,
    and the scheduler's own ``timezone="UTC"`` does NOT override it - APScheduler
    applies the scheduler timezone only when trigger fields are passed as kwargs
    to ``add_job``. Passing a constructed trigger, as every job here does, silently
    inherits the host zone.

    On a host in Asia/Shanghai that shifted every documented-UTC cron by +8 hours.
    `map_stations`, documented "daily at 05:00 UTC", ran at 21:00 UTC; the Aug-27
    mapping opportunity was lost entirely and the Aug-28 forecast dependency chain
    sat at 0/7 while the scheduler reported itself healthy. `collection_health`,
    documented 23:50 UTC, ran at 15:50 UTC - same offset, independently confirming
    the cause.

    Every trigger goes through here so the binding cannot be forgotten once and
    quietly diverge from its own documentation again.
    """
    return CronTrigger(timezone="UTC", **fields)


def build_scheduler() -> BlockingScheduler:
    settings = get_settings()
    scheduler = BlockingScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        timezone="UTC",
    )

    # Market discovery: metadata changes slowly.
    scheduler.add_job(
        _sync(lambda: jobs.job_ingest()),
        IntervalTrigger(seconds=CADENCE_BY_JOB["ingest"].interval_seconds),
        id="ingest",
        name="market discovery + orderbooks",
    )

    # Orderbooks for the markets that can actually rank. A quote older than a few
    # minutes is not an executable price, which is the whole premise of the platform.
    scheduler.add_job(
        _sync(lambda: jobs.job_orderbooks()),
        IntervalTrigger(seconds=CADENCE_BY_JOB["orderbooks"].interval_seconds),
        id="orderbooks",
        name="orderbook refresh",
    )

    # Arbitrage is the most time-sensitive scan: these windows close in seconds.
    scheduler.add_job(
        _guard(jobs.job_arbitrage),
        IntervalTrigger(seconds=CADENCE_BY_JOB["arbitrage"].interval_seconds),
        id="arbitrage",
        name="arbitrage scan",
    )

    # Model scoring is expensive (external data per market) and the inputs move on
    # the scale of hours, so a 2-hour cadence with an hourly re-rank is sufficient.
    scheduler.add_job(
        _sync(lambda: jobs.job_score()),
        IntervalTrigger(seconds=CADENCE_BY_JOB["score"].interval_seconds),
        id="score",
        name="probability ensemble",
    )
    scheduler.add_job(
        _sync(lambda: jobs.job_rank()),
        IntervalTrigger(seconds=CADENCE_BY_JOB["rank"].interval_seconds),
        id="rank",
        name="ranking + recommendations",
    )

    scheduler.add_job(
        _sync(lambda: jobs.job_settle()),
        IntervalTrigger(seconds=CADENCE_BY_JOB["settle"].interval_seconds),
        id="settle",
        name="settlement sync + grading",
    )

    # The immutable daily record, at a configurable wall-clock time.
    scheduler.add_job(
        _guard(jobs.job_snapshot),
        _utc_cron(
            hour=settings.daily_snapshot_hour_utc,
            minute=settings.daily_snapshot_minute_utc,
        ),
        id="snapshot",
        name="daily recommendation snapshot",
    )

    # Backtest after the snapshot, so the day's record is included.
    scheduler.add_job(
        _guard(jobs.job_backtest),
        _utc_cron(
            hour=(settings.daily_snapshot_hour_utc + 1) % 24,
            minute=settings.daily_snapshot_minute_utc,
        ),
        id="backtest",
        name="walk-forward backtest",
    )

    # Calibration after the backtest, for the same reason the backtest follows the
    # snapshot: it reads the day's settled record. Scheduled from day one even
    # though it will refuse to fit for weeks - a recorded refusal each day is the
    # audit trail for why estimates are still uncalibrated, and the day it starts
    # fitting is then visible rather than needing to be remembered.
    scheduler.add_job(
        _guard(jobs.job_calibrate),
        _utc_cron(
            hour=(settings.daily_snapshot_hour_utc + 2) % 24,
            minute=settings.daily_snapshot_minute_utc,
        ),
        id="calibrate",
        name="calibration fit",
    )

    scheduler.add_job(
        _guard(jobs.job_prune),
        # Hourly, not daily.
        #
        # A daily pass has to delete a whole day of levels in one window, and a
        # missed pass doubles that. Hourly keeps each run small enough that no
        # batch competes with a capture cycle, and makes a skipped run harmless.
        IntervalTrigger(seconds=3600),
        id="prune",
        name="orderbook retention",
        misfire_grace_time=900,
    )

    # --- research collection -------------------------------------------------
    #
    # `max_instances=1` and `coalesce=True` come from the defaults above and
    # matter more here than anywhere else: `research_books` holds its slot for
    # the length of an issuance window, and a second concurrent invocation would
    # double the venue load and write duplicate captures at the same instant.
    # The archive's uniqueness constraint makes a duplicate harmless, but the
    # request would still have been made.
    scheduler.add_job(
        _sync(jobs.job_forecast_observations),
        IntervalTrigger(
            seconds=CADENCE_BY_JOB["forecast_observations"].interval_seconds
        ),
        id="forecast_observations",
        name="NWS gridpoint forecasts",
        misfire_grace_time=300,
    )

    scheduler.add_job(
        _sync(jobs.job_station_observations),
        IntervalTrigger(
            seconds=CADENCE_BY_JOB["station_observations"].interval_seconds
        ),
        id="station_observations",
        name="settlement-station observations",
        # An observation missed is an observation lost; a late run is still
        # worth making, so the grace is generous.
        misfire_grace_time=900,
    )

    scheduler.add_job(
        _sync(jobs.job_research_books),
        IntervalTrigger(
            seconds=CADENCE_BY_JOB["research_books"].interval_seconds
        ),
        id="research_books",
        name="weather orderbook archiving",
        # Short grace: this job is about capturing a specific minute. A run that
        # fires five minutes late has missed the thing it exists to observe, and
        # queueing it would only add load.
        misfire_grace_time=30,
    )

    scheduler.add_job(
        _sync(jobs.job_research_trades),
        IntervalTrigger(seconds=CADENCE_BY_JOB["research_trades"].interval_seconds),
        id="research_trades",
        name="weather trade prints",
        misfire_grace_time=300,
    )

    scheduler.add_job(
        _sync(jobs.job_daily_climate),
        IntervalTrigger(seconds=CADENCE_BY_JOB["daily_climate"].interval_seconds),
        id="daily_climate",
        name="CLI daily climate (settlement label)",
        # Generous. The bulletin is published on the office's schedule, not
        # ours, and a late run still collects the same completed-day value - so
        # a misfire should be caught up rather than dropped.
        misfire_grace_time=1800,
    )

    scheduler.add_job(
        _sync(jobs.job_forecast_validity),
        IntervalTrigger(seconds=CADENCE_BY_JOB["forecast_validity"].interval_seconds),
        id="forecast_validity",
        name="forecast validity audit",
        misfire_grace_time=600,
    )

    scheduler.add_job(
        _guard(jobs.job_collection_health),
        _utc_cron(hour=23, minute=50),
        id="collection_health",
        name="collection-health snapshot",
    )

    scheduler.add_job(
        _guard(jobs.job_map_settlement_stations),
        IntervalTrigger(seconds=CADENCE_BY_JOB["map_stations"].interval_seconds),
        id="map_stations",
        name="settlement-station mapping",
        # Idempotent, so a coalesced catch-up run is harmless.
        misfire_grace_time=600,
    )

    return scheduler


def run_scheduler() -> None:
    setup_logging()
    scheduler = build_scheduler()

    def shutdown(_signum: int, _frame: Any) -> None:
        log.info("shutting down scheduler")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("scheduler starting with jobs: %s", [j.id for j in scheduler.get_jobs()])
    scheduler.start()
