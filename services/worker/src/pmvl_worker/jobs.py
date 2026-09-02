"""Job wrappers.

Every job records a :class:`JobRun` row - start, finish, duration, records written,
and the error if it failed. ``/system`` reads these, so a job that silently stopped
running is visible rather than being mistaken for "no opportunities today".
"""

from __future__ import annotations

import threading
import time
import uuid
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import OperationalError

from pmvl_shared.config import get_settings
from pmvl_shared.db import session_scope
from pmvl_shared.enums import JobStatus
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from pmvl_shared.job_record import RunRecord, idempotency_key

from pmvl_markets.alpha.collection import COLLECTOR_VERSION
from pmvl_markets.db_models import JobRun

log = get_logger(__name__)

#: A 'running' row older than this, *and with no heartbeat*, is a process that
#: died mid-run rather than an active competitor. A new run with the same key
#: supersedes it instead of being skipped forever.
ACTIVE_RUN_STALE_AFTER = timedelta(hours=2)

#: The same judgement for a run that reports a heartbeat. Ten missed 30-second
#: ticks is dead, whatever its total age.
#:
#: Two hours was the only bound before, and it made a documented guarantee
#: false. ``research_books`` holds its slot for an entire issuance window, so a
#: worker killed mid-window left a 'running' row that outlived the window it was
#: blocking: every subsequent tick recorded itself as skipped, and collection
#: did not resume until two hours after the crash - one window lost entirely and
#: most of the next. Observed on 2026-08-25 19:45 UTC, where a deliberate
#: restart produced exactly that and needed a manual database write to clear.
HEARTBEAT_STALE_AFTER = timedelta(minutes=5)

#: How often a running job proves it is alive. Comfortably inside
#: HEARTBEAT_STALE_AFTER so an ordinary scheduling hiccup cannot make a healthy
#: job look dead, and cheap enough to run for every job rather than a chosen few.
HEARTBEAT_INTERVAL = timedelta(seconds=45)


class JobSkipped(RuntimeError):
    """An identical run is already active; this invocation did no work.

    Raised from inside ``job_run`` BEFORE the wrapped body executes, so the job's
    own code never runs. The skipped invocation is still recorded as a JobRun row
    pointing at the active run, keeping the audit trail complete.
    """

    def __init__(self, job_name: str, active_run_id: int) -> None:
        super().__init__(
            f"{job_name} skipped: an identical run (id {active_run_id}) is already active"
        )
        self.job_name = job_name
        self.active_run_id = active_run_id


def _claim_active_key(db, job_name: str, key: str, started: datetime) -> int:
    """Insert the running row, arbitrating identical concurrent runs.

    Returns the new row's id. The partial unique index on (status='running',
    idempotency_key) is the arbiter, enforced with an upsert primitive so a
    losing writer never poisons its own transaction: a second writer inserting
    the same key either records itself as skipped (a fresh run is genuinely
    active) or supersedes dead 'running' rows and claims the key.
    """
    from sqlalchemy import text

    from pmvl_shared.db.base import insert_or_skip

    values = {
        "job_name": job_name,
        "status": JobStatus.RUNNING.value,
        "started_at": started,
        "idempotency_key": key,
        "records_written": 0,
        "error": "",
    }
    conflict_target = {
        "conflict_cols": ["idempotency_key"],
        "conflict_where": text("status = 'running' AND idempotency_key IS NOT NULL"),
    }

    def claim() -> int:
        if not insert_or_skip(db, JobRun.__table__, dict(values), **conflict_target):
            return -1
        row = db.scalar(
            select(JobRun)
            .where(
                JobRun.idempotency_key == key,
                JobRun.status == JobStatus.RUNNING.value,
                JobRun.started_at == started,
            )
            .order_by(JobRun.id.desc())
            .limit(1)
        )
        if row is None:  # pragma: no cover - the insert just targeted this key
            raise RuntimeError("claimed the idempotency key but cannot reload the row")
        return row.id

    record_id = claim()
    if record_id != -1:
        return record_id

    # Liveness, not age. A row that reports a heartbeat is judged on the
    # heartbeat; one that does not is judged on start time as before.
    active = db.scalar(
        select(JobRun)
        .where(
            JobRun.idempotency_key == key,
            JobRun.status == JobStatus.RUNNING.value,
            or_(
                and_(
                    JobRun.heartbeat_at.is_(None),
                    JobRun.started_at > started - ACTIVE_RUN_STALE_AFTER,
                ),
                JobRun.heartbeat_at > started - HEARTBEAT_STALE_AFTER,
            ),
        )
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )
    if active is not None:
        raise JobSkipped(job_name, active.id)

    # Everything still marked running for this key is a dead process. Mark it
    # interrupted so the audit trail shows what happened, then claim the key.
    stale = list(
        db.scalars(
            select(JobRun).where(
                JobRun.idempotency_key == key,
                JobRun.status == JobStatus.RUNNING.value,
            )
        )
    )
    for old in stale:
        old.status = JobStatus.INTERRUPTED.value
        old.finished_at = utcnow()
        old.error = "interrupted: superseded by a new run with the same idempotency key"
    db.flush()

    record_id = claim()
    if record_id == -1:  # pragma: no cover - the stale rows freed the key
        raise RuntimeError("could not claim the idempotency key after superseding stale runs")
    return record_id


#: How many times the opening claim may be retried when SQLite is busy.
#:
#: The claim is one small INSERT, but it is the FIRST statement a scheduled job
#: runs, so it is the one that loses under write contention - and losing it is
#: silent. On 2026-08-31T14:00:00Z the backtest's claim INSERT failed with
#: `database is locked`; the exception left `job_run` before the body executed,
#: `_guard` logged it and returned, and APScheduler then logged the job as
#: "executed successfully". The result was a cron job that did not run, left no
#: job_runs row at all, and looked healthy in the scheduler log.
#:
#: Retrying here does not paper over contention - it stops a transient lock from
#: deleting a scheduled execution AND its evidence in the same moment.
_CLAIM_RETRIES = 5
_CLAIM_BACKOFF_SECONDS = 0.5


