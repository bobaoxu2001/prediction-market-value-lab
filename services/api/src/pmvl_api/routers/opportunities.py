"""Top-N value opportunities per resolution horizon."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.enums import MarketStatus, RecommendationState, Side
from pmvl_shared.timeutil import HORIZONS, ensure_utc, horizons_for, utcnow

from pmvl_markets.db_models import Market, ModelPrediction, Recommendation

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


def _serialize(rec: Recommendation, market: Market | None) -> dict[str, Any]:
    return {
        "id": rec.id,
        "rank": rec.rank,
        "horizon": rec.horizon,
        "batch_id": rec.batch_id,
        "market_id": rec.market_id,
        "platform": market.platform if market else None,
        "platform_market_id": market.platform_market_id if market else None,
        "title": market.title if market else "",
        "category": market.category if market else None,
        "side": rec.side,
        # Executable entry price - the VWAP of the ask ladder at reference size,
        # never a last-trade or midpoint.
        "entry_price": rec.entry_price,
        "current_price": rec.current_price,
        "total_cost_per_contract": rec.total_cost_per_contract,
        "executable_size": rec.executable_size,
        "fair_probability": rec.fair_probability,
        "fair_probability_low": rec.fair_probability_low,
        "fair_probability_high": rec.fair_probability_high,
        "net_edge": rec.net_ev_per_contract,
        "conservative_net_ev": rec.conservative_net_ev,
        "net_roi": rec.net_roi,
        "expected_profit_10": rec.expected_profit_10,
        "expected_profit_50": rec.expected_profit_50,
        "expected_profit_100": rec.expected_profit_100,
        "expected_profit_per_100_usd": rec.expected_profit_per_100_usd,
        "fractional_kelly": rec.fractional_kelly,
        "recommended_position_cap": rec.recommended_position_cap,
        "composite_score": rec.composite_score,
        "model_confidence": rec.model_confidence,
        "spread": rec.spread,
        "liquidity_usd": rec.liquidity_usd,
        "expected_resolution_time": rec.expected_resolution_time,
        "risk_flags": rec.risk_flags or [],
        "cost_breakdown": rec.cost_breakdown or {},
        "model_version": rec.model_version,
        "state": rec.state,
        "created_at": rec.created_at,
        "evidence_updated_at": rec.evidence_updated_at,
        "settlement_result": rec.settlement_result,
        "realized_profit_per_contract": rec.realized_profit_per_contract,
        "provenance": rec.provenance,
    }


@router.get("")
def list_opportunities(
    horizon: str = Query("24h", pattern="^(24h|7d|30d)$"),
    platform: str | None = Query(None),
    category: str | None = Query(None),
    side: Side | None = Query(None),
    min_liquidity: Decimal | None = Query(None),
    min_edge: Decimal | None = Query(None),
    min_confidence: Decimal | None = Query(None),
    include_inactive: bool = Query(
        False,
        description=(
            "Include recommendations whose edge has since disappeared. They are "
            "retained and labelled rather than deleted, so the list cannot be made "
            "to look better than it was."
        ),
    ),
    limit: int = Query(10, ge=1, le=100),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Ranked value opportunities for one resolution horizon.

    Only markets whose fair probability rests on an *independent* prior can appear
    here; see ``/opportunities/watchlist`` for those that do not qualify.
    """
    latest_batch = db.scalar(
        apply_provenance(
            select(Recommendation.batch_id).order_by(Recommendation.created_at.desc()),
            Recommendation.provenance,
            mode,
        ).limit(1)
    )
    if not latest_batch:
        return envelope(
            [], mode,
            horizon=horizon,
            batch_id=None,
            empty_reason=(
                "No recommendations have been published yet. Run `make ingest` then "
                "`make rank`. An empty list is also a legitimate outcome: the ranking "
                "gate requires a conservative net EV above the configured threshold "
                "against an independent prior, and efficiently-priced markets "
                "routinely produce nothing."
            ),
        )

    stmt = (
        select(Recommendation)
        .where(
            Recommendation.batch_id == latest_batch,
            Recommendation.horizon == horizon,
        )
        .order_by(Recommendation.rank)
    )
    stmt = apply_provenance(stmt, Recommendation.provenance, mode)
    if not include_inactive:
        stmt = stmt.where(
            Recommendation.state.in_(
                (
                    RecommendationState.STILL_ACTIONABLE.value,
                    RecommendationState.EDGE_REDUCED.value,
                )
            )
        )
    if side is not None:
        stmt = stmt.where(Recommendation.side == side.value)
    if min_liquidity is not None:
        stmt = stmt.where(Recommendation.liquidity_usd >= min_liquidity)
    if min_edge is not None:
        stmt = stmt.where(Recommendation.conservative_net_ev >= min_edge)
    if min_confidence is not None:
        stmt = stmt.where(Recommendation.model_confidence >= min_confidence)

    recs = list(db.scalars(stmt))
    market_ids = {r.market_id for r in recs}
    markets = {
        m.id: m for m in db.scalars(select(Market).where(Market.id.in_(market_ids)))
    } if market_ids else {}

    rows = []
    for rec in recs:
        market = markets.get(rec.market_id)
        if platform and (not market or market.platform != platform):
            continue
        if category and (not market or market.category != category):
            continue
        rows.append(_serialize(rec, market))
        if len(rows) >= limit:
            break

    return envelope(
        rows, mode,
        horizon=horizon,
        batch_id=latest_batch,
        count=len(rows),
        generated_at=recs[0].created_at if recs else None,
    )


