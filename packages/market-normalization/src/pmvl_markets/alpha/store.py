"""Persistence for the point-in-time store and the shadow ledger.

Two write paths, both append-only, both with the same shape of guarantee:

* :func:`record_observation` writes one source observation and returns whether it
  was new. A re-poll that sees an unchanged value is a no-op rather than a
  duplicate, enforced by a uniqueness constraint rather than by a check-then-act
  the way concurrent writers would race.
* :func:`publish_decision` writes a sealed shadow decision, and refuses to write
  a second one under an ID that already exists.

Nothing here updates a row that has already been written. That is the whole
contract, and it is enforced by there being no update statement in the module.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pmvl_shared.enums import DataProvenance
from pmvl_shared.timeutil import ensure_utc, utcnow

from ..db_models import ShadowDecisionRow, ShadowOutcomeRow, SourceObservationRow
from .pit import SourceObservation
from .shadow import ImmutabilityError, ShadowDecision, ShadowOutcome, verify_unchanged


def record_observation(
    session: Session,
    *,
    market_id: int,
    observation: SourceObservation[Any],
    provenance: DataProvenance = DataProvenance.LIVE,
) -> bool:
    """Persist one observation. Returns True when a new row was written.

    ``available_at`` is required. An observation that cannot say when it became
    knowable cannot be placed in time, and storing it with a guessed availability
    would decide, silently, whether a past decision was allowed to see it.

    The insert is attempted and the uniqueness violation caught, rather than
    checking for an existing row first. Two ingest workers polling the same
    source race the check-then-act version, and the loser writes a duplicate that
    every dispersion and lag factor then counts twice.
    """
    if observation.available_at is None:
        raise ValueError(
            f"{observation.kind!r} from {observation.source_id!r} has no "
            "available_at; it cannot be stored as a point-in-time observation"
        )

    value, value_json = _split_value(observation.value)
    row = SourceObservationRow(
        market_id=market_id,
        source_id=observation.source_id,
        kind=observation.kind,
        value=value,
        value_json=value_json,
        event_time=ensure_utc(observation.event_time),
        observed_at=ensure_utc(observation.observed_at),
        available_at=ensure_utc(observation.available_at),
        ingested_at=utcnow(),
        source_endpoint=str(observation.provenance.get("endpoint", "")),
        source_payload_hash=str(observation.provenance.get("payload_hash", "")),
        parser_version=str(observation.provenance.get("parser_version", "")),
        provenance=provenance,
    )
    savepoint = session.begin_nested()
    try:
        session.add(row)
        savepoint.commit()
        return True
    except IntegrityError:
        savepoint.rollback()
        return False


def load_observations(
    session: Session,
    *,
    market_id: int,
    as_of: datetime | None = None,
    kinds: Sequence[str] = (),
) -> tuple[SourceObservation[Any], ...]:
    """Read observations back as point-in-time records.

    ``as_of`` filters in the query rather than after it. Loading everything and
    filtering in Python would work and would put the future in memory beside the
    past, one attribute access away from a leak; keeping the cutoff in the
    ``WHERE`` clause means the rows never arrive.
    """
    query = select(SourceObservationRow).where(
        SourceObservationRow.market_id == market_id
    )
    if as_of is not None:
        query = query.where(SourceObservationRow.available_at <= ensure_utc(as_of))
    if kinds:
        query = query.where(SourceObservationRow.kind.in_(list(kinds)))
    query = query.order_by(
        SourceObservationRow.available_at, SourceObservationRow.id
    )
    return tuple(_to_observation(row) for row in session.scalars(query).all())


def publish_decision(
    session: Session,
    decision: ShadowDecision,
    *,
    provenance: DataProvenance = DataProvenance.LIVE,
) -> ShadowDecisionRow:
    """Write a sealed shadow decision, once.

    Verifies the seal before writing. A decision whose content no longer matches
    its own hash must not be published: whatever changed it did so between
    sealing and storage, and persisting it would make the drift permanent and
    invisible.
    """
    verify_unchanged(decision)
    existing = session.scalar(
        select(ShadowDecisionRow).where(
            ShadowDecisionRow.decision_id == decision.decision_id
        )
    )
    if existing is not None:
        if existing.content_hash == decision.content_hash:
            # Same decision, published twice - a rerun of a deterministic pass.
            # Idempotent, and NOT an error: the stored row already says exactly
            # this.
            return existing
        raise ImmutabilityError(
            f"shadow decision {decision.decision_id} is already published with a "
            f"different content hash. A published decision is never replaced; if "
            f"this is genuinely a new decision it needs a new identity."
        )

    row = ShadowDecisionRow(
        decision_id=decision.decision_id,
        strategy=decision.strategy,
        strategy_version=decision.strategy_version,
        market_key=decision.market_key,
        decision_time=ensure_utc(decision.decision_time),
        status=decision.status.value,
        side=decision.side.value,
        liquidity=decision.liquidity.value,
        model_probability=decision.model_probability,
        model_probability_low=decision.model_probability_low,
        model_probability_high=decision.model_probability_high,
        conservative_probability=decision.conservative_probability,
        executable_probability=decision.executable_probability,
        alpha_expected_move=decision.alpha_expected_move,
        alpha_confidence=decision.alpha_confidence,
        proposed_price=decision.proposed_price,
        proposed_size=decision.proposed_size,
        expected_edge_per_contract=decision.expected_edge_per_contract,
        information_cutoff=ensure_utc(decision.information_cutoff),
        outcome_model_version=decision.outcome_model_version,
        alpha_model_version=decision.alpha_model_version,
        factor_versions=dict(decision.factor_versions),
        feature_values=dict(decision.feature_values),
        source_values=dict(decision.source_values),
        rationale=list(decision.rationale),
        content_hash=decision.content_hash,
        published_at=utcnow(),
        provenance=provenance,
    )
    session.add(row)
    session.flush()
    return row


def append_outcome(
    session: Session,
    outcome: ShadowOutcome,
    *,
    provenance: DataProvenance = DataProvenance.LIVE,
) -> ShadowOutcomeRow:
    """Append what happened after a decision.

    Refuses an outcome that predates its decision. That is either a clock problem
    or a backfill, and both would make the record claim foreknowledge - which is
    the one thing a shadow ledger exists to rule out.
    """
    decision = session.scalar(
        select(ShadowDecisionRow).where(
            ShadowDecisionRow.decision_id == outcome.decision_id
        )
    )
    if decision is None:
        raise KeyError(
            f"no published decision {outcome.decision_id} to attach an outcome to"
        )
    if ensure_utc(outcome.observed_at) < ensure_utc(decision.decision_time):
        raise ValueError(
            f"outcome observed at {outcome.observed_at.isoformat()} predates "
            f"decision {outcome.decision_id} at "
            f"{decision.decision_time.isoformat()}"
        )
    row = ShadowOutcomeRow(
        decision_id=outcome.decision_id,
        kind=outcome.kind,
        observed_at=ensure_utc(outcome.observed_at),
        value=outcome.value,
        detail=dict(outcome.detail),
        recorded_at=utcnow(),
        provenance=provenance,
    )
    session.add(row)
    session.flush()
    return row


def audit_decisions(session: Session, *, strategy: str | None = None) -> list[str]:
    """Recompute every stored decision's hash. Empty means the ledger is intact.

    The check that makes the immutability claim more than an intention. It reads
    the stored fields, rebuilds the digest the way the writer did, and reports a
    mismatch - it never rewrites, because rewriting would erase the evidence
    that anything moved.
    """
    query = select(ShadowDecisionRow)
    if strategy is not None:
        query = query.where(ShadowDecisionRow.strategy == strategy)
    query = query.order_by(ShadowDecisionRow.decision_time, ShadowDecisionRow.id)

    problems: list[str] = []
    for row in session.scalars(query).all():
        try:
            verify_unchanged(_to_decision(row))
        except ImmutabilityError as exc:
            problems.append(str(exc))
    return problems


def _to_decision(row: ShadowDecisionRow) -> ShadowDecision:
    from pmvl_shared.enums import Side  # noqa: PLC0415

    from .models import Liquidity  # noqa: PLC0415
    from .shadow import ShadowStatus  # noqa: PLC0415

    return ShadowDecision(
        decision_id=row.decision_id,
        strategy=row.strategy,
        strategy_version=row.strategy_version,
        market_key=row.market_key,
        decision_time=ensure_utc(row.decision_time),
        status=ShadowStatus(row.status),
        side=Side(row.side),
        liquidity=Liquidity(row.liquidity),
        model_probability=row.model_probability,
        model_probability_low=row.model_probability_low,
        model_probability_high=row.model_probability_high,
        conservative_probability=row.conservative_probability,
        executable_probability=row.executable_probability,
        alpha_expected_move=row.alpha_expected_move,
        alpha_confidence=row.alpha_confidence,
        proposed_price=row.proposed_price,
        proposed_size=row.proposed_size,
        expected_edge_per_contract=row.expected_edge_per_contract,
        information_cutoff=ensure_utc(row.information_cutoff),
        outcome_model_version=row.outcome_model_version,
        alpha_model_version=row.alpha_model_version,
        factor_versions=dict(row.factor_versions or {}),
        feature_values=dict(row.feature_values or {}),
        source_values=dict(row.source_values or {}),
        rationale=tuple(row.rationale or ()),
        content_hash=row.content_hash,
    )


def _to_observation(row: SourceObservationRow) -> SourceObservation[Any]:
    value: Any = row.value if row.value is not None else row.value_json
    return SourceObservation(
        source_id=row.source_id,
        kind=row.kind,
        value=value,
        available_at=ensure_utc(row.available_at),
        observed_at=ensure_utc(row.observed_at),
        event_time=ensure_utc(row.event_time),
        ingested_at=ensure_utc(row.ingested_at),
        provenance={
            "endpoint": row.source_endpoint,
            "payload_hash": row.source_payload_hash,
            "parser_version": row.parser_version,
        },
    )


def _split_value(value: Any) -> tuple[Decimal | None, dict[str, Any] | None]:
    """Store a numeric value in the Money column, anything else as JSON.

    Not "coerce everything to a string". A probability that lands in a TEXT
    column cannot be compared or ordered by the database, and the ``Money``
    column exists precisely so Decimal survives both backends exactly.
    """
    if isinstance(value, Decimal):
        return value, None
    if isinstance(value, bool):
        return None, {"value": value}
    if isinstance(value, int):
        return Decimal(value), None
    if isinstance(value, float):
        return Decimal(str(value)), None
    if isinstance(value, str):
        try:
            return Decimal(value), None
        except (ArithmeticError, ValueError):
            return None, {"value": value}
    return None, {"value": value}


def observation_kinds(session: Session, *, market_id: int) -> tuple[str, ...]:
    """Which observation kinds exist for a market. Cheap coverage probe."""
    rows = session.scalars(
        select(SourceObservationRow.kind)
        .where(SourceObservationRow.market_id == market_id)
        .distinct()
    ).all()
    return tuple(sorted(rows))


def record_observations(
    session: Session,
    *,
    market_id: int,
    observations: Iterable[SourceObservation[Any]],
    provenance: DataProvenance = DataProvenance.LIVE,
) -> int:
    """Record many, returning how many were new."""
    return sum(
        1
        for observation in observations
        if record_observation(
            session,
            market_id=market_id,
            observation=observation,
            provenance=provenance,
        )
    )
