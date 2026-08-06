"""One recommendation, from price to settlement, as an auditable walkthrough.

Everything here is **read** from frozen records - the immutable
``recommendation_snapshot`` written at publication time, plus the linked
recommendation, market and prediction rows. Nothing is recomputed.

That distinction is the point of the page. Re-deriving the numbers now would produce
today's answer using today's model, which is precisely the look-ahead the platform is
built to avoid. If the walkthrough disagreed with the frozen record, the frozen
record would be right.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.enums import Side
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div

from pmvl_markets.db_models import (
    Market,
    ModelPrediction,
    Recommendation,
    RecommendationSnapshot,
)

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope

router = APIRouter(prefix="/case-study", tags=["case-study"])

#: Cost components in the order they are applied, with labels the page renders.
COST_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("entry_price", "Executable entry (VWAP)"),
    ("platform_fee", "Platform fee"),
    ("fee_rounding", "Fee rounding"),
    ("estimated_slippage", "Estimated slippage"),
    ("transfer_cost", "Transfer cost"),
    ("capital_cost", "Capital cost"),
    ("execution_risk_penalty", "Execution-risk penalty"),
)


def _brier(probability: Decimal | None, outcome: Decimal) -> Decimal | None:
    """Squared error of a single forecast. Lower is better."""
    if probability is None:
        return None
    diff = probability - outcome
    return quantize_usd(diff * diff)


def _serialize(
    snapshot: RecommendationSnapshot,
    recommendation: Recommendation | None,
    market: Market | None,
    prediction: ModelPrediction | None,
) -> dict[str, Any]:
    side = Side(snapshot.side)
    book = snapshot.orderbook_snapshot or {}
    evidence = snapshot.evidence_snapshot or {}

    # --- what the model said, expressed for the side actually recommended -----
    #
    # For a NO recommendation the conservative case is the one where YES turns out
    # MORE likely than estimated, so the bound is 1 - fair_probability_high. Reusing
    # the YES lower bound would be the optimistic case and would overstate the edge.
    if side == Side.YES:
        win_probability = snapshot.fair_probability
        conservative = snapshot.fair_probability_low
        bound_label = "lower bound (5th percentile)"
    else:
        win_probability = ONE - snapshot.fair_probability
        conservative = ONE - snapshot.fair_probability_high
        bound_label = "mirrored bound (1 - upper bound)"

    market_implied = evidence.get("market_implied_probability")
    market_implied_dec = D(market_implied) if market_implied is not None else None

    # --- the cost stack, in application order --------------------------------
    breakdown = (recommendation.cost_breakdown or {}) if recommendation else {}
    costs: list[dict[str, Any]] = []
    for key, label in COST_COMPONENTS:
        raw = breakdown.get(key)
        costs.append(
            {
                "key": key,
                "label": label,
                # A component that genuinely costs nothing is 0, not missing. The page
                # renders $0.00 for these; only a truly absent component is null.
                "amount": D(raw) if raw is not None else (
                    ZERO if key in breakdown or key == "entry_price" else None
                ),
                "applicable": raw is not None or key == "entry_price",
            }
        )

    # --- settlement --------------------------------------------------------
    settled = snapshot.final_result is not None
    yes_payout = None
    if snapshot.final_result == "yes":
        yes_payout = ONE
    elif snapshot.final_result == "no":
        yes_payout = ZERO
    elif snapshot.final_result == "fifty_fifty":
        yes_payout = D("0.5")

    outcome_for_side = None
    if yes_payout is not None:
        outcome_for_side = yes_payout if side == Side.YES else ONE - yes_payout

    realized = snapshot.realized_profit_per_contract
    won = bool(realized is not None and realized > 0)

    # Brier is scored on the YES event so model and market are directly comparable.
    model_brier = _brier(snapshot.fair_probability, yes_payout) if yes_payout is not None else None
    market_brier = (
        _brier(market_implied_dec, yes_payout)
        if yes_payout is not None and market_implied_dec is not None
        else None
    )
    forecast_beat_market = (
        None if model_brier is None or market_brier is None else model_brier < market_brier
    )

    return {
        # ---------------------------------------------------- 1. the market
        "market": {
            "snapshot_id": snapshot.id,
            "market_id": snapshot.market_id,
            "title": snapshot.market_title,
            "platform": snapshot.platform,
            "platform_market_id": snapshot.platform_market_id,
            "category": market.category if market else None,
            "side": snapshot.side,
            "horizon": snapshot.horizon,
            "rank": snapshot.rank,
            "published_at": snapshot.recommendation_created_at,
            "expected_resolution_time": snapshot.expected_resolution_time,
            "settled_at": snapshot.settled_at,
            "final_result": snapshot.final_result,
            "settlement_rules": (market.settlement_rules_raw or "") if market else "",
            "settlement_source": (market.settlement_source or "") if market else "",
            "provenance": snapshot.provenance,
        },
        # -------------------------------------------- 2. what could be bought
        "execution": {
            "best_ask": D(book["best_yes_ask"]) if book.get("best_yes_ask") else None,
            "best_no_ask": D(book["best_no_ask"]) if book.get("best_no_ask") else None,
            "entry_vwap": snapshot.entry_price_at_recommendation,
            "reference_size": snapshot.executable_size,
            "spread": recommendation.spread if recommendation else None,
            "depth_usd": recommendation.liquidity_usd if recommendation else None,
            "quote_observed_at": book.get("observed_at"),
            "levels": book.get("yes_asks" if side == Side.YES else "no_asks", []),
        },
        # ------------------------------------------- 3. probability estimate
        "probability": {
            "market_implied": market_implied_dec,
            "fair_probability_yes": snapshot.fair_probability,
            "win_probability_for_side": win_probability,
            "conservative_bound": conservative,
            "conservative_bound_label": bound_label,
            "interval_low": snapshot.fair_probability_low,
            "interval_high": snapshot.fair_probability_high,
            "model_confidence": snapshot.model_confidence,
            "model_version": snapshot.model_version,
            "has_independent_prior": (
                prediction.has_independent_prior if prediction else
                evidence.get("has_independent_prior")
            ),
            "components": (prediction.components or []) if prediction else [],
            "explanation": evidence.get("explanation", ""),
            "evidence_items": evidence.get("items", []),
        },
        # -------------------------------------------------- 4. the cost stack
        "costs": {
            "components": costs,
            "total_cost_per_contract": snapshot.total_cost_at_recommendation,
            "cost_above_entry": quantize_usd(
                snapshot.total_cost_at_recommendation
                - snapshot.entry_price_at_recommendation
            ),
        },
        # ---------------------------------------------------- 5. the decision
        "decision": {
            "raw_edge": quantize_usd(
                win_probability - snapshot.entry_price_at_recommendation
            ),
            "net_edge": snapshot.expected_value,
            "conservative_net_ev": snapshot.conservative_net_ev,
            "net_roi": recommendation.net_roi if recommendation else None,
            "expected_profit_per_100_usd": (
                recommendation.expected_profit_per_100_usd if recommendation else None
            ),
            "position_cap": (
                recommendation.recommended_position_cap if recommendation else None
            ),
            "risk_flags": snapshot.risk_flags or [],
            "state": recommendation.state if recommendation else None,
            "qualified": snapshot.conservative_net_ev > 0,
            "verdict": (
                "Qualified as actionable"
                if snapshot.conservative_net_ev > 0
                else "Rejected: no positive edge once costs are deducted"
            ),
        },
        # ------------------------------------------------- 6. what happened
        "outcome": {
            "settled": settled,
            "final_result": snapshot.final_result,
            "yes_payout": yes_payout,
            "payout_for_side": outcome_for_side,
            "realized_profit_per_contract": realized,
            "realized_profit_at_100_usd": snapshot.realized_profit_at_100_usd,
            "trade_won": won if settled else None,
            "model_brier": model_brier,
            "market_brier": market_brier,
            "forecast_beat_market": forecast_beat_market,
            # The two questions the page must keep separate.
            "summary": _outcome_summary(won, forecast_beat_market) if settled else None,
        },
    }


def _outcome_summary(won: bool, forecast_beat_market: bool | None) -> str:
    """State the trade result and the forecast quality as separate facts."""
    if forecast_beat_market is None:
        return (
            "This trade made money." if won else "This trade lost money."
        ) + " No market probability was recorded, so forecast accuracy cannot be compared."
    if won and forecast_beat_market:
        return (
            "This trade made money AND the model's probability was closer to the "
            "outcome than the market's."
        )
    if won and not forecast_beat_market:
        return (
            "This trade made money, but the market's probability was closer to the "
            "outcome than the model's. Profit here came from the price, not from a "
            "better forecast."
        )
    if not won and forecast_beat_market:
        return (
            "This trade lost money, but the model's probability was closer to the "
            "outcome than the market's. A single loss does not invalidate a "
            "calibrated forecast."
        )
    return (
        "This trade lost money AND the market's probability was closer to the "
        "outcome. On this call the model was simply worse."
    )


def _pick(
    db: Session, mode: DataMode, result: str
) -> RecommendationSnapshot | None:
    """Choose the most illustrative settled snapshot for ``result``.

    Prefers rows that exercise the whole walkthrough: an itemised cost breakdown, a
    recorded market-implied probability, and a decisive outcome.
    """
    stmt = (
        select(RecommendationSnapshot)
        .where(RecommendationSnapshot.final_result.is_not(None))
        .order_by(RecommendationSnapshot.recommendation_created_at.desc())
    )
    stmt = apply_provenance(stmt, RecommendationSnapshot.provenance, mode)
    candidates = list(db.scalars(stmt.limit(400)))

    def usable(snapshot: RecommendationSnapshot) -> bool:
        evidence = snapshot.evidence_snapshot or {}
        return bool(
            (snapshot.orderbook_snapshot or {}).get("best_yes_ask")
            and evidence.get("market_implied_probability") is not None
            and snapshot.realized_profit_per_contract is not None
        )

    pool = [s for s in candidates if usable(s)] or candidates
    if result == "winner":
        pool = [s for s in pool if (s.realized_profit_per_contract or ZERO) > 0]
    elif result == "loser":
        pool = [s for s in pool if (s.realized_profit_per_contract or ZERO) <= 0]

    if not pool:
        return None
    # Largest absolute outcome reads most clearly as an illustration.
    return max(pool, key=lambda s: abs(s.realized_profit_per_contract or ZERO))


@router.get("")
def case_study(
    result: str = Query(
        "featured",
        pattern="^(featured|winner|loser)$",
        description="Which settled example to walk through.",
    ),
    snapshot_id: int | None = Query(None, description="Pin a specific snapshot."),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    """A single published recommendation, traced from quote to settlement."""
    if snapshot_id is not None:
        snapshot = db.get(RecommendationSnapshot, snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        allowed = mode.provenances
        if allowed is not None and snapshot.provenance not in allowed:
            raise HTTPException(
                status_code=404, detail="snapshot not available in this data mode"
            )
    else:
        snapshot = _pick(db, mode, "featured" if result == "featured" else result)

    if snapshot is None:
        return envelope(
            None, mode,
            result=result,
            empty_reason=(
                "No settled recommendation is available to walk through yet. Live "
                "recommendations must reach their resolution date first; use "
                "?mode=demo to see the walkthrough on the synthetic dataset."
            ),
        )

    recommendation = (
        db.get(Recommendation, snapshot.recommendation_id)
        if snapshot.recommendation_id
        else None
    )
    market = db.get(Market, snapshot.market_id)
    prediction = (
        db.get(ModelPrediction, recommendation.prediction_id)
        if recommendation and recommendation.prediction_id
        else None
    )

    # Which alternatives the selector can offer, so the page never links to nothing.
    available = {
        key: (_pick(db, mode, key).id if _pick(db, mode, key) else None)
        for key in ("winner", "loser")
    }

    return envelope(
        _serialize(snapshot, recommendation, market, prediction),
        mode,
        result=result,
        snapshot_id=snapshot.id,
        available=available,
        provenance=snapshot.provenance,
        audit_note=(
            "Every figure on this page is read from the record frozen when the "
            "recommendation was published. Nothing is recomputed: re-deriving it now "
            "would use today's model on a past decision, which is the look-ahead bias "
            "the backtest is designed to exclude."
        ),
    )
