"""Recording point-in-time observations, with the timestamp rules enforced.

Six instants, and exactly one of them gates admissibility
---------------------------------------------------------

``event_time``
    When the measured thing happened. A 14:00 temperature reading is 14:00
    forever, whoever reads it and whenever.

``published_at``
    When the **source** made it public. NWS ``updateTime``. This is what the
    source-lag hypothesis measures *from*, and it is the field whose absence
    made the hypothesis untestable.

``source_generated_at``
    When the source says it produced the product, where that differs from
    publication.

``poll_started_at`` / ``poll_completed_at``
    Our request interval. The value could have changed at any point inside it,
    so the honest availability is the *end*.

``available_at``
    When **this system** could first have used it. Derived from
    ``poll_completed_at``. **The only admissibility timestamp.**

The rule that matters
---------------------
``published_at`` never becomes ``available_at``. A backtest gated on
``published_at`` would trade on an NWS forecast at the instant NWS issued it -
possibly hours before any poll ran - and would report an edge that is a property
of the clock rather than of the market. :func:`record_source_observation`
enforces this: it takes the two separately and refuses to derive one from the
other.

Never fabricate
---------------
Where a product does not publish a timestamp, the column is NULL and
``timestamp_absence`` records that the source does not provide it. This is not
pedantry. The weather provider used to write ``parse_ts(updateTime) or utcnow()``,
so a response without ``updateTime`` silently reported a publication time equal
to the poll time - **zero lag, for exactly the observations whose lag is
unknown**. That is a bias toward finding the market slow, in the one measurement
the whole programme turns on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pmvl_shared.enums import DataProvenance
from pmvl_shared.payload_store import PayloadStore
from pmvl_shared.timeutil import ensure_utc, utcnow

from ..db_models import PollAttemptRow, SourceObservationRow, StationObservationRow

#: Bumped when the collection code changes in a way that could alter what is
#: captured. Stored on every row so a later reader can tell which collector
#: produced it rather than assuming one behaviour across the whole archive.
COLLECTOR_VERSION = "collector-v1.0.0"


class TimestampDisciplineError(ValueError):
    """A timestamp rule was violated."""


@dataclass
class PollWindow:
    """The interval a single upstream request occupied.

    ``available_at`` is ``poll_completed_at``, not ``poll_started_at``: the value
    could have changed at any moment inside the request, and claiming
    availability at the start would grant up to a full round-trip of look-ahead
    on every observation.
    """

    started_at: datetime
    completed_at: datetime | None = None
    served_from_cache: bool = False
    http_status: int | None = None
    endpoint: str = ""

    def finish(
        self, *, http_status: int | None = None, served_from_cache: bool = False
    ) -> "PollWindow":
        self.completed_at = utcnow()
        self.http_status = http_status
        self.served_from_cache = served_from_cache
        return self

    @property
    def available_at(self) -> datetime:
        if self.completed_at is None:
            raise TimestampDisciplineError(
                "poll has not completed; available_at is not yet defined and "
                "must not be guessed from the start time"
            )
        return self.completed_at

    @property
    def duration_ms(self) -> int | None:
        if self.completed_at is None:
            return None
        return int(
            (ensure_utc(self.completed_at) - ensure_utc(self.started_at)).total_seconds()
            * 1000
        )


@dataclass
class TimestampAbsence:
    """Which timestamps this product does not provide, and why.

    Stored alongside the NULLs so a later reader can tell "the source does not
    publish this" from "the collector forgot", and cannot be tempted to
    reconstruct a plausible value for either.
    """

    missing: dict[str, str] = field(default_factory=dict)

    def note(self, field_name: str, reason: str) -> "TimestampAbsence":
        self.missing[field_name] = reason
        return self

    def as_dict(self) -> dict[str, Any] | None:
        return {"missing": dict(self.missing)} if self.missing else None


def record_source_observation(
    session: Session,
    *,
    market_id: int,
    source_id: str,
    source_product: str,
    kind: str,
    value: Decimal | None,
    poll: PollWindow,
    raw_payload: bytes | None = None,
    payload_store: PayloadStore | None = None,
    published_at: datetime | None = None,
    source_generated_at: datetime | None = None,
    event_time: datetime | None = None,
    forecast_valid_start: datetime | None = None,
    forecast_valid_end: datetime | None = None,
    source_location: str = "",
    settlement_station_id: str = "",
    revision_id: str = "",
    supersedes_revision_id: str | None = None,
    model_run_cycle: str = "",
    absence: TimestampAbsence | None = None,
    value_json: Mapping[str, Any] | None = None,
    parser_version: str = "",
    provenance: DataProvenance = DataProvenance.LIVE,
) -> SourceObservationRow | None:
    """Persist one observation. Returns None when it was already recorded.

    ``available_at`` is taken from ``poll.completed_at`` and cannot be passed in.
    That is deliberate: a caller who could supply it could supply
    ``published_at``, and the resulting dataset would be silently un-backtestable
    with nothing to indicate which rows were affected.
    """
    available_at = poll.available_at

    if published_at is not None and ensure_utc(published_at) > ensure_utc(available_at):
        # The source cannot have published after we finished reading it. This is
        # a clock-skew or parse error, and accepting it would create rows whose
        # measured lag is negative.
        raise TimestampDisciplineError(
            f"published_at {published_at.isoformat()} is after available_at "
            f"{available_at.isoformat()}; a source cannot publish after we read it"
        )

    absence = absence or TimestampAbsence()
    if published_at is None:
        absence.note(
            "published_at",
            f"{source_product or source_id} does not expose a publication time "
            "in its payload; NOT defaulted to the poll time",
        )

    ref, payload_hash = "", ""
    if raw_payload is not None and payload_store is not None:
        stored = payload_store.put(raw_payload)
        ref, payload_hash = stored.ref, stored.sha256

    row = SourceObservationRow(
        market_id=market_id,
        source_id=source_id,
        source_product=source_product,
        kind=kind,
        value=value,
        value_json=dict(value_json) if value_json else None,
        event_time=ensure_utc(event_time),
        observed_at=ensure_utc(published_at),
        available_at=ensure_utc(available_at),
        ingested_at=utcnow(),
        published_at=ensure_utc(published_at),
        source_generated_at=ensure_utc(source_generated_at),
        poll_started_at=ensure_utc(poll.started_at),
        poll_completed_at=ensure_utc(poll.completed_at),
        forecast_valid_start=ensure_utc(forecast_valid_start),
        forecast_valid_end=ensure_utc(forecast_valid_end),
        source_location=source_location,
        settlement_station_id=settlement_station_id,
        revision_id=revision_id,
        supersedes_revision_id=supersedes_revision_id,
        model_run_cycle=model_run_cycle,
        source_endpoint=poll.endpoint,
        source_payload_hash=payload_hash,
        raw_payload_ref=ref,
        collector_version=COLLECTOR_VERSION,
        parser_version=parser_version,
        timestamp_absence=absence.as_dict(),
        provenance=provenance,
    )
    savepoint = session.begin_nested()
    try:
        session.add(row)
        savepoint.commit()
        return row
    except IntegrityError:
        savepoint.rollback()
        return None


def record_station_observation(
    session: Session,
    *,
    station_id: str,
    source_product: str,
    observation_time: datetime,
    poll: PollWindow,
    temperature_f: Decimal | None = None,
    measurements: Mapping[str, Any] | None = None,
    published_at: datetime | None = None,
    is_daily_summary: bool = False,
    revision_id: str = "",
    supersedes_revision_id: str | None = None,
    raw_payload: bytes | None = None,
    payload_store: PayloadStore | None = None,
    parser_version: str = "",
    absence: TimestampAbsence | None = None,
    provenance: DataProvenance = DataProvenance.LIVE,
) -> StationObservationRow | None:
    """Persist one observation at a settlement station.

    ``station_id`` is the station the contract settles on - never a forecast grid
    cell. The two are different places, and this repository has already shipped
    an 8F error from treating them as one.

    A missing ``published_at`` is recorded as an explicit absence, the same way
    ``record_source_observation`` does it. The NWS observations product carries
    no publication time at all, so without the note that NULL is an unexplained
    gap somebody could later "repair" with the observation time or the poll time
    - the second of which would report zero source lag on every row.
    """
    if not station_id.strip():
        raise TimestampDisciplineError(
            "station_id is required; an observation with no station cannot be "
            "matched to a contract's settlement definition"
        )

    absence = absence or TimestampAbsence()
    if published_at is None:
        absence.note(
            "published_at",
            f"{source_product or 'station observations'} does not expose a "
            "publication time; NOT defaulted to observation_time or poll time",
        )

    ref, payload_hash = "", ""
    if raw_payload is not None and payload_store is not None:
        stored = payload_store.put(raw_payload)
        ref, payload_hash = stored.ref, stored.sha256

    row = StationObservationRow(
        station_id=station_id,
        source_product=source_product,
        observation_time=ensure_utc(observation_time),
        published_at=ensure_utc(published_at),
        available_at=ensure_utc(poll.available_at),
        ingested_at=utcnow(),
        temperature_f=temperature_f,
        measurements=dict(measurements) if measurements else None,
        is_daily_summary=is_daily_summary,
        revision_id=revision_id,
        supersedes_revision_id=supersedes_revision_id,
        source_endpoint=poll.endpoint,
        raw_payload_hash=payload_hash,
        raw_payload_ref=ref,
        collector_version=COLLECTOR_VERSION,
        parser_version=parser_version,
        timestamp_absence=absence.as_dict(),
        provenance=provenance,
    )
    savepoint = session.begin_nested()
    try:
        session.add(row)
        savepoint.commit()
        return row
    except IntegrityError:
        savepoint.rollback()
        return None


def record_poll_attempt(
    session: Session,
    *,
    source_id: str,
    source_product: str,
    target: str,
    poll: PollWindow,
    success: bool,
    error_type: str = "",
    error_detail: str = "",
    retry_count: int = 0,
    provenance: DataProvenance = DataProvenance.LIVE,
) -> PollAttemptRow:
    """Record that a poll happened, whatever its outcome.

    Written for failures as well as successes, and that is the point. A gap in
    the observation series has two possible causes - the source did not change,
    or we could not reach it - and only this table distinguishes them. Without
    it, collector downtime becomes "the market was quiet" in every downstream
    statistic.
    """
    row = PollAttemptRow(
        source_id=source_id,
        source_product=source_product,
        target=target,
        started_at=ensure_utc(poll.started_at),
        completed_at=ensure_utc(poll.completed_at),
        duration_ms=poll.duration_ms,
        success=success,
        http_status=poll.http_status,
        error_type=error_type,
        error_detail=error_detail[:2000],
        retry_count=retry_count,
        served_from_cache=poll.served_from_cache,
        collector_version=COLLECTOR_VERSION,
        endpoint=poll.endpoint,
        provenance=provenance,
    )
    session.add(row)
    session.flush()
    return row


def coverage_report(session: Session, *, source_id: str | None = None) -> dict[str, Any]:
    """Observations against attempts: what was collected versus what was tried.

    The ratio is the honest coverage figure. A series with no gaps because
    nothing was ever attempted looks identical, in the observation table alone,
    to one with complete coverage.
    """
    obs_query = select(SourceObservationRow)
    poll_query = select(PollAttemptRow)
    if source_id:
        obs_query = obs_query.where(SourceObservationRow.source_id == source_id)
        poll_query = poll_query.where(PollAttemptRow.source_id == source_id)

    observations = session.scalars(obs_query).all()
    attempts = session.scalars(poll_query).all()
    failures = [a for a in attempts if not a.success]
    with_published = [o for o in observations if o.published_at is not None]

    return {
        "observations": len(observations),
        "poll_attempts": len(attempts),
        "failed_polls": len(failures),
        "failure_rate": (
            f"{len(failures) / len(attempts):.1%}" if attempts else "n/a"
        ),
        "error_types": _counts(a.error_type for a in failures if a.error_type),
        # The share that can measure source-to-market lag at all.
        "with_published_at": len(with_published),
        "published_at_coverage": (
            f"{len(with_published) / len(observations):.1%}" if observations else "n/a"
        ),
        "served_from_cache": sum(1 for a in attempts if a.served_from_cache),
    }


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def payload_roundtrip_ok(store: PayloadStore, ref: str, expected_hash: str) -> bool:
    """Whether a stored payload still hashes to what was recorded for it."""
    try:
        return store.digest_of(store.get(ref)) == expected_hash
    except (FileNotFoundError, ValueError):
        return False


def as_json_bytes(payload: Any) -> bytes:
    """Canonical JSON bytes for storage.

    ``sort_keys`` so an unchanged upstream response hashes identically across
    polls regardless of dict ordering - which is what makes deduplication work
    and what makes "the source did not change" a checkable statement.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
