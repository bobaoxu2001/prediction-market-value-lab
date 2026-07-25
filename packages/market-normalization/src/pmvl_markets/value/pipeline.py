"""Scoring and ranking pipeline: DB -> ensemble -> candidates -> recommendations.

Cross-platform quotes are wired into the ensemble here. That linkage is what turns a
matched pair of markets into an *independent* prior for each side of the pair, and it
is the main reason the matching engine exists at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.config import get_settings
from pmvl_shared.enums import (
    Category,
    DataProvenance,
    MarketStatus,
    Platform,
    RecommendationState,
    Side,
)
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ZERO
from pmvl_shared.schemas import FairProbability, NormalizedMarket, OrderBook, ValueCandidate
from pmvl_shared.timeutil import HORIZONS, Horizon, utcnow

from ..db_models import (
    Market,
    MarketMatch,
    ModelPrediction,
    ModelVersion,
    Recommendation,
)
from ..ingest.runner import _market_from_row
from ..ingest.store import latest_orderbook, orderbook_from_snapshot
from ..probability.base import ModelContext
from ..probability.ensemble import MODEL_VERSION, ProbabilityEnsemble
from ..research.provider import get_research_provider
from .ranking import RankingConfig, build_candidate, horizon_of, rank_candidates

log = get_logger(__name__)


@dataclass
class ScoringReport:
    batch_id: str = ""
    markets_considered: int = 0
    markets_scored: int = 0
    predictions_written: int = 0
    candidates_built: int = 0
    recommendations_written: int = 0
    by_horizon: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "markets_considered": self.markets_considered,
            "markets_scored": self.markets_scored,
            "predictions_written": self.predictions_written,
            "candidates_built": self.candidates_built,
            "recommendations_written": self.recommendations_written,
            "by_horizon": self.by_horizon,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def load_scoreable_markets(
    session: Session, *, limit: int | None = None, now: datetime | None = None
) -> list[tuple[Market, NormalizedMarket, OrderBook]]:
    """Markets with a live book, inside a horizon, that could produce a candidate."""
    now = now or utcnow()
    rows = session.scalars(
        select(Market).where(
            Market.status == MarketStatus.OPEN.value,
            Market.accepting_orders.is_(True),
            Market.expected_resolution_time.is_not(None),
        )
    ).all()

    out: list[tuple[Market, NormalizedMarket, OrderBook]] = []
    for row in rows:
        market = _market_from_row(row)
        if horizon_of(market, now=now) is None:
            continue
        snapshot = latest_orderbook(session, row.id)
        if snapshot is None:
            continue
        book = orderbook_from_snapshot(session, snapshot, row)
        if book.is_empty:
            continue
        out.append((row, market, book))

    out.sort(key=lambda triple: -(triple[1].volume_24h or ZERO))
    return out[:limit] if limit else out


def build_cross_platform_quotes(
    session: Session, market_ids: Sequence[int]
) -> dict[int, dict[str, Decimal]]:
    """For each market, the mid-prices of its verified matches on other venues.

    Only matches whose rule compatibility is at least ``similar`` are used. A pairing
    the verifier rejected is not evidence about this market's probability - feeding
    it in would create an "independent prior" out of an unrelated question.
    """
    if not market_ids:
        return {}

    id_set = set(market_ids)
    matches = session.scalars(
        select(MarketMatch).where(
            MarketMatch.rule_compatibility.in_(("identical", "equivalent", "similar"))
        )
    ).all()

    relevant = [
        m for m in matches if m.market_a_id in id_set or m.market_b_id in id_set
    ]
    if not relevant:
        return {}

    needed = {m.market_a_id for m in relevant} | {m.market_b_id for m in relevant}
    market_rows = {
        row.id: row
        for row in session.scalars(select(Market).where(Market.id.in_(needed)))
    }

    out: dict[int, dict[str, Decimal]] = {}
    for match in relevant:
        for own_id, other_id in (
            (match.market_a_id, match.market_b_id),
            (match.market_b_id, match.market_a_id),
        ):
            if own_id not in id_set:
                continue
            other = market_rows.get(other_id)
            if other is None:
                continue
            mid = _mid_of(other)
            if mid is None:
                continue
            # Opposite polarity means the other venue's YES is this market's NO.
            if match.polarity_inverted:
                mid = D(1) - mid
            out.setdefault(own_id, {})[other.platform] = mid
    return out


def _mid_of(row: Market) -> Decimal | None:
    if row.best_yes_bid is not None and row.best_yes_ask is not None:
        return (row.best_yes_bid + row.best_yes_ask) / D(2)
    return row.best_yes_ask or row.last_trade_price


def build_sibling_prices(
    session: Session, rows: Sequence[Market]
) -> dict[int, list[tuple[str, Decimal]]]:
    """Prices of the other outcomes in each market's event."""
    event_ids = {r.event_id for r in rows if r.event_id}
    if not event_ids:
        return {}

    siblings: dict[int, list[Market]] = {}
    for row in session.scalars(select(Market).where(Market.event_id.in_(event_ids))):
        siblings.setdefault(row.event_id, []).append(row)

    out: dict[int, list[tuple[str, Decimal]]] = {}
    for row in rows:
        if not row.event_id:
            continue
        peers = [s for s in siblings.get(row.event_id, []) if s.id != row.id]
        prices = [(s.platform_market_id, _mid_of(s)) for s in peers]
        out[row.id] = [(pid, p) for pid, p in prices if p is not None]
    return out


