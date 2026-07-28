"""Arbitrage scan results, grouped by honesty label."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.enums import (
    ArbitrageKind,
    ArbitrageLabel,
    classify_arbitrage_label,
)

from pmvl_markets.db_models import ArbitrageOpportunity

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope

router = APIRouter(prefix="/arbitrage", tags=["arbitrage"])

#: Plain-language meaning of each label, returned with every response so the UI never
#: has to invent an explanation for what a classification means.
LABEL_MEANINGS: dict[str, str] = {
    ArbitrageLabel.EXECUTABLE.value: (
        "Every leg has sufficient depth right now, all fees and slippage are deducted, "
        "the settlement conditions are an exact match, and the net profit is still "
        "strictly positive. This is the only label that claims a locked-in result, and "
        "it still assumes both legs fill."
    ),
    ArbitrageLabel.THEORETICAL.value: (
        "A price edge exists but at least one execution precondition is unmet."
    ),
    ArbitrageLabel.RULE_MISMATCH_RISK.value: (
        "The two markets are close but their settlement rules are not an exact match. "
        "If they resolve differently, both legs can lose. NOT guaranteed."
    ),
    ArbitrageLabel.EXECUTION_RISK.value: (
        "The legs cannot be filled reliably or simultaneously - a venue is not "
        "accepting orders, or the legs settle far apart in time."
    ),
    ArbitrageLabel.STALE_QUOTE.value: (
        "One venue has repriced and the other has not. The resting quote is usually "
        "cancelled before a retail order arrives. This is a race, not an edge."
    ),
    ArbitrageLabel.INSUFFICIENT_LIQUIDITY.value: (
        "The edge is real at the top of the book but the executable size is too small "
        "to be worth the transaction and capital costs."
    ),
    ArbitrageLabel.NOT_GUARANTEED.value: (
        "A precondition for a risk-free claim is missing - most often that the outcome "
        "set is not provably exhaustive."
    ),
    ArbitrageLabel.LOGICAL_MISPRICING.value: (
        "Prices violate a logical constraint (monotonicity, or a probability sum). "
        "Real information, but not automatically harvestable: capturing it needs a "
        "complete executable hedge, which is checked separately."
    ),
}


@router.get("")
def list_arbitrage(
    kind: ArbitrageKind | None = Query(None),
    label: ArbitrageLabel | None = Query(None),
    executable_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    latest_batch = db.scalar(
        apply_provenance(
            select(ArbitrageOpportunity.batch_id).order_by(
                ArbitrageOpportunity.created_at.desc()
            ),
            ArbitrageOpportunity.provenance,
            mode,
        ).limit(1)
    )
    if not latest_batch:
        return envelope(
            [], mode,
            batch_id=None,
            label_meanings=LABEL_MEANINGS,
            empty_reason=(
                "No arbitrage scan has run yet, or the most recent scan found nothing. "
                "Finding nothing is the normal result: both venues are actively "
                "arbitraged, and this scanner refuses to label an opportunity "
                "executable unless every leg is fillable after all costs."
            ),
        )

    stmt = (
        select(ArbitrageOpportunity)
        .where(ArbitrageOpportunity.batch_id == latest_batch)
        .order_by(ArbitrageOpportunity.max_net_profit.desc())
        .limit(limit)
    )
    stmt = apply_provenance(stmt, ArbitrageOpportunity.provenance, mode)
    if kind:
        stmt = stmt.where(ArbitrageOpportunity.kind == kind.value)
    if label:
        stmt = stmt.where(ArbitrageOpportunity.label == label.value)
    if executable_only:
        stmt = stmt.where(
            ArbitrageOpportunity.label == ArbitrageLabel.EXECUTABLE.value
        )

    rows = [
        {
            "id": o.id,
            "kind": o.kind,
            "label": o.label,
            "label_meaning": LABEL_MEANINGS.get(o.label, ""),
            # Public taxonomy. Only a guaranteed terminal payout may be called
            # arbitrage; every weaker label is demoted to a class that names how it
            # is weaker, so the strength of a claim never has to be inferred.
            "opportunity_class": classify_arbitrage_label(o.label).value,
            "may_be_called_arbitrage": classify_arbitrage_label(
                o.label
            ).may_be_called_arbitrage,
            "title": o.title,
            "legs": o.legs or [],
            "gross_edge_per_set": o.gross_edge_per_set,
            "total_cost_per_set": o.total_cost_per_set,
            "net_profit_per_set": o.net_profit_per_set,
            "max_executable_sets": o.max_executable_sets,
            "max_net_profit": o.max_net_profit,
            "capital_required": o.capital_required,
            "net_roi": o.net_roi,
            "rule_compatibility": o.rule_compatibility,
            "risk_flags": o.risk_flags or [],
            "quote_age_seconds": o.quote_age_seconds,
            "expected_resolution_time": o.expected_resolution_time,
            "cost_breakdown": o.cost_breakdown or {},
            "created_at": o.created_at,
            "provenance": o.provenance,
        }
        for o in db.scalars(stmt)
    ]

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1

    return envelope(
        rows, mode,
        batch_id=latest_batch,
        count=len(rows),
        counts_by_label=counts,
        label_meanings=LABEL_MEANINGS,
    )