@router.get("/summary")
def summary(
    include_inactive: bool = Query(False),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Counts per horizon for the latest batch, for the home page tabs.

    Applies the *same* state filter as the listing endpoint. Counting all
    recommendations while the list showed only actionable ones produced tabs
    advertising opportunities next to an empty page - the tab count must mean the
    same thing as the list beneath it.
    """
    latest_batch = db.scalar(
        apply_provenance(
            select(Recommendation.batch_id).order_by(Recommendation.created_at.desc()),
            Recommendation.provenance,
            mode,
        ).limit(1)
    )
    counts = {h: 0 for h in HORIZONS}
    if latest_batch:
        stmt = apply_provenance(
            select(Recommendation).where(Recommendation.batch_id == latest_batch),
            Recommendation.provenance,
            mode,
        )
        if not include_inactive:
            stmt = stmt.where(
                Recommendation.state.in_(
                    (
                        RecommendationState.STILL_ACTIONABLE.value,
                        RecommendationState.EDGE_REDUCED.value,
                    )
                )
            )
        for rec in db.scalars(stmt):
            counts[rec.horizon] = counts.get(rec.horizon, 0) + 1
    return envelope(counts, mode, batch_id=latest_batch)


@router.get("/watchlist")
def watchlist(
    horizon: str = Query("24h", pattern="^(24h|7d|30d)$"),
    limit: int = Query(25, ge=1, le=100),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Markets scored but **not** eligible to be recommended.

    These are markets where the only information the model had was the market's own
    price. Their "fair probability" is a restatement of that price, so any apparent
    edge would be circular. They are surfaced explicitly - rather than hidden - so the
    coverage gap is visible, but they are never presented as opportunities.
    """
    stmt = (
        select(ModelPrediction)
        .where(ModelPrediction.has_independent_prior.is_(False))
        .order_by(ModelPrediction.created_at.desc())
        .limit(limit * 4)
    )
    stmt = apply_provenance(stmt, ModelPrediction.provenance, mode)
    predictions = list(db.scalars(stmt))

    market_ids = {p.market_id for p in predictions}
    markets = {
        m.id: m for m in db.scalars(select(Market).where(Market.id.in_(market_ids)))
    } if market_ids else {}

    seen: set[int] = set()
    rows = []
    for prediction in predictions:
        if prediction.market_id in seen:
            continue
        market = markets.get(prediction.market_id)
        if market is None:
            continue
        seen.add(prediction.market_id)
        rows.append(
            {
                "market_id": market.id,
                "platform": market.platform,
                "platform_market_id": market.platform_market_id,
                "title": market.title,
                "category": market.category,
                "best_yes_ask": market.best_yes_ask,
                "best_no_ask": market.best_no_ask,
                "spread": market.spread,
                "liquidity_usd": market.orderbook_depth_usd,
                "volume_24h": market.volume_24h,
                "expected_resolution_time": market.expected_resolution_time,
                "market_implied_probability": prediction.market_implied_probability,
                "model_confidence": prediction.model_confidence,
                "reason": "no independent prior - fair value is the market's own price",
            }
        )
        if len(rows) >= limit:
            break

    return envelope(
        rows, mode,
        horizon=horizon,
        count=len(rows),
        explanation=(
            "These markets are NOT opportunities. The model had no information source "
            "independent of the market itself, so no edge can be demonstrated against "
            "its price."
        ),
    )


@router.get("/disagreements")
def disagreements(
    horizon: str = Query("24h", pattern="^(24h|7d|30d)$"),
    min_divergence: Decimal = Query(
        Decimal("0.03"),
        description="Minimum |model - market| in probability terms.",
    ),
    limit: int = Query(20, ge=1, le=100),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Markets where an **independent** model most disagrees with the market price.

    This is the platform's research output when nothing clears the recommendation
    gate, which on efficiently-priced venues is most of the time. Every row here has
    a probability estimate that does *not* come from the market's own price, so the
    divergence is real information rather than an artefact - but none of them has
    cleared the conservative bound after costs, so **none is a recommendation**.

    Ranked by divergence, not by expected value, because the point is to surface
    where the model and the market see the world differently and let a human judge
    which one is wrong. A large divergence usually means the model is missing
    something the market knows.
    """
    now = utcnow()
    predictions = list(
        db.scalars(
            apply_provenance(
                select(ModelPrediction)
                .where(ModelPrediction.has_independent_prior.is_(True))
                .order_by(ModelPrediction.created_at.desc())
                .limit(3000),
                ModelPrediction.provenance,
                mode,
            )
        )
    )

    market_ids = {p.market_id for p in predictions}
    markets = {
        m.id: m for m in db.scalars(select(Market).where(Market.id.in_(market_ids)))
    } if market_ids else {}

    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
    for prediction in predictions:
        if prediction.market_id in seen:
            continue
        market = markets.get(prediction.market_id)
        if market is None or market.status != MarketStatus.OPEN.value:
            continue
        # An event that has already happened is not a disagreement - the market knows
        # the outcome and the model is still estimating. Those rows dominated the list
        # with 99c-vs-4c "divergences" that are entirely the model's error.
        occurrence = ensure_utc(market.event_occurrence_time)
        if occurrence is not None and occurrence <= now:
            continue
        implied = prediction.market_implied_probability
        if implied is None:
            continue
        if horizons_for(market.expected_resolution_time, now=now)[:1] != (horizon,):
            continue
        seen.add(prediction.market_id)

        divergence = prediction.fair_probability_mean - implied
        if abs(divergence) < min_divergence:
            continue

        rows.append(
            {
                "market_id": market.id,
                "platform": market.platform,
                "platform_market_id": market.platform_market_id,
                "title": market.title,
                "subtitle": market.subtitle,
                "category": market.category,
                "market_implied_probability": implied,
                "model_probability": prediction.fair_probability_mean,
                "model_low": prediction.fair_probability_low,
                "model_high": prediction.fair_probability_high,
                "divergence": divergence,
                "abs_divergence": abs(divergence),
                "direction": "model_higher" if divergence > 0 else "model_lower",
                "model_confidence": prediction.model_confidence,
                "best_yes_ask": market.best_yes_ask,
                "best_no_ask": market.best_no_ask,
                "spread": market.spread,
                "liquidity_usd": market.orderbook_depth_usd,
                "volume_24h": market.volume_24h,
                "expected_resolution_time": market.expected_resolution_time,
                "explanation": prediction.explanation,
                "model_version": prediction.model_version,
            }
        )

    rows.sort(key=lambda r: r["abs_divergence"], reverse=True)
    rows = rows[:limit]

    return envelope(
        rows, mode,
        horizon=horizon,
        count=len(rows),
        is_recommendation_list=False,
        explanation=(
            "These are NOT recommendations. Each row has an independent model estimate "
            "that differs from the market price, but none cleared the conservative net "
            "EV gate after fees, slippage and capital cost. A large divergence more "
            "often means the model is missing information than that the market is wrong."
        ),
    )
