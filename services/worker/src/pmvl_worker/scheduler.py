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
        except Exception as exc:  # noqa: BLE001 - a failed job must not kill the scheduler
            log.error("scheduled job failed: %s", exc)

    return runner


def _guard(fn: Callable[[], Any]) -> Callable[[], None]:
    def runner() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.error("scheduled job failed: %s", exc)

    return runner


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
        CronTrigger(
            hour=settings.daily_snapshot_hour_utc,
            minute=settings.daily_snapshot_minute_utc,
        ),
        id="snapshot",
        name="daily recommendation snapshot",
    )

    # Backtest after the snapshot, so the day's record is included.
    scheduler.add_job(
        _guard(jobs.job_backtest),
        CronTrigger(
            hour=(settings.daily_snapshot_hour_utc + 1) % 24,
            minute=settings.daily_snapshot_minute_utc,
        ),
        id="backtest",
        name="walk-forward backtest",
    )

    scheduler.add_job(
        _guard(lambda: jobs.job_prune(keep_days=30)),
        CronTrigger(hour=4, minute=30),
        id="prune",
        name="orderbook retention",
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
