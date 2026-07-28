"""Immutable daily recommendation snapshots.

A snapshot freezes exactly what was published: the entry price at that moment, the
model's probability and interval, the confidence, the evidence, and the top of the
order book that justified the executable price.

Nothing in this codebase updates a snapshot's decision fields. The only later write
is the settlement outcome (see :mod:`.settlement`). That asymmetry is the entire
point: without it, a price that moved favourably could be back-written into history
and the track record would measure nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmvl_shared.enums import DataProvenance, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import HORIZONS, utcnow

from ..db_models import (
    EvidenceItem,
    Market,
    ModelPrediction,
    OrderbookLevel,
    OrderbookSnapshot,
    Recommendation,
    RecommendationSnapshot,
)

log = get_logger(__name__)


@dataclass
class SnapshotReport:
    snapshot_date: date | None = None
    batch_id: str = ""
    written: int = 0
    skipped_existing: int = 0
    by_horizon: dict[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_date": self.snapshot_date.isoformat() if self.snapshot_date else None,
            "batch_id": self.batch_id,
            "written": self.written,
            "skipped_existing": self.skipped_existing,
            "by_horizon": self.by_horizon or {},
        }


def latest_batch_id(
    session: Session, *, provenance: str = DataProvenance.LIVE.value
) -> str | None:
    """Most recent batch of the requested provenance.

    Scoped to live by default. Taking the newest batch regardless of provenance meant
    that with a demo dataset present, the scheduled snapshot job froze *demo*
    recommendations into the track record instead of the real ones - the rows stayed
    correctly labelled, but the day's genuine recommendations went unrecorded.
    """
    return session.scalar(
        select(Recommendation.batch_id)
        .where(Recommendation.provenance == provenance)
        .order_by(Recommendation.created_at.desc())
        .limit(1)
    )


def write_daily_snapshot(
    session: Session,
    *,
    batch_id: str | None = None,
    now: datetime | None = None,
    force: bool = False,
    provenance: str = DataProvenance.LIVE.value,
) -> SnapshotReport:
    """Freeze the current recommendation batch into the immutable record.

    Idempotent by ``(batch_id, market_id, horizon, side)``: re-running the job on the
    same batch adds nothing. ``force`` is *not* an override for that - it only allows
    a second snapshot on the same calendar day from a different batch.
    """
    now = now or utcnow()
    report = SnapshotReport(snapshot_date=now.date())

    batch_id = batch_id or latest_batch_id(session, provenance=provenance)
    if not batch_id:
        log.info("no recommendations to snapshot")
        return report
    report.batch_id = batch_id

    if not force:
        same_day_other_batch = session.scalar(
            select(func.count())
            .select_from(RecommendationSnapshot)
            .where(
                RecommendationSnapshot.snapshot_date == now.date(),
                RecommendationSnapshot.batch_id != batch_id,
            )
        )
        if same_day_other_batch:
            log.info(
                "a snapshot already exists for %s from a different batch; "
                "pass force=True to add another",
                now.date(),
            )

    recommendations = session.scalars(
        select(Recommendation).where(Recommendation.batch_id == batch_id)
    ).all()
    if not recommendations:
        return report

    market_ids = {r.market_id for r in recommendations}
    markets = {
        m.id: m for m in session.scalars(select(Market).where(Market.id.in_(market_ids)))
    }
    evidence = _evidence_by_market(session, market_ids)
    books = _orderbook_by_market(session, market_ids)

    by_horizon: dict[str, int] = {h: 0 for h in HORIZONS}

    for rec in recommendations:
        market = markets.get(rec.market_id)
        if market is None:
            continue

        exists = session.scalar(
            select(RecommendationSnapshot.id).where(
                RecommendationSnapshot.batch_id == batch_id,
                RecommendationSnapshot.market_id == rec.market_id,
                RecommendationSnapshot.horizon == rec.horizon,
                RecommendationSnapshot.side == rec.side,
            )
        )
        if exists:
            report.skipped_existing += 1
            continue

        prediction = (
            session.get(ModelPrediction, rec.prediction_id) if rec.prediction_id else None
        )

        session.add(
            RecommendationSnapshot(
                batch_id=batch_id,
                snapshot_date=now.date(),
                recommendation_id=rec.id,
                recommendation_created_at=rec.created_at,
                market_id=rec.market_id,
                platform=market.platform,
                platform_market_id=market.platform_market_id,
                market_title=market.title,
                horizon=rec.horizon,
                rank=rec.rank,
                side=rec.side,
                entry_price_at_recommendation=rec.entry_price,
                total_cost_at_recommendation=rec.total_cost_per_contract,
                executable_size=rec.executable_size,
                fair_probability=rec.fair_probability,
                fair_probability_low=rec.fair_probability_low,
                fair_probability_high=rec.fair_probability_high,
                expected_value=rec.net_ev_per_contract,
                conservative_net_ev=rec.conservative_net_ev,
                model_confidence=rec.model_confidence,
                model_version=rec.model_version,
                expected_resolution_time=rec.expected_resolution_time,
                evidence_snapshot={
                    "items": evidence.get(rec.market_id, []),
                    "explanation": prediction.explanation if prediction else "",
                    "components": prediction.components if prediction else [],
                    "has_independent_prior": (
                        prediction.has_independent_prior if prediction else False
                    ),
                    "market_implied_probability": (
                        str(prediction.market_implied_probability)
                        if prediction and prediction.market_implied_probability is not None
                        else None
                    ),
                },
                orderbook_snapshot=books.get(rec.market_id),
                risk_flags=rec.risk_flags,
                provenance=rec.provenance,
            )
        )
        report.written += 1
        by_horizon[rec.horizon] = by_horizon.get(rec.horizon, 0) + 1

    session.flush()
    report.by_horizon = by_horizon
    log.info(
        "snapshot %s: %d written, %d already present",
        now.date(), report.written, report.skipped_existing,
    )
    return report


def _evidence_by_market(
    session: Session, market_ids: set[int], *, per_market: int = 8
) -> dict[int, list[dict[str, Any]]]:
    if not market_ids:
        return {}
    out: dict[int, list[dict[str, Any]]] = {}
    rows = session.scalars(
        select(EvidenceItem)
        .where(EvidenceItem.market_id.in_(market_ids))
        .order_by(EvidenceItem.published_at.desc())
    ).all()
    for row in rows:
        bucket = out.setdefault(row.market_id, [])
        if len(bucket) >= per_market:
            continue
        bucket.append(
            {
                "source_name": row.source_name,
                "source_url": row.source_url,
                "title": row.title,
                "summary": row.summary,
                "stance": row.stance,
                "published_at": row.published_at.isoformat() if row.published_at else None,
                "is_novel": row.is_novel,
                "source_quality": str(row.source_quality),
            }
        )
    return out


def _orderbook_by_market(
    session: Session, market_ids: set[int], *, depth: int = 5
) -> dict[int, dict[str, Any]]:
    """Top-of-book at snapshot time, frozen alongside the recommendation.

    Without this the backtest could not distinguish "the edge was real but the depth
    was two contracts" from "the edge was real and tradeable".
    """
    if not market_ids:
        return {}

    out: dict[int, dict[str, Any]] = {}
    for market_id in market_ids:
        snapshot = session.scalar(
            select(OrderbookSnapshot)
            .where(OrderbookSnapshot.market_id == market_id)
            .order_by(OrderbookSnapshot.observed_at.desc())
            .limit(1)
        )
        if snapshot is None:
            continue
        levels = session.scalars(
            select(OrderbookLevel)
            .where(OrderbookLevel.snapshot_id == snapshot.id)
            .order_by(OrderbookLevel.level_index)
        ).all()

        def side_levels(
            side: str, is_ask: bool, rows=levels  # noqa: B008 - bound per iteration
        ) -> list[dict[str, str]]:
            return [
                {"price": str(l.price), "size": str(l.size)}
                for l in rows
                if l.side == side and l.is_ask == is_ask
            ][:depth]

        out[market_id] = {
            "observed_at": snapshot.observed_at.isoformat(),
            "best_yes_bid": str(snapshot.best_yes_bid) if snapshot.best_yes_bid else None,
            "best_yes_ask": str(snapshot.best_yes_ask) if snapshot.best_yes_ask else None,
            "best_no_bid": str(snapshot.best_no_bid) if snapshot.best_no_bid else None,
            "best_no_ask": str(snapshot.best_no_ask) if snapshot.best_no_ask else None,
            "yes_depth_usd": str(snapshot.yes_depth_usd) if snapshot.yes_depth_usd else None,
            "no_depth_usd": str(snapshot.no_depth_usd) if snapshot.no_depth_usd else None,
            "yes_asks": side_levels(Side.YES.value, True),
            "no_asks": side_levels(Side.NO.value, True),
        }
    return out
