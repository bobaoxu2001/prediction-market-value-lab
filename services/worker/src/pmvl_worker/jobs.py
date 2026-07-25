"""Job wrappers.

Every job records a :class:`JobRun` row - start, finish, duration, records written,
and the error if it failed. ``/system`` reads these, so a job that silently stopped
running is visible rather than being mistaken for "no opportunities today".
"""

from __future__ import annotations

import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator

from pmvl_shared.db import session_scope
from pmvl_shared.enums import JobStatus
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from pmvl_markets.db_models import JobRun

log = get_logger(__name__)


@contextmanager
def job_run(job_name: str) -> Iterator[dict[str, Any]]:
    """Record a job's lifecycle. Yields a mutable dict for the job's own details."""
    started = utcnow()
    monotonic = time.monotonic()
    details: dict[str, Any] = {}
    record_id: int | None = None

    with session_scope() as db:
        row = JobRun(
            job_name=job_name,
            status=JobStatus.RUNNING.value,
            started_at=started,
        )
        db.add(row)
        db.flush()
        record_id = row.id

    status = JobStatus.SUCCESS
    error = ""
    try:
        yield details
    except Exception as exc:  # noqa: BLE001 - the failure must be recorded, then re-raised
        status = JobStatus.FAILED
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
        log.error("job %s failed: %s", job_name, exc)
        raise
    finally:
        duration = time.monotonic() - monotonic
        with session_scope() as db:
            row = db.get(JobRun, record_id)
            if row is not None:
                row.status = status.value
                row.finished_at = utcnow()
                row.duration_seconds = round(duration, 3)
                row.records_written = int(details.get("records_written", 0) or 0)
                row.error = error
                row.details = _jsonable(details)


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so job details always persist."""
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"repr": str(value)[:4000]}


# ------------------------------------------------------------------- job bodies
async def job_ingest(*, market_limit: int | None = None, orderbook_limit: int | None = None) -> dict[str, Any]:
    from pmvl_markets.ingest import run_ingest

    with job_run("ingest") as details:
        with session_scope() as db:
            report = await run_ingest(
                db, market_limit=market_limit, orderbook_limit=orderbook_limit
            )
        details.update(report.as_dict())
        details["records_written"] = report.markets_written
        return report.as_dict()


async def job_orderbooks(*, limit: int | None = None) -> dict[str, Any]:
    from pmvl_markets.ingest import refresh_orderbooks

    with job_run("orderbooks") as details:
        with session_scope() as db:
            report = await refresh_orderbooks(db, limit=limit)
        details.update(report.as_dict())
        details["records_written"] = report.orderbooks_written
        return report.as_dict()


async def job_score(*, limit: int | None = None) -> dict[str, Any]:
    from pmvl_markets.value import score_markets

    with job_run("score") as details:
        with session_scope() as db:
            report, candidates = await score_markets(db, limit=limit)
        payload = report.as_dict()
        details.update(payload)
        details["records_written"] = report.predictions_written
        return payload


async def job_rank(*, limit: int | None = None) -> dict[str, Any]:
    from pmvl_markets.value import run_ranking

    with job_run("rank") as details:
        with session_scope() as db:
            report = await run_ranking(db, limit=limit)
        payload = report.as_dict()
        details.update(payload)
        details["records_written"] = report.recommendations_written
        return payload


def job_arbitrage() -> dict[str, Any]:
    from pmvl_markets.arbitrage import run_arbitrage_scan

    with job_run("arbitrage") as details:
        with session_scope() as db:
            report, _results = run_arbitrage_scan(db)
        payload = report.as_dict()
        details.update(payload)
        details["records_written"] = sum(report.opportunities.values())
        return payload


async def job_settle(*, lookback_days: int = 45) -> dict[str, Any]:
    from pmvl_markets.backtest import sync_settlements

    with job_run("settle") as details:
        with session_scope() as db:
            report = await sync_settlements(db, lookback_days=lookback_days)
        payload = report.as_dict()
        details.update(payload)
        details["records_written"] = report.settlements_written
        return payload


def job_snapshot(*, force: bool = False) -> dict[str, Any]:
    from pmvl_markets.backtest import write_daily_snapshot

    with job_run("snapshot") as details:
        with session_scope() as db:
            report = write_daily_snapshot(db, force=force)
        payload = report.as_dict()
        details.update(payload)
        details["records_written"] = report.written
        return payload


def job_backtest() -> dict[str, Any]:
    from pmvl_markets.backtest import run_backtest

    with job_run("backtest") as details:
        with session_scope() as db:
            results = run_backtest(db)
        payload = {
            "runs": len(results),
            "strategies": [r.as_dict() for r in results],
        }
        details["records_written"] = len(results)
        details["runs"] = len(results)
        details["settled_total"] = sum(r.n_settled for r in results)
        return payload


def job_prune(*, keep_days: int = 30) -> dict[str, Any]:
    from pmvl_markets.ingest import prune_orderbook_snapshots

    with job_run("prune") as details:
        with session_scope() as db:
            removed = prune_orderbook_snapshots(db, keep_days=keep_days)
        details["records_written"] = removed
        return {"orderbook_snapshots_removed": removed}
