"""Backtest results, calibration and the public track record."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmvl_markets.db_models import BacktestRun, BacktestTrade, RecommendationSnapshot

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope

router = APIRouter(tags=["performance"])

DATA_QUALITY_MEANING = {
    "orderbook": "Fills derived from a frozen depth ladder captured at publication.",
    "quote": (
        "Fills derived from top-of-book only. The price is real but the achievable "
        "size at that price is not established."
    ),
    "candle": (
        "Fills derived from OHLC bars. A candle is NOT an executable quote and this "
        "is not an executable-price backtest."
    ),
    "unknown": "No book or quote was frozen; achievable execution is not established.",
}


@router.get("/backtest")
def backtest(
    strategy: str | None = Query(None),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Latest run per strategy."""
    latest = (
        select(BacktestRun.strategy, func.max(BacktestRun.created_at).label("latest"))
        .group_by(BacktestRun.strategy)
        .subquery()
    )
    stmt = (
        select(BacktestRun)
        .join(
            latest,
            (BacktestRun.strategy == latest.c.strategy)
            & (BacktestRun.created_at == latest.c.latest),
        )
        .order_by(BacktestRun.strategy)
    )
    stmt = apply_provenance(stmt, BacktestRun.provenance, mode)
    if strategy:
        stmt = stmt.where(BacktestRun.strategy == strategy)

    runs = [
        {
            "run_id": r.run_id,
            "strategy": r.strategy,
            "description": (r.config or {}).get("description", ""),
            "model_version": r.model_version,
            "window_start": r.window_start,
            "window_end": r.window_end,
            "walk_forward": r.walk_forward,
            "data_quality": r.data_quality,
            "data_quality_meaning": DATA_QUALITY_MEANING.get(r.data_quality, ""),
            "n_recommendations": r.n_recommendations,
            "n_settled": r.n_settled,
            "metrics": r.metrics or {},
            "by_slice": (r.config or {}).get("by_slice", {}),
            "notes": r.notes,
            "created_at": r.created_at,
            "provenance": r.provenance,
        }
        for r in db.scalars(stmt)
    ]

    return envelope(
        runs, mode,
        count=len(runs),
        empty_reason=(
            None if runs else
            "No backtest has produced results yet. The backtest reads only settled "
            "recommendation snapshots, so published recommendations must first reach "
            "their resolution date. Use ?mode=demo to inspect the surface with "
            "synthetic data."
        ),
        methodology_note=(
            "Walk-forward by construction: the engine reads only immutable snapshots "
            "frozen at publication time. It never re-prices an entry, never re-runs "
            "the model, and applies selection within each publication day."
        ),
    )


@router.get("/backtest/{run_id}/trades")
def backtest_trades(
    run_id: str,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    run = db.scalar(
        apply_provenance(
            select(BacktestRun).where(BacktestRun.run_id == run_id),
            BacktestRun.provenance,
            mode,
        )
    )
    if run is None:
        raise HTTPException(
            status_code=404, detail="backtest run not available in this data mode"
        )

    rows = [
        {
            "market_title": t.market_title,
            "platform": t.platform,
            "side": t.side,
            "entered_at": t.entered_at,
            "fill_price": t.fill_price,
            "fees": t.fees,
            "contracts": t.contracts,
            "stake": t.stake,
            "predicted_probability": t.predicted_probability,
            "market_probability": t.market_probability,
            "outcome": t.outcome,
            "payout": t.payout,
            "pnl": t.pnl,
            "settled_at": t.settled_at,
            "data_quality": t.data_quality,
        }
        for t in db.scalars(
            select(BacktestTrade)
            .where(BacktestTrade.run_id == run_id)
            .order_by(BacktestTrade.entered_at)
            .limit(limit)
        )
    ]
    return envelope(rows, mode, run_id=run_id, count=len(rows))


@router.get("/track-record")
def track_record(
    horizon: str | None = Query(None, pattern="^(24h|7d|30d)$"),
    settled_only: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """Every recommendation ever published, exactly as published.

    This endpoint deliberately returns losers alongside winners and provides no
    filter that could hide them. ``settled_only`` narrows to graded rows; it does not
    filter by outcome.
    """
    stmt = select(RecommendationSnapshot).order_by(
        RecommendationSnapshot.recommendation_created_at.desc(),
        RecommendationSnapshot.rank,
    )
    stmt = apply_provenance(stmt, RecommendationSnapshot.provenance, mode)
    if horizon:
        stmt = stmt.where(RecommendationSnapshot.horizon == horizon)
    if settled_only:
        stmt = stmt.where(RecommendationSnapshot.final_result.is_not(None))

    total = db.scalar(
        apply_provenance(
            select(func.count()).select_from(RecommendationSnapshot),
            RecommendationSnapshot.provenance,
            mode,
        )
    )

    rows = [
        {
            "id": s.id,
            "snapshot_date": s.snapshot_date,
            "recommendation_created_at": s.recommendation_created_at,
            "market_id": s.market_id,
            "platform": s.platform,
            "platform_market_id": s.platform_market_id,
            "market_title": s.market_title,
            "horizon": s.horizon,
            "rank": s.rank,
            "side": s.side,
            "entry_price_at_recommendation": s.entry_price_at_recommendation,
            "total_cost_at_recommendation": s.total_cost_at_recommendation,
            "executable_size": s.executable_size,
            "fair_probability": s.fair_probability,
            "confidence_interval": [s.fair_probability_low, s.fair_probability_high],
            "expected_value": s.expected_value,
            "conservative_net_ev": s.conservative_net_ev,
            "model_confidence": s.model_confidence,
            "model_version": s.model_version,
            "expected_resolution_time": s.expected_resolution_time,
            "evidence_snapshot": s.evidence_snapshot or {},
            "orderbook_snapshot": s.orderbook_snapshot or {},
            "risk_flags": s.risk_flags or [],
            "final_result": s.final_result,
            "realized_profit_per_contract": s.realized_profit_per_contract,
            "realized_profit_at_100_usd": s.realized_profit_at_100_usd,
            "settled_at": s.settled_at,
            "provenance": s.provenance,
        }
        for s in db.scalars(stmt.offset(offset).limit(limit))
    ]

    settled = [r for r in rows if r["final_result"]]
    wins = sum(
        1 for r in settled
        if r["realized_profit_per_contract"] and r["realized_profit_per_contract"] > 0
    )

    return envelope(
        rows, mode,
        total=total,
        count=len(rows),
        offset=offset,
        settled_in_page=len(settled),
        wins_in_page=wins,
        losses_in_page=len(settled) - wins,
        integrity_note=(
            "Snapshots are append-only. Entry price, fair probability, confidence "
            "interval and evidence are frozen at publication and are never rewritten. "
            "Losing recommendations are shown and cannot be filtered out."
        ),
    )