def _claim_with_retry(
    db, job_name: str, key: str, started: datetime  # noqa: ANN001
) -> int:
    """Claim the active-run slot, retrying only on SQLite busy/locked errors.

    ``JobSkipped`` is re-raised immediately: another live run holds the slot and
    waiting cannot change that. Every other exception propagates unchanged.
    """
    from sqlalchemy.exc import OperationalError

    last: Exception | None = None
    for attempt in range(_CLAIM_RETRIES):
        try:
            return _claim_active_key(db, job_name, key, started)
        except JobSkipped:
            raise
        except OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            db.rollback()
            if attempt < _CLAIM_RETRIES - 1:
                time.sleep(_CLAIM_BACKOFF_SECONDS * (2**attempt))
                log.warning(
                    "claim for %s busy, retry %d/%d",
                    job_name, attempt + 1, _CLAIM_RETRIES - 1,
                )
    assert last is not None
    log.error("could not claim a run slot for %s: %s", job_name, last)
    raise last


class CollectionUpstreamFailure(RuntimeError):
    """A collection cycle could not reach its venue.

    Distinct from a crash: the code worked, the venue did not. It is raised so
    the run is recorded as FAILED rather than closing as a success that wrote
    zero rows, and it is caught by the scheduler guard so one unreachable
    provider degrades the record without stopping the schedule.
    """


def _record_claim_failure(
    job_name: str, record: "RunRecord", started: datetime, exc: Exception
) -> None:
    """Leave durable evidence that a scheduler fire was prevented by contention.

    Deliberately its own short session: the caller's is poisoned by the failed
    INSERT. Deliberately unretried: this runs while a writer is holding the very
    lock that caused the failure, and queuing another retry storm behind it
    would amplify the contention it is reporting. If this write cannot land
    either, the stderr log is the fallback channel and the original
    OperationalError still propagates, so the failure is never silent.
    """
    try:
        with session_scope() as db:
            db.add(
                JobRun(
                    job_name=job_name,
                    status=JobStatus.FAILED.value,
                    started_at=started,
                    finished_at=utcnow(),
                    duration_seconds=0.0,
                    records_written=0,
                    idempotency_key=record.idempotency_key,
                    error=f"claim blocked by database lock: {str(exc)[:240]}",
                    details={
                        "run": {
                            "run_id": record.run_id,
                            "idempotency_key": record.idempotency_key,
                            "claim_failed": True,
                            "body_executed": False,
                        }
                    },
                )
            )
    except Exception as inner:  # noqa: BLE001
        # Never recurse into the ledger to report a ledger failure.
        log.error(
            "job %s could not claim a run slot AND could not record that "
            "failure: claim=%s record=%s",
            job_name, str(exc)[:160], str(inner)[:160],
        )