async def score_markets(
    session: Session,
    *,
    limit: int | None = None,
    now: datetime | None = None,
    ensemble: ProbabilityEnsemble | None = None,
) -> tuple[ScoringReport, list[ValueCandidate]]:
    """Run the ensemble over every scoreable market and build value candidates."""
    settings = get_settings()
    now = now or utcnow()
    report = ScoringReport(batch_id=uuid4().hex[:16])
    config = RankingConfig.from_settings()

    triples = load_scoreable_markets(session, limit=limit, now=now)
    report.markets_considered = len(triples)
    if not triples:
        return report, []

    rows = [t[0] for t in triples]
    cross_quotes = build_cross_platform_quotes(session, [r.id for r in rows])
    sibling_prices = build_sibling_prices(session, rows)
    event_flags = _event_flags(session, rows)

    owns_ensemble = ensemble is None
    ensemble = ensemble or ProbabilityEnsemble(research_provider=get_research_provider())
    _ensure_model_version(session)

    candidates: list[ValueCandidate] = []
    try:
        for row, market, book in triples:
            ctx = ModelContext(
                market=market,
                target_book=book,
                cross_platform_quotes={
                    k: v for k, v in cross_quotes.get(row.id, {}).items()
                },
                sibling_outcome_prices=sibling_prices.get(row.id, []),
                now=now,
                extra=event_flags.get(row.event_id or -1, {}),
            )
            try:
                output = await ensemble.estimate(ctx)
            except Exception as exc:  # noqa: BLE001 - one market must not kill the run
                report.errors.append(f"{market.platform_market_id}: {exc}")
                continue

            fair = output.fair
            report.markets_scored += 1
            _write_prediction(session, row, fair)
            report.predictions_written += 1

            horizon = horizon_of(market, now=now)
            if horizon is None:
                continue

            for side in (Side.YES, Side.NO):
                candidate = build_candidate(
                    market, book, fair, side, horizon,
                    config=config, now=now, market_id=row.id,
                )
                if candidate is not None:
                    candidates.append(candidate)
    finally:
        if owns_ensemble:
            await ensemble.aclose()

    report.candidates_built = len(candidates)
    session.flush()
    return report, candidates


def _event_flags(session: Session, rows: Sequence[Market]) -> dict[int, dict[str, Any]]:
    from ..db_models import Event

    event_ids = {r.event_id for r in rows if r.event_id}
    if not event_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for ev in session.scalars(select(Event).where(Event.id.in_(event_ids))):
        out[ev.id] = {
            "mutually_exclusive_exhaustive": bool(ev.mutually_exclusive and ev.exhaustive),
            "negative_risk": ev.negative_risk,
        }
    return out


def _ensure_model_version(session: Session) -> None:
    existing = session.scalar(
        select(ModelVersion).where(
            ModelVersion.name == "ensemble", ModelVersion.version == MODEL_VERSION
        )
    )
    if existing is not None:
        return
    session.add(
        ModelVersion(
            name="ensemble",
            version=MODEL_VERSION,
            description=(
                "Log-odds pooled ensemble: cross-platform consensus, sibling coherence, "
                "related markets, crypto GBM (Coinbase), weather NWS, research agent, "
                "and a non-independent target-market reference prior."
            ),
            config={"model_version": MODEL_VERSION},
            created_at=utcnow(),
        )
    )
    session.flush()


