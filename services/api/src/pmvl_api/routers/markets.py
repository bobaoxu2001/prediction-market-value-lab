"""Market browser and market detail."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from pmvl_shared.timeutil import horizons_for, utcnow

from pmvl_markets.db_models import (
    EvidenceItem,
    Market,
    MarketMatch,
    MarketRule,
    ModelPrediction,
    OrderbookLevel,
    OrderbookSnapshot,
    PriceSnapshot,
    Recommendation,
    Settlement,
)

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope

router = APIRouter(prefix="/markets", tags=["markets"])


def _market_row(market: Market) -> dict[str, Any]:
    horizons = horizons_for(market.expected_resolution_time)
    return {
        "id": market.id,
        "platform": market.platform,
        "platform_market_id": market.platform_market_id,
        "title": market.title,
        "subtitle": market.subtitle,
        "category": market.category,
        "status": market.status,
        "accepting_orders": market.accepting_orders,
        "best_yes_bid": market.best_yes_bid,
        "best_yes_ask": market.best_yes_ask,
        "best_no_bid": market.best_no_bid,
        "best_no_ask": market.best_no_ask,
        "spread": market.spread,
        "orderbook_depth_usd": market.orderbook_depth_usd,
        "volume_24h": market.volume_24h,
        "total_volume": market.total_volume,
        "open_interest": market.open_interest,
        "last_trade_price": market.last_trade_price,
        "tick_size": market.tick_size,
        "fee_rate": market.fee_rate,
        "close_time": market.close_time,
        "expected_resolution_time": market.expected_resolution_time,
        "horizon": horizons[0] if horizons else None,
        "quote_observed_at": market.quote_observed_at,
        "result": market.result,
        "provenance": market.provenance,
    }


@router.get("")
def list_markets(
    q: str | None = Query(None, description="Substring search on title"),
    platform: str | None = Query(None),
    category: str | None = Query(None),
    horizon: str | None = Query(None, pattern="^(24h|7d|30d)$"),
    status: str | None = Query(None),
    min_volume: Decimal | None = Query(None),
    has_orderbook: bool = Query(False),
    sort: str = Query("volume", pattern="^(volume|spread|resolution|liquidity)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    stmt = select(Market)
    stmt = apply_provenance(stmt, Market.provenance, mode)

    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Market.title).like(pattern),
                func.lower(Market.normalized_title).like(pattern),
            )
        )
    if platform:
        stmt = stmt.where(Market.platform == platform)
    if category:
        stmt = stmt.where(Market.category == category)
    if status:
        stmt = stmt.where(Market.status == status)
    if min_volume is not None:
        stmt = stmt.where(Market.volume_24h >= min_volume)
    if has_orderbook:
        stmt = stmt.where(Market.orderbook_depth_usd.is_not(None))

    order = {
        "volume": Market.volume_24h.desc(),
        "spread": Market.spread.asc(),
        "resolution": Market.expected_resolution_time.asc(),
        "liquidity": Market.orderbook_depth_usd.desc(),
    }[sort]
    stmt = stmt.order_by(order)

    total = db.scalar(
        apply_provenance(select(func.count()).select_from(Market), Market.provenance, mode)
    )

    # Horizon is derived from the current time rather than stored, so it is filtered
    # after the query. Over-fetching keeps the page full after that filter.
    rows = list(db.scalars(stmt.offset(offset).limit(limit * 3 if horizon else limit)))
    out = []
    for market in rows:
        row = _market_row(market)
        if horizon and row["horizon"] != horizon:
            continue
        out.append(row)
        if len(out) >= limit:
            break

    return envelope(out, mode, total=total, count=len(out), offset=offset, limit=limit)


@router.get("/categories")
def categories(db: Session = DbDep, mode: DataMode = ModeDep) -> dict[str, Any]:
    stmt = apply_provenance(
        select(Market.category, func.count(Market.id)).group_by(Market.category),
        Market.provenance,
        mode,
    )
    return envelope(
        [{"category": c, "count": n} for c, n in db.execute(stmt)], mode
    )


@router.get("/{market_id}")
def market_detail(
    market_id: int, db: Session = DbDep, mode: DataMode = ModeDep
) -> dict[str, Any]:
    market = db.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    rule = db.scalar(select(MarketRule).where(MarketRule.market_id == market_id))

    snapshot = db.scalar(
        select(OrderbookSnapshot)
        .where(OrderbookSnapshot.market_id == market_id)
        .order_by(OrderbookSnapshot.observed_at.desc())
        .limit(1)
    )
    book: dict[str, Any] = {}
    if snapshot is not None:
        levels = list(
            db.scalars(
                select(OrderbookLevel)
                .where(OrderbookLevel.snapshot_id == snapshot.id)
                .order_by(OrderbookLevel.level_index)
            )
        )
        book = {
            "observed_at": snapshot.observed_at,
            "source_timestamp": snapshot.source_timestamp,
            "yes_depth_usd": snapshot.yes_depth_usd,
            "no_depth_usd": snapshot.no_depth_usd,
            "yes_asks": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "yes" and l.is_ask
            ],
            "yes_bids": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "yes" and not l.is_ask
            ],
            "no_asks": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "no" and l.is_ask
            ],
            "no_bids": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "no" and not l.is_ask
            ],
        }

    price_history = [
        {
            "observed_at": p.observed_at,
            "yes_bid": p.yes_bid,
            "yes_ask": p.yes_ask,
            "mid": p.mid,
            "last_trade_price": p.last_trade_price,
        }
        for p in db.scalars(
            select(PriceSnapshot)
            .where(PriceSnapshot.market_id == market_id)
            .order_by(PriceSnapshot.observed_at)
            .limit(500)
        )
    ]

    predictions = [
        {
            "created_at": p.created_at,
            "fair_probability_mean": p.fair_probability_mean,
            "fair_probability_low": p.fair_probability_low,
            "fair_probability_high": p.fair_probability_high,
            "model_confidence": p.model_confidence,
            "market_implied_probability": p.market_implied_probability,
            "has_independent_prior": p.has_independent_prior,
            "explanation": p.explanation,
            "components": p.components or [],
            "model_version": p.model_version,
            "evidence_quality": p.evidence_quality,
            "data_freshness_seconds": p.data_freshness_seconds,
        }
        for p in db.scalars(
            select(ModelPrediction)
            .where(ModelPrediction.market_id == market_id)
            .order_by(ModelPrediction.created_at.desc())
            .limit(100)
        )
    ]

    evidence = [
        {
            "source_name": e.source_name,
            "source_url": e.source_url,
            "title": e.title,
            "summary": e.summary,
            "stance": e.stance,
            "published_at": e.published_at,
            "event_time": e.event_time,
            "is_novel": e.is_novel,
            "source_quality": e.source_quality,
        }
        for e in db.scalars(
            select(EvidenceItem)
            .where(EvidenceItem.market_id == market_id)
            .order_by(EvidenceItem.published_at.desc())
            .limit(50)
        )
    ]

    matches = []
    for match in db.scalars(
        select(MarketMatch).where(
            or_(MarketMatch.market_a_id == market_id, MarketMatch.market_b_id == market_id)
        )
    ):
        other_id = (
            match.market_b_id if match.market_a_id == market_id else match.market_a_id
        )
        other = db.get(Market, other_id)
        matches.append(
            {
                "other_market_id": other_id,
                "other_platform": other.platform if other else None,
                "other_title": other.title if other else None,
                "other_best_yes_ask": other.best_yes_ask if other else None,
                "match_confidence": match.match_confidence,
                "rule_compatibility": match.rule_compatibility,
                "time_compatibility": match.time_compatibility,
                "source_compatibility": match.source_compatibility,
                "polarity_inverted": match.polarity_inverted,
                "mismatch_reasons": match.mismatch_reasons or [],
            }
        )

    recommendations = [
        {
            "created_at": r.created_at,
            "horizon": r.horizon,
            "rank": r.rank,
            "side": r.side,
            "entry_price": r.entry_price,
            "current_price": r.current_price,
            "total_cost_per_contract": r.total_cost_per_contract,
            "net_ev_per_contract": r.net_ev_per_contract,
            "conservative_net_ev": r.conservative_net_ev,
            "state": r.state,
            "settlement_result": r.settlement_result,
            "realized_profit_per_contract": r.realized_profit_per_contract,
        }
        for r in db.scalars(
            select(Recommendation)
            .where(Recommendation.market_id == market_id)
            .order_by(Recommendation.created_at.desc())
            .limit(50)
        )
    ]

    settlement = db.scalar(select(Settlement).where(Settlement.market_id == market_id))

    return envelope(
        {
            "market": {
                **_market_row(market),
                "description": market.description,
                "settlement_source": market.settlement_source,
                "settlement_rules_raw": market.settlement_rules_raw,
                "settlement_rules_normalized": market.settlement_rules_normalized,
                "resolution_hash": market.resolution_hash,
                "open_time": market.open_time,
                "event_occurrence_time": market.event_occurrence_time,
                "actual_settlement_time": market.actual_settlement_time,
                "strike_type": market.strike_type,
                "floor_strike": market.floor_strike,
                "cap_strike": market.cap_strike,
                "min_order_size": market.min_order_size,
                "fee_type": market.fee_type,
                "yes_token_id": market.yes_token_id,
                "no_token_id": market.no_token_id,
                "condition_id": market.condition_id,
            },
            "rule": {
                "threshold_semantics": rule.threshold_semantics,
                "threshold_value": rule.threshold_value,
                "comparator": rule.comparator,
                "cutoff_time": rule.cutoff_time,
                "includes_overtime": rule.includes_overtime,
                "uses_revised_data": rule.uses_revised_data,
                "entities": rule.entities or [],
                "normalized_terms": rule.normalized_terms or {},
            } if rule else None,
            "orderbook": book,
            "price_history": price_history,
            "predictions": predictions,
            "evidence": evidence,
            "cross_platform_matches": matches,
            "recommendations": recommendations,
            "settlement": {
                "result": settlement.result,
                "yes_payout": settlement.yes_payout,
                "settled_at": settlement.settled_at,
                "settlement_source": settlement.settlement_source,
                "disputed": settlement.disputed,
            } if settlement else None,
        },
        mode,
    )