@contextmanager
def job_run(
    job_name: str,
    *,
    cutoff: datetime | None = None,
    params: dict[str, Any] | None = None,
    upstream: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Record a job's lifecycle. Yields a mutable dict for the job's own details.

    The yielded dict carries a ``record`` key holding a :class:`RunRecord`. A job
    that reports per-provider outcomes on it gets PARTIAL_SUCCESS automatically
    when some sources failed and others did not - which is the case that used to
    be indistinguishable from "there was nothing to find".
    """
    started = utcnow()
    monotonic = time.monotonic()
    run_id = uuid.uuid4().hex[:16]
    record = RunRecord(
        job_name=job_name,
        run_id=run_id,
        idempotency_key=idempotency_key(job_name, cutoff=cutoff, params=params),
        scheduled_at=started,
        input_data_cutoff=cutoff,
        upstream_dependencies=list(upstream or []),
    )
    details: dict[str, Any] = {"record": record}
    record_id: int | None = None

    with session_scope() as db:
        try:
            record_id = _claim_with_retry(
                db, job_name, record.idempotency_key, started
            )
        except JobSkipped as skip:
            # Record this invocation as skipped so the audit trail shows both
            # sides, then re-raise: the job body must not run.
            db.add(
                JobRun(
                    job_name=job_name,
                    status=JobStatus.SKIPPED.value,
                    started_at=started,
                    finished_at=utcnow(),
                    duration_seconds=0.0,
                    records_written=0,
                    idempotency_key=record.idempotency_key,
                    details={
                        "run": {
                            "run_id": record.run_id,
                            "idempotency_key": record.idempotency_key,
                            "skipped": True,
                            "skipped_because_active_run_id": skip.active_run_id,
                        }
                    },
                )
            )
            # session_scope rolls back on exception; the skipped record must
            # survive so the audit trail shows both sides of the skip.
            db.commit()
            raise
        except OperationalError as exc:
            # Contention must not erase the attempt.
            #
            # This is the defect that made a real 2026-09-01T15:00Z calibrate
            # fire vanish. The claim INSERT is the only durable artifact an
            # attempt has, so when it exhausts its retries against a held write
            # lock the rollback takes the sole evidence with it: the scheduler
            # logged a failure, the ledger showed nothing, and `job_status`
            # went on reporting the previous day's run. Thirteen minutes of
            # every job's history disappeared the same way.
            #
            # The JobSkipped branch above already set the precedent - it commits
            # a durable record precisely so "the audit trail shows both sides".
            # Lock contention had no equivalent, which is the asymmetry being
            # repaired. FAILED is the existing status for a run that did not do
            # its job; no new state is invented for it. The row cannot collide
            # with the active-key unique index because that index covers only
            # status='running'.
            _record_claim_failure(job_name, record, started, exc)
            raise

    def heartbeat() -> None:
        """Prove this run is still alive.

        Its own short session, committed immediately: the point is to be visible
        to *another* process deciding whether this run is dead, and an update
        held inside the job's long-lived transaction would not be. Failures are
        swallowed - a heartbeat that cannot be written must never take down the
        collection it exists to protect. The cost of a missed write is that a
        healthy run may be superseded, which is recoverable; the cost of raising
        here is losing the capture.
        """
        try:
            with session_scope() as hb:
                row = hb.get(JobRun, record_id)
                if row is not None and row.status == JobStatus.RUNNING.value:
                    row.heartbeat_at = utcnow()
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat failed for %s: %s", job_name, exc)

    details["heartbeat"] = heartbeat

    # Every job reports liveness, automatically, for as long as it runs.
    #
    # Wiring the heartbeat into one job's loop fixed one job. Observed directly:
    # after a restart, `research_books` resumed immediately while `ingest`,
    # `orderbooks` and `research_trades` sat blocked behind their own orphaned
    # rows, because those never heartbeat and so fell back to the two-hour age
    # bound. Fixing the deadlock for the job being measured and leaving it in
    # place for the rest is not a fix, it is a narrower bug.
    #
    # A daemon thread avoids asking each job to remember. It stamps on entry and
    # then periodically, so liveness does not depend on a job's internal shape -
    # a tight loop, one long request, or no loop at all all behave the same.
    heartbeat()
    stop_beating = threading.Event()

    def _beat() -> None:
        while not stop_beating.wait(HEARTBEAT_INTERVAL.total_seconds()):
            heartbeat()

    beater = threading.Thread(
        target=_beat, name=f"heartbeat-{job_name}", daemon=True
    )
    beater.start()

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
        # Stop before the terminal write, so a beat cannot resurrect
        # heartbeat_at on a row that has already been closed out.
        stop_beating.set()
        beater.join(timeout=5.0)
        duration = time.monotonic() - monotonic
        with session_scope() as db:
            row = db.get(JobRun, record_id)
            if row is not None:
                # A run that lost one provider and kept the rest is neither a
                # success nor a failure, and calling it either loses the fact an
                # operator needs. Derived here so a job cannot forget to say so.
                if status is JobStatus.SUCCESS and record.is_partial:
                    status = JobStatus.PARTIAL_SUCCESS
                    log.warning(
                        "job %s partially succeeded; failed providers: %s",
                        job_name, ", ".join(record.failed_providers),
                    )
                row.status = status.value
                row.finished_at = utcnow()
                row.duration_seconds = round(duration, 3)
                record.records_written = int(
                    details.get("records_written", record.records_written) or 0
                )
                if error:
                    record.errors.append(error[:500])
                row.records_written = record.records_written
                row.error = error
                # `record` and `heartbeat` are control handles, not reported
                # data: the RunRecord is replaced by its dict form below, and the
                # heartbeat is a callable. Neither is JSON-serialisable, and
                # leaving either in would trip _jsonable's fallback.
                payload = {
                    k: v for k, v in details.items() if k not in ("record", "heartbeat")
                }
                payload["run"] = record.as_dict()
                row.details = _jsonable(payload)


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so job details always persist.

    Degrades per key rather than wholesale. The previous version replaced the
    entire payload with a repr as soon as any single value failed, so one
    unserialisable entry silently discarded every other detail the job had
    reported - including the run provenance an operator needs to diagnose the
    job. Found when a callable was added to the details dict and every job's
    ``run`` key disappeared.
    """
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return {"repr": str(value)[:4000]}


# ------------------------------------------------------------------- job bodies
async def job_ingest(*, market_limit: int | None = None, orderbook_limit: int | None = None) -> dict[str, Any]:
    from pmvl_markets.ingest import run_ingest

    with job_run(
        "ingest", params={"market_limit": market_limit, "orderbook_limit": orderbook_limit}
    ) as details:
        record = details["record"]
        with session_scope() as db:
            report = await run_ingest(
                db, market_limit=market_limit, orderbook_limit=orderbook_limit
            )
        # Per-venue outcomes, so one platform failing degrades the run to
        # PARTIAL_SUCCESS instead of producing a shorter market list that reads
        # downstream as "there was nothing to find today".
        for platform, written in (report.by_platform or {}).items():
            record.provider(platform).records = written
        for message in report.errors:
            platform = next(
                (p for p in (report.by_platform or {}) if p.lower() in message.lower()),
                None,
            )
            if platform:
                record.fail_provider(platform, message)
            else:
                record.warn(message[:300])
        # A platform that returned nothing at all is a failure even when it
        # raised no error: an empty venue is not a normal ingest result.
        for platform, written in (report.by_platform or {}).items():
            if not written and record.provider(platform).healthy:
                record.fail_provider(platform, "returned no markets")

        record.records_read = report.markets_fetched
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


async def job_retrodict(
    *,
    settled_within_days: int = 60,
    max_markets: int = 200,
) -> dict[str, Any]:
    """Replay the models against already-settled markets.

    Not part of ``pmvl pipeline`` and not on the scheduler. It reaches out to the
    venues' candlestick endpoints for every market it samples, which is a heavy,
    bursty read that has nothing to do with keeping the live surface fresh, and its
    output is a measurement someone reads rather than a table the site serves.
    """
    from pmvl_markets.retrodiction import SamplingCriteria, run_retrodiction

    with job_run("retrodict") as details:
        criteria = SamplingCriteria(
            settled_within_days=settled_within_days, max_markets=max_markets
        )
        with session_scope() as db:
            payload = await run_retrodiction(db, criteria=criteria)
        result = payload.get("result", {})
        details["records_written"] = result.get("n_forecasts", 0)
        details["brier_improvement_vs_market"] = result.get(
            "brier_improvement_vs_market"
        )
        details["n_scored"] = result.get("n_scored_against_market", 0)
        return payload


def job_calibrate() -> dict[str, Any]:
    """Fit a calibration map on live settled recommendations, or record why not.

    Safe to run on a schedule from day one: with too few settled recommendations
    it stores a refusal and changes nothing, which is the honest state of the
    system until the run clock has been turning for several weeks.
    """
    from pmvl_markets.backtest import fit_and_store
    from pmvl_markets.probability.ensemble import MODEL_VERSION

    with job_run("calibrate") as details:
        with session_scope() as db:
            fit = fit_and_store(db, model_name="ensemble", version=MODEL_VERSION)
        payload = fit.as_dict()
        details.update(
            {
                "applied": fit.applied,
                "method": fit.method,
                "n_train": fit.n_train,
                "records_written": 1,
            }
        )
        return payload


def job_prune(
    *, keep_days: int | None = None, keep_hours: int | None = None
) -> dict[str, Any]:
    from pmvl_markets.ingest import prune_orderbook_snapshots

    with job_run("prune") as details:
        with session_scope() as db:
            removed = prune_orderbook_snapshots(
                db, keep_days=keep_days, keep_hours=keep_hours
            )
        details["records_written"] = removed
        return {"orderbook_snapshots_removed": removed}


# --------------------------------------------------------------- research collection
#
# These jobs exist to stop the loss of irrecoverable point-in-time history. They
# write to the research archive and the PIT tables; none of them computes a
# markout, a forward return or a signal. Collection only.


async def job_station_observations(*, limit_per_station: int = 36) -> dict[str, Any]:
    """Collect observations at every settlement station in the research universe.

    Every station is polled independently and a failure on one never stops the
    others: a station-level outage should cost that station's coverage, not the
    whole day's.

    Every attempt is persisted whether it succeeded or not. A gap in the
    observation series has two causes - the station did not report, or we could
    not reach it - and only ``poll_attempts`` distinguishes them.
    """
    from pmvl_shared.payload_store import PayloadStore
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.alpha.collection import (
        PollWindow,
        record_poll_attempt,
        record_station_observation,
    )
    from pmvl_markets.normalize.weather_stations import research_station_ids
    from pmvl_markets.providers.nws_observations import (
        PARSER_VERSION,
        NwsObservationsProvider,
    )

    store = PayloadStore(_research_data_dir() / "payloads")
    provider = NwsObservationsProvider()
    written, attempted, failed = 0, 0, 0
    per_station: dict[str, int] = {}
    errors: list[str] = []

    with job_run("station_observations") as details:
        try:
            for station_id in research_station_ids():
                poll = PollWindow(
                    started_at=utcnow(), endpoint=f"/stations/{station_id}/observations"
                )
                attempted += 1
                try:
                    fetch = await provider.fetch_recent(
                        station_id, limit=limit_per_station
                    )
                except Exception as exc:  # noqa: BLE001
                    # One station's failure must not end the pass.
                    poll.finish()
                    with session_scope() as db:
                        record_poll_attempt(
                            db, source_id="nws", source_product="observations",
                            target=station_id, poll=poll, success=False,
                            error_type="unhandled", error_detail=str(exc),
                        )
                    failed += 1
                    errors.append(f"{station_id}: {exc}")
                    continue

                poll.finish(
                    http_status=fetch.http_status,
                    served_from_cache=fetch.served_from_cache,
                )
                with session_scope() as db:
                    record_poll_attempt(
                        db, source_id="nws", source_product="observations",
                        target=station_id, poll=poll, success=fetch.success,
                        error_type=fetch.error_type, error_detail=fetch.error_detail,
                    )
                    if not fetch.success:
                        failed += 1
                        continue
                    for record in fetch.records:
                        row = record_station_observation(
                            db,
                            station_id=record.station_id,
                            source_product="observations",
                            observation_time=record.observation_time,
                            poll=poll,
                            temperature_f=record.temperature_f,
                            measurements=record.measurements,
                            published_at=record.published_at,
                            revision_id=record.revision_id,
                            raw_payload=record.raw_payload,
                            payload_store=store,
                            parser_version=PARSER_VERSION,
                        )
                        if row is not None:
                            written += 1
                            per_station[station_id] = per_station.get(station_id, 0) + 1
        finally:
            await provider.aclose()

        # Must stay zero. A cached body stamped with a new available_at is a
        # fabricated availability, not a stale number.
        cache_hits = provider.cache_hits
        details.update(
            {
                "stations_polled": attempted,
                "observations_written": written,
                "failed_polls": failed,
                "per_station": per_station,
                "cache_hits": cache_hits,
                "records_written": written,
            }
        )
        if errors:
            details["errors"] = errors[:10]
        return dict(details)


async def job_research_books(*, max_seconds: int = 0) -> dict[str, Any]:
    """Capture weather order books, faster inside an NWS issuance window.

    Outside a window this is one capture at the baseline cadence. Inside one it
    loops at 30-second cadence until the window closes or ``max_seconds`` is
    reached, because the interesting part of a source-lag response is the first
    few minutes and a 180-second cadence cannot see it.

    ``max_seconds`` bounds the loop so the job cannot outlive its scheduler slot.
    Zero means "until the window ends", which is what a resident worker wants;
    a batch invocation passes its own budget.
    """
    import asyncio

    from pmvl_shared.collection_outcome import (
        CollectionOutcome,
        classify_collection,
    )
    from pmvl_shared.research_archive import ResearchArchive, default_archive_path
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.alpha.issuance import (
        RESEARCH_CADENCE_SECONDS,
        WINDOW_HORIZON_HOURS,
        cadence_for,
    )
    from pmvl_markets.ingest import refresh_orderbooks

    archive = ResearchArchive(default_archive_path(_research_data_dir().parent))

    with job_run("research_books") as details:
        started = utcnow()
        published = _observed_publication_times()
        cadence, window = cadence_for(started, observed_published_at=published)
        anchor = window.anchor.isoformat() if window else ""

        captures, archived, errors = 0, 0, []
        tick, skipped = 0, 0
        markets_seen, upstream, configured = 0, [], 0

        # Claim liveness BEFORE the first capture, not only after it.
        #
        # Heartbeating only at the end of each capture left a hole exactly the
        # width of one capture: a worker killed during its first one had
        # heartbeat_at NULL, so the reclaimer fell back to the two-hour age
        # bound and the dead run held the slot for the rest of the window. That
        # is the 2026-08-25 failure verbatim - it was killed 15 minutes in,
        # before any heartbeat existed, and every later tick recorded itself as
        # skipped until the row was cleared by hand.
        #
        # Stamping on entry means a run is reclaimable five minutes after it
        # stops proving itself, whether it died in its first capture or its
        # hundredth.
        details["heartbeat"]()

        deadline = None
        if window is not None:
            deadline = window.end
            if max_seconds > 0:
                budget = started + timedelta(seconds=max_seconds)
                deadline = min(deadline, budget)

        while True:
            try:
                with session_scope() as db:
                    report = await refresh_orderbooks(
                        db,
                        archive=archive,
                        window_anchor=anchor,
                        # Always the research universe, in or out of a window.
                        #
                        # This was `window is not None`, so outside a window the
                        # job fetched the FULL board and wrote the product
                        # orderbook tables - duplicating job_orderbooks, which
                        # already owns that store. archive_books only keeps
                        # research markets, so it was fetching ~2,000 markets to
                        # archive 252, and writing every one of them to a
                        # product table the snapshot builder discards all but
                        # the latest row of.
                        #
                        # Measured at 05:36-05:59 on 2026-08-26, out of window
                        # and after the in-window fix: +233,684 orderbook_levels
                        # rows in 23 minutes, about 395 MiB of database growth a
                        # day. That is the same write amplification that stalled
                        # collection for two hours, just on the other side of
                        # the window boundary.
                        #
                        # Inside a window the horizon below narrows this further
                        # to the near-dated markets, for cadence rather than for
                        # write volume.
                        research_only=True,
                        max_hours_to_resolution=(
                            WINDOW_HORIZON_HOURS if window is not None else None
                        ),
                    )
                captures += 1
                archived += int(report.archive.get("books_archived", 0))
                markets_seen += int(report.archive.get("markets_seen", 0))
                configured += int(getattr(report, "configured_markets", 0) or 0)
                # Provider-level failures are reported, not raised: a platform
                # whose circuit is open is recorded in report.errors and the
                # cycle returns normally. Collecting them here is what lets a
                # zero-row cycle say whether the venue was reachable.
                upstream.extend(report.errors)
                upstream.extend(report.archive.get("errors", []))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
                upstream.append(f"{type(exc).__name__}: {exc}")

            # Prove liveness before sleeping. A worker killed mid-window used to
            # leave its 'running' row blocking every later tick; the reclaimer
            # reads this to tell a dead run from a legitimately long one.
            details["heartbeat"]()

            if deadline is None or utcnow() >= deadline:
                break

            # Sleep to the next cadence BOUNDARY, not for a fixed interval.
            #
            # A fixed `sleep(30)` after the capture makes the period
            # `capture_time + 30`. A research-universe capture measured at ~30s,
            # so the effective cadence was ~60s - half the captures, silently,
            # with the configured value still reading 30.
            #
            # Aligning to boundaries also means a slow capture skips a tick
            # rather than shifting every subsequent one, so the window's captures
            # stay on a grid a later reader can reason about.
            tick += 1
            next_tick = started + timedelta(seconds=RESEARCH_CADENCE_SECONDS * tick)
            now = utcnow()
            while next_tick <= now:
                # The capture overran its slot. Drop the missed ticks rather than
                # firing them back-to-back, and count them so the shortfall has a
                # cause instead of being an unexplained gap.
                skipped += 1
                tick += 1
                next_tick = started + timedelta(
                    seconds=RESEARCH_CADENCE_SECONDS * tick
                )
            if next_tick >= deadline:
                break
            await asyncio.sleep((next_tick - now).total_seconds())

        stats = archive.stats()
        details.update(
            {
                "captures": captures,
                "ticks_skipped": skipped,
                "books_archived": archived,
                "cadence_seconds": cadence,
                "in_issuance_window": window is not None,
                "window_anchor": anchor,
                "window_derivation": window.derivation.value if window else "",
                "window_horizon_hours": WINDOW_HORIZON_HOURS if window else None,
                "archive_books_total": stats.books,
                "records_written": archived,
                # Why the row count is what it is.
                #
                # `records_written = 0` alone cannot distinguish a quiet market
                # from an unreachable venue, and on 2026-08-31 the difference
                # was six decision instants. Health reads this rather than
                # inferring intent from a count.
                "outcome": classify_collection(
                    books_archived=archived,
                    markets_seen=markets_seen,
                    configured_markets=configured,
                    errors=upstream,
                ).value,
                "markets_seen": markets_seen,
                "configured_markets": configured,
            }
        )
        if errors:
            details["errors"] = errors[:10]
        if upstream:
            details["upstream_errors"] = upstream[:10]

        # A run that could not reach the venue must not close as a success.
        #
        # On 2026-09-01 five consecutive cycles wrote status='success' through a
        # 64-minute Kalshi outage. The capture-age alarm caught it, but every
        # per-run record said the collection was fine, and the outcome field
        # said there had been nothing to collect. Raising here routes the run
        # through job_run's existing failure path - status FAILED with the
        # reason attached - which is the contract this repository already uses
        # for a run that did not do its job. `_guard` still absorbs it at the
        # scheduler, so one unreachable venue degrades the record without
        # taking down the schedule.
        outcome = CollectionOutcome(details["outcome"])
        if outcome.is_upstream_failure:
            record = details["record"]
            record.fail_provider("kalshi", details["outcome"])
            raise CollectionUpstreamFailure(
                f"{details['outcome']}: {configured} markets configured, "
                f"{markets_seen} fetched, {archived} archived"
            )
        return dict(details)


def job_map_settlement_stations() -> dict[str, Any]:
    """Populate ``markets.settlement_station_id`` from the venue's own rules.

    Deterministic and re-runnable: the same rules text always produces the same
    station. Markets the parser does not recognise are left empty and counted,
    never guessed - a wrong station trades confidently while an absent one
    declines, and only one of those is recoverable.
    """
    from sqlalchemy import select

    from pmvl_markets.db_models import Market
    from pmvl_markets.normalize.weather_stations import (
        RESEARCH_SERIES_PREFIX,
        parse_settlement_provenance,
        parse_settlement_station,
    )

    with job_run("map_stations") as details:
        mapped, unmapped, inexact, unchanged = 0, [], 0, 0
        by_station: dict[str, int] = {}
        by_vendor: dict[str, int] = {}
        by_product: dict[str, int] = {}
        with session_scope() as db:
            rows = db.scalars(
                select(Market).where(
                    Market.series_ticker.like(f"{RESEARCH_SERIES_PREFIX}%")
                )
            ).all()
            for market in rows:
                result = parse_settlement_station(
                    market.settlement_rules_raw, series_ticker=market.series_ticker
                )
                if not result.mapped:
                    unmapped.append(
                        {
                            "market": market.platform_market_id,
                            "series": market.series_ticker,
                            "reason": result.reason.value if result.reason else "",
                        }
                    )
                    continue
                mapped += 1
                if not result.exact:
                    inexact += 1
                by_station[result.station_id] = by_station.get(result.station_id, 0) + 1

                # Station, product code and named vendor change independently.
                # Kalshi kept the first two and changed the third in August 2026;
                # storing them apart is what makes that visible rather than
                # buried in a rules diff nobody reads.
                provenance = parse_settlement_provenance(
                    market.settlement_rules_raw, series_ticker=market.series_ticker
                )
                by_vendor[provenance.source_vendor or "unnamed"] = (
                    by_vendor.get(provenance.source_vendor or "unnamed", 0) + 1
                )
                if provenance.product_code:
                    by_product[provenance.product_code] = (
                        by_product.get(provenance.product_code, 0) + 1
                    )
                already = (
                    market.settlement_station_id == result.station_id
                    and market.settlement_product_code == provenance.product_code
                    and market.settlement_source_vendor == provenance.source_vendor
                )
                if already:
                    unchanged += 1
                    continue
                market.settlement_station_id = result.station_id
                market.settlement_product_code = provenance.product_code
                market.settlement_source_vendor = provenance.source_vendor

        details.update(
            {
                "markets_considered": len(rows),
                "mapped": mapped,
                "unmapped": len(unmapped),
                "inexact": inexact,
                "already_correct": unchanged,
                "by_station": dict(sorted(by_station.items())),
                "by_settlement_vendor": dict(sorted(by_vendor.items())),
                "by_settlement_product": dict(sorted(by_product.items())),
                # The explicit list, never a silent gap.
                "unmapped_markets": unmapped[:25],
                "records_written": mapped - unchanged,
            }
        )
        return dict(details)


def _research_data_dir() -> Path:
    """Where the research archive and payload store live.

    Derived from the configured SQLite path rather than a second setting, so the
    archive always sits beside the operational database and cannot drift to a
    different disk from the data it describes.
    """
    from pmvl_shared.config import DEFAULT_SQLITE_PATH

    return DEFAULT_SQLITE_PATH.parent


def _observed_publication_times() -> list[datetime]:
    """Publication times seen so far, for deriving the issuance schedule.

    Returns an empty list before any exist, which makes
    :func:`cadence_for` fall back to the documented NDFD cycle and record that
    it did so.
    """
    from sqlalchemy import select

    from pmvl_markets.db_models import SourceObservationRow

    try:
        with session_scope() as db:
            return [
                value
                for value in db.scalars(
                    select(SourceObservationRow.published_at)
                    .where(SourceObservationRow.published_at.is_not(None))
                    .order_by(SourceObservationRow.published_at.desc())
                    .limit(500)
                ).all()
                if value is not None
            ]
    except Exception:  # noqa: BLE001
        return []


def _day_start(day: str | None, fallback: Any) -> Any:
    """Midnight UTC of ``day``, or the fallback when it cannot be parsed."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    if not day:
        return fallback
    try:
        return _dt.strptime(day, "%Y-%m-%d").replace(tzinfo=_tz.utc)
    except ValueError:
        return fallback


def _weather_day_for(market: Any) -> str | None:
    """The local calendar day a weather contract measures, from its ticker.

    Kalshi encodes it as ``KXHIGHNY-26AUG26-T87`` -> 2026-08-26. Parsed rather
    than derived from ``expected_resolution_time``, which is the payout instant
    and lands on the following morning.
    """
    from datetime import datetime as _dt

    ticker = getattr(market, "platform_market_id", "") or ""
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    try:
        return _dt.strptime(parts[1], "%y%b%d").date().isoformat()
    except ValueError:
        return None


async def job_forecast_observations() -> dict[str, Any]:
    """Collect NWS gridpoint forecasts for every settlement station.

    This is the collector the source-lag hypothesis depends on. ``updateTime`` is
    the only field in either NWS product that records when the source *published*,
    and it is what separates "the market was slow" from "our poll was slow" -
    which imply opposite trades.

    Forecasts are attached to the open research markets for each station, so a
    later reader can join a revision to the contracts it was relevant to without
    re-deriving the mapping.
    """
    from sqlalchemy import select

    from pmvl_shared.payload_store import PayloadStore
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.alpha.collection import (
        PollWindow,
        TimestampAbsence,
        record_poll_attempt,
        record_source_observation,
    )
    from pmvl_markets.db_models import Market
    from pmvl_markets.normalize.weather_stations import (
        RESEARCH_SERIES_PREFIX,
        STATIONS,
    )
    from pmvl_markets.providers.nws_observations import (
        PARSER_VERSION,
        NwsForecastProvider,
    )

    store = PayloadStore(_research_data_dir() / "payloads")
    provider = NwsForecastProvider()
    written, attempted, failed, with_published = 0, 0, 0, 0
    errors: list[str] = []

    with job_run("forecast_observations") as details:
        try:
            for station in STATIONS:
                poll = PollWindow(
                    started_at=utcnow(), endpoint=f"/gridpoints (for {station.station_id})"
                )
                attempted += 1
                try:
                    fetch = await provider.fetch_forecast(
                        station.station_id, station.latitude, station.longitude
                    )
                except Exception as exc:  # noqa: BLE001
                    poll.finish()
                    with session_scope() as db:
                        record_poll_attempt(
                            db, source_id="nws", source_product="gridpoint_forecast",
                            target=station.station_id, poll=poll, success=False,
                            error_type="unhandled", error_detail=str(exc),
                        )
                    failed += 1
                    errors.append(f"{station.station_id}: {exc}")
                    continue

                poll.endpoint = fetch.endpoint
                poll.finish(
                    http_status=fetch.http_status,
                    served_from_cache=fetch.served_from_cache,
                )
                with session_scope() as db:
                    record_poll_attempt(
                        db, source_id="nws", source_product="gridpoint_forecast",
                        target=station.station_id, poll=poll, success=fetch.success,
                        error_type=fetch.error_type, error_detail=fetch.error_detail,
                    )
                    if not fetch.success:
                        failed += 1
                        continue
                    if fetch.has_publication_time:
                        with_published += 1

                    markets = db.scalars(
                        select(Market).where(
                            Market.series_ticker.like(f"{RESEARCH_SERIES_PREFIX}%"),
                            Market.settlement_station_id == station.station_id,
                            Market.status == "open",
                        )
                    ).all()
                    for market in markets:
                        # The WEATHER day, from the ticker - not the resolution
                        # day.
                        #
                        # `expected_resolution_time` is when the contract pays,
                        # which for these markets is the morning AFTER the day
                        # being measured. Using it looked up tomorrow's forecast
                        # and then stored it against today's validity window, so
                        # every row's value described the day after the day it
                        # claimed. Verified against the live gridpoint on
                        # 2026-08-26: KNYC forecast 82F for the 26th and 81F for
                        # the 27th, and the row labelled 2026-08-26 held 81.
                        #
                        # A one-day shift is invisible in any aggregate - the
                        # values are plausible temperatures either way - and
                        # silently destroys every forecast-to-outcome join.
                        target_day = _weather_day_for(market)
                        value = (
                            fetch.max_temperature_f.get(target_day)
                            if target_day
                            else None
                        )
                        if value is None:
                            continue
                        absence = TimestampAbsence()
                        if fetch.source_generated_at is None:
                            absence.note(
                                "source_generated_at",
                                "gridpoint payload carried no generatedAt",
                            )
                        row = record_source_observation(
                            db,
                            market_id=market.id,
                            source_id="nws",
                            source_product="gridpoint_forecast",
                            kind="source_point_value",
                            value=value,
                            poll=poll,
                            published_at=fetch.published_at,
                            source_generated_at=fetch.source_generated_at,
                            # The day THIS value describes, not the first day
                            # in the gridpoint payload. They differ whenever a
                            # market's weather day is not the nearest forecast
                            # day, and a row whose value and validity window
                            # disagree is worse than a missing row: it joins
                            # cleanly and is wrong.
                            forecast_valid_start=_day_start(
                                target_day, fetch.forecast_valid_start
                            ),
                            forecast_valid_end=fetch.forecast_valid_end,
                            # The grid cell the forecast covers, kept apart from
                            # the station the contract settles on. Conflating
                            # them produced an 8F error here once already.
                            source_location=fetch.grid_endpoint,
                            settlement_station_id=station.station_id,
                            revision_id=(
                                f"{station.station_id}:{fetch.published_at.isoformat()}"
                                if fetch.published_at
                                else ""
                            ),
                            raw_payload=fetch.raw_payload,
                            payload_store=store,
                            absence=absence,
                            parser_version=PARSER_VERSION,
                        )
                        if row is not None:
                            written += 1
        finally:
            await provider.aclose()

        details.update(
            {
                "stations_polled": attempted,
                "observations_written": written,
                "failed_polls": failed,
                "stations_with_published_at": with_published,
                "cache_hits": provider.cache_hits,
                "records_written": written,
            }
        )
        if errors:
            details["errors"] = errors[:10]
        return dict(details)


async def job_research_trades(*, limit: int = 100) -> dict[str, Any]:
    """Archive public trade prints for the research universe, uncapped.

    A separate job from the product's ``_ingest_trades``, which samples only the
    top 25 markets by volume per platform. Weather contracts frequently miss that
    cut, so relying on it would leave the research universe with no tape at all -
    and a print that was never captured cannot be recovered.

    Prints are archived raw. Nothing in this phase reads them.
    """
    from sqlalchemy import select

    from pmvl_shared.research_archive import ResearchArchive, default_archive_path
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.alpha.archiving import archive_trades
    from pmvl_markets.db_models import Market
    from pmvl_markets.ingest.runner import _market_from_row
    from pmvl_markets.normalize.weather_stations import RESEARCH_SERIES_PREFIX
    from pmvl_markets.providers.kalshi import KalshiProvider

    archive = ResearchArchive(default_archive_path(_research_data_dir().parent))

    with job_run("research_trades") as details:
        with session_scope() as db:
            rows = db.scalars(
                select(Market).where(
                    Market.series_ticker.like(f"{RESEARCH_SERIES_PREFIX}%"),
                    Market.status == "open",
                )
            ).all()
            markets = [_market_from_row(r) for r in rows]

        provider = KalshiProvider()
        collected: dict[str, list[Any]] = {}
        by_key: dict[str, Any] = {}
        errors: list[str] = []
        try:
            for market in markets:
                key = f"{market.platform.value}:{market.platform_market_id}"
                by_key[key] = market
                try:
                    prints = await provider.get_trades(market, limit=limit)
                except Exception as exc:  # noqa: BLE001
                    # One market's failure must not stop the rest of the tape.
                    errors.append(f"{market.platform_market_id}: {exc}")
                    continue
                if prints:
                    collected[key] = list(prints)
        finally:
            await provider.aclose()

        report = archive_trades(archive, collected, by_key, available_at=utcnow())
        stats = archive.stats()
        details.update(
            {
                "markets_polled": len(markets),
                "markets_with_prints": len(collected),
                "trades_archived": report.trades_archived,
                "archive_trades_total": stats.trades,
                "records_written": report.trades_archived,
            }
        )
        if errors or report.errors:
            details["errors"] = (errors + report.errors)[:10]
        return dict(details)


def job_collection_health() -> dict[str, Any]:
    """Persist a daily collection-health snapshot to the research archive.

    The CLI report answers "is collection healthy now". This answers "was it
    healthy on the 14th" - the question that matters when a research result
    turns out to rest on a week with a silent gap.

    Append-only: a later good day cannot erase an earlier bad one.
    """
    from pmvl_shared.research_archive import ResearchArchive, default_archive_path

    from pmvl_markets.alpha.collection_health import build_report

    archive = ResearchArchive(default_archive_path(_research_data_dir().parent))

    with job_run("collection_health") as details:
        with session_scope() as db:
            report = build_report(db, days=1)
        with archive.connect() as con:
            snapshot_id = archive.record_health(
                con, report, collector_version=COLLECTOR_VERSION
            )
            history = archive.health_history(con, limit=7)
        details.update(
            {
                "snapshot_id": snapshot_id,
                "all_gates_passing": report["all_gates_passing"],
                "failing_gates": [
                    g["name"] for g in report["gates"] if not g["passing"]
                ],
                "snapshots_retained": len(history),
                "records_written": 1,
            }
        )
        return dict(details)


async def job_daily_climate(*, limit_per_station: int = 4) -> dict[str, Any]:
    """Archive the daily CLI maximum every weather contract settles on.

    This is the job that removes SETTLEMENT_MISMATCH. Until it runs, the
    repository holds hourly observations and the settled quantity has never been
    observed - a gap no amount of modelling can close, because the label itself
    is absent.

    Append-only and idempotent on ``(station, date, payload hash, available_at)``:
    a re-poll of an unchanged bulletin writes nothing, while a genuine revision
    lands as a new row beside the original rather than replacing it. A label must
    be reconstructable as it stood at any past instant, which an in-place update
    destroys.

    Preliminary bulletins are stored, not discarded. The afternoon issuance is a
    real observation of the day so far and is useful for intraday work; it is
    marked ``is_final=False`` so a settlement label can refuse it.
    """
    from pmvl_shared.payload_store import PayloadStore
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.normalize.weather_stations import research_station_ids
    from pmvl_markets.providers.nws_cli import (
        COLLECTOR_VERSION,
        PARSER_VERSION,
        NwsCliProvider,
    )

    store = PayloadStore(_research_data_dir() / "payloads")
    provider = NwsCliProvider()
    written = duplicates = finals = preliminary = 0
    per_station: dict[str, int] = {}
    errors: list[str] = []
    unmapped: list[str] = []

    with job_run("daily_climate") as details:
        try:
            for station_id in research_station_ids():
                try:
                    fetches, report = await provider.fetch_recent(
                        station_id, limit=limit_per_station
                    )
                except Exception as exc:  # noqa: BLE001
                    # One station's outage costs that station's coverage, never
                    # the whole sweep.
                    errors.append(f"{station_id}: {type(exc).__name__}: {exc}")
                    continue
                errors.extend(report.errors)
                errors.extend(report.parse_failures)
                unmapped.extend(report.unmapped_stations)

                for fetch in fetches:
                    try:
                        ref = store.put(fetch.raw_text.encode("utf-8")).ref
                    except Exception:  # noqa: BLE001
                        ref = ""
                    if _record_daily_climate(fetch, raw_payload_ref=ref,
                                             collector_version=COLLECTOR_VERSION,
                                             parser_version=PARSER_VERSION):
                        written += 1
                        per_station[station_id] = per_station.get(station_id, 0) + 1
                        if fetch.climate.is_final:
                            finals += 1
                        else:
                            preliminary += 1
                    else:
                        duplicates += 1
                details["heartbeat"]()
        finally:
            await provider.aclose()

        result = {
            "rows_written": written,
            "duplicates_skipped": duplicates,
            "final_reports": finals,
            "preliminary_reports": preliminary,
            "per_station": dict(sorted(per_station.items())),
            "unmapped_stations": sorted(set(unmapped)),
            "cache_hits": provider.cache_hits,
            "errors": errors[:10],
        }
        details.update(result)
        details["records_written"] = written
        return result


def _record_daily_climate(
    fetch: Any, *, raw_payload_ref: str, collector_version: str, parser_version: str
) -> bool:
    """Insert one bulletin, or report that it was already known.

    Returns False for an exact re-read. Identity is the payload hash together
    with the availability instant, so an unchanged bulletin polled twice is one
    row while a corrected one is two.
    """
    from pmvl_shared.db.models import DailyClimateObservationRow
    from pmvl_shared.timeutil import utcnow

    with session_scope() as db:
        existing = db.scalar(
            select(DailyClimateObservationRow).where(
                DailyClimateObservationRow.station_id == fetch.station_id,
                DailyClimateObservationRow.observation_date
                == fetch.climate.observation_date.isoformat(),
                DailyClimateObservationRow.raw_payload_hash == fetch.raw_payload_hash,
            )
        )
        if existing is not None:
            return False
        db.add(
            DailyClimateObservationRow(
                station_id=fetch.station_id,
                product_code=fetch.climate.product_code,
                observation_date=fetch.climate.observation_date.isoformat(),
                daily_max_f=(
                    float(fetch.climate.daily_max_f)
                    if fetch.climate.daily_max_f is not None else None
                ),
                daily_min_f=(
                    float(fetch.climate.daily_min_f)
                    if fetch.climate.daily_min_f is not None else None
                ),
                is_final=fetch.climate.is_final,
                valid_as_of=fetch.climate.valid_as_of,
                published_at=fetch.published_at,
                available_at=fetch.available_at,
                ingested_at=utcnow(),
                is_backfill=fetch.is_backfill,
                source_product_id=fetch.source_product_id,
                source_endpoint=fetch.source_endpoint,
                raw_payload_hash=fetch.raw_payload_hash,
                raw_payload_ref=raw_payload_ref,
                collector_version=collector_version,
                parser_version=parser_version,
                provenance="live",
            )
        )
        return True


async def job_forecast_validity() -> dict[str, Any]:
    """Audit newly collected forecasts against their own immutable raw payloads.

    Runs continuously, not once. Every new forecast row lands with
    ``validity_status='UNKNOWN'`` and ``research_eligible=0`` because the column
    fails closed, so without this job the eligible set silently stops growing
    while collection appears healthy - the archive fills up and no new
    observation ever becomes usable.

    Verifying rather than trusting is deliberate. The collector now writes the
    correct target day, so new rows *should* be correct by construction - but
    "correct by construction" is precisely the assumption that produced 3,102
    misaligned rows, and the payload is right there to check against.

    Idempotent: rows already audited are re-checked cheaply and corrections are
    never duplicated.
    """
    from pmvl_shared.payload_store import PayloadStore

    from pmvl_markets.research.rederive import classify_and_rederive

    with job_run("forecast_validity") as details:
        store = PayloadStore(_research_data_dir() / "payloads")
        with session_scope() as db:
            report = classify_and_rederive(db, store)
        payload = report.as_dict()
        details.update(payload)
        details["records_written"] = report.rederived
        return payload