def _write_prediction(session: Session, row: Market, fair: FairProbability) -> ModelPrediction:
    prediction = ModelPrediction(
        market_id=row.id,
        model_version=fair.model_version or MODEL_VERSION,
        fair_probability_mean=fair.fair_probability_mean,
        fair_probability_low=fair.fair_probability_low,
        fair_probability_high=fair.fair_probability_high,
        model_confidence=fair.model_confidence,
        data_freshness_seconds=fair.data_freshness_seconds,
        evidence_quality=fair.evidence_quality,
        has_independent_prior=fair.has_independent_prior,
        market_implied_probability=fair.market_implied_probability,
        components=[c.model_dump(mode="json") for c in fair.components],
        explanation=fair.probability_explanation,
        category=fair.category.value,
        provenance=DataProvenance(row.provenance).value,
        created_at=utcnow(),
    )
    session.add(prediction)
    session.flush()
    return prediction


def publish_recommendations(
    session: Session,
    candidates: Sequence[ValueCandidate],
    *,
    batch_id: str,
    now: datetime | None = None,
    config: RankingConfig | None = None,
) -> dict[str, int]:
    """Rank per horizon and persist the Top-N as recommendations."""
    now = now or utcnow()
    config = config or RankingConfig.from_settings()
    written: dict[str, int] = {}

    for horizon in HORIZONS:
        top = rank_candidates(candidates, horizon, config=config)
        written[horizon] = len(top)
        for rank, candidate in enumerate(top, start=1):
            prediction = session.scalar(
                select(ModelPrediction)
                .where(ModelPrediction.market_id == candidate.market_id)
                .order_by(ModelPrediction.created_at.desc())
                .limit(1)
            )
            session.add(
                Recommendation(
                    batch_id=batch_id,
                    market_id=candidate.market_id,
                    prediction_id=prediction.id if prediction else None,
                    horizon=horizon,
                    rank=rank,
                    side=candidate.side.value,
                    entry_price=candidate.entry_price,
                    executable_size=candidate.executable_size,
                    total_cost_per_contract=candidate.total_cost_per_contract,
                    fair_probability=candidate.fair.fair_probability_mean,
                    fair_probability_low=candidate.fair.fair_probability_low,
                    fair_probability_high=candidate.fair.fair_probability_high,
                    net_ev_per_contract=candidate.net_ev_per_contract,
                    conservative_net_ev=candidate.conservative_net_ev,
                    net_roi=candidate.net_roi,
                    expected_profit_10=_profit_at(candidate, D(10)),
                    expected_profit_50=_profit_at(candidate, D(50)),
                    expected_profit_100=_profit_at(candidate, D(100)),
                    expected_profit_per_100_usd=candidate.expected_profit_per_100_usd,
                    fractional_kelly=candidate.fractional_kelly,
                    recommended_position_cap=candidate.recommended_position_cap,
                    composite_score=candidate.composite_score,
                    model_confidence=candidate.fair.model_confidence,
                    spread=candidate.spread,
                    liquidity_usd=candidate.liquidity_usd,
                    risk_flags=candidate.risk_flags,
                    cost_breakdown=candidate.cost.model_dump(mode="json"),
                    model_version=candidate.fair.model_version,
                    expected_resolution_time=candidate.expected_resolution_time,
                    state=RecommendationState.STILL_ACTIONABLE.value,
                    current_price=candidate.entry_price,
                    current_net_ev=candidate.net_ev_per_contract,
                    state_checked_at=now,
                    provenance=candidate.provenance.value,
                    created_at=now,
                )
            )
    session.flush()
    return written


def _profit_at(candidate: ValueCandidate, size: Decimal) -> Decimal:
    for quote in candidate.sized_quotes:
        if quote.size == size:
            return quote.expected_profit
    return ZERO


async def run_ranking(
    session: Session, *, limit: int | None = None, now: datetime | None = None
) -> ScoringReport:
    """Score every market, then publish the Top-N for each horizon."""
    report, candidates = await score_markets(session, limit=limit, now=now)
    report.by_horizon = publish_recommendations(
        session, candidates, batch_id=report.batch_id, now=now
    )
    report.recommendations_written = sum(report.by_horizon.values())
    log.info(
        "ranking complete: %d scored, %d candidates, %d recommendations %s",
        report.markets_scored, report.candidates_built,
        report.recommendations_written, report.by_horizon,
    )
    return report
