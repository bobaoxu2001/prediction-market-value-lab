"""Arbitrage scan results, grouped by honesty label."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.enums import (
    ACTIONABLE_ARBITRAGE_LABELS,
    ArbitrageKind,
    ArbitrageLabel,
    classify_arbitrage_label,
    is_actionable_label,
)

from pmvl_markets.db_models import ArbitrageOpportunity, JobRun

from pmvl_shared.cadence import DeploymentMode
from pmvl_shared.config import get_settings
from pmvl_shared.freshness import DataType
from pmvl_shared.timeutil import age_seconds, ensure_utc

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope
from ..health import classify_empty_result

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
    view: str = Query(
        "all",
        pattern="^(all|actionable|diagnostics)$",
        description=(
            "'actionable' returns only opportunities that cleared every gate. "
            "'diagnostics' returns the research output - stale quotes, rule "
            "mismatches, logical inconsistencies, cost-rejected edges - which is real "
            "signal but not a trade. Mixing them lets a 25-hour-old quote with "
            "negative net profit sit in a list a reader takes as actionable."
        ),
    ),
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
    diagnostics = _matching_diagnostics(db)
    # Why this list is the length it is. An empty result is the platform's most
    # common output and its most dangerous, because a scan that found nothing and
    # a scanner that never ran render identically.
    settings = get_settings()
    mode_value = (
        DeploymentMode.READ_ONLY_SNAPSHOT
        if settings.snapshot_mode
        else settings.deployment_mode
    )
    scan_age = (
        age_seconds(ensure_utc(diagnostics["ran_at"]))
        if diagnostics and diagnostics.get("ran_at")
        else None
    )
    health = classify_empty_result(
        db, mode_value, input_age_seconds=scan_age, input_type=DataType.ARBITRAGE_SCAN
    )

    if not latest_batch:
        return envelope(
            [], mode,
            batch_id=None,
            label_meanings=LABEL_MEANINGS,
            matching_diagnostics=diagnostics,
            health=health,
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
    if executable_only or view == "actionable":
        stmt = stmt.where(
            ArbitrageOpportunity.label.in_(sorted(ACTIONABLE_ARBITRAGE_LABELS))
        )
    elif view == "diagnostics":
        stmt = stmt.where(
            ArbitrageOpportunity.label.notin_(sorted(ACTIONABLE_ARBITRAGE_LABELS))
        )

    rows = [
        {
            "id": o.id,
            "kind": o.kind,
            "label": o.label,
            "label_meaning": LABEL_MEANINGS.get(o.label, ""),
            "actionable": is_actionable_label(o.label),
            "equivalence_verdict": o.equivalence_verdict,
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
        matching_diagnostics=diagnostics,
        health=health,
        view=view,
        actionable_count=sum(1 for r in rows if r["actionable"]),
        diagnostic_count=sum(1 for r in rows if not r["actionable"]),
        view_note=(
            "Only opportunities in the 'actionable' view cleared every gate. "
            "Diagnostics are research output - a stale quote or a rule mismatch is a "
            "finding, not a trade."
        ),
    )


def _matching_diagnostics(db: Session) -> dict[str, Any] | None:
    """Why cross-platform pairs failed to reach equivalence on the last scan.

    Zero cross-platform arbitrage is either a finding about the venues or a gap in
    rule parsing, and the two are indistinguishable from the outside. Surfacing the
    histogram is what lets a reader tell which one they are looking at instead of
    taking the empty list on trust.
    """
    row = db.scalar(
        select(JobRun)
        .where(JobRun.job_name == "arbitrage")
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )
    histogram = (row.details or {}).get("demotion_histogram") if row else None
    if not histogram:
        return None
    return {
        "ran_at": row.started_at,
        "pairs_examined": histogram.get("pairs_examined"),
        "verified_equivalent": histogram.get("verified_equivalent"),
        "blocked_only_by_missing_info": histogram.get("blocked_only_by_missing_info"),
        "missing_information_count": histogram.get("missing_information_count"),
        "contradiction_count": histogram.get("contradiction_count"),
        "top_reasons": (histogram.get("by_code") or [])[:8],
        "diagnosis": histogram.get("diagnosis"),
    }
