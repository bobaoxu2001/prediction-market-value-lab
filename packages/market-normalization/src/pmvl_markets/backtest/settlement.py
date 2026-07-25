"""Settlement synchronisation and recommendation grading.

Two jobs live here:

* **Settlement sync** - poll both venues for markets that have resolved and record
  the terminal payout.
* **Grading** - attach the realised outcome to every past recommendation and its
  immutable snapshot.

Grading writes the outcome onto the snapshot but changes nothing else about it. The
entry price, fair probability, confidence interval and evidence recorded at
publication time are never touched, which is what makes the public track record an
audit trail rather than a marketing artefact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.enums import (
    DataProvenance,
    MarketStatus,
    Platform,
    RecommendationState,
    SettlementResult,
    Side,
)
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import ResolutionInfo
from pmvl_shared.timeutil import utcnow

from ..db_models import Market, Recommendation, RecommendationSnapshot, Settlement
from ..ingest.runner import _market_from_row
from ..providers.kalshi import KalshiProvider
from ..providers.polymarket import PolymarketProvider

log = get_logger(__name__)


@dataclass
class SettlementReport:
    markets_checked: int = 0
    settlements_written: int = 0
    recommendations_graded: int = 0
    snapshots_graded: int = 0
    states_updated: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "markets_checked": self.markets_checked,
            "settlements_written": self.settlements_written,
            "recommendations_graded": self.recommendations_graded,
            "snapshots_graded": self.snapshots_graded,
            "states_updated": self.states_updated,
            "errors": self.errors,
        }


def payout_for_side(side: Side, yes_payout: Decimal) -> Decimal:
    """Terminal value of one contract of ``side``.

    A 50-50 resolution pays $0.50 to both sides, which is why this is arithmetic on
    the YES payout rather than a boolean.
    """
    return yes_payout if side == Side.YES else ONE - yes_payout


def result_to_enum(result: str | None) -> SettlementResult:
    mapping = {
        "yes": SettlementResult.YES,
        "no": SettlementResult.NO,
        "void": SettlementResult.VOID,
        "fifty_fifty": SettlementResult.FIFTY_FIFTY,
    }
    return mapping.get((result or "").lower(), SettlementResult.UNKNOWN)


async def sync_settlements(
    session: Session,
    *,
    lookback_days: int = 45,
    limit: int = 400,
    now: datetime | None = None,
) -> SettlementReport:
    """Check markets past their expected resolution for a terminal result."""
    now = now or utcnow()
    report = SettlementReport()
    cutoff = now - timedelta(days=lookback_days)

    already_settled = {
        (p, m) for p, m in session.execute(
            select(Settlement.platform, Settlement.platform_market_id)
        )
    }

    rows = session.scalars(
        select(Market).where(
            Market.expected_resolution_time.is_not(None),
            Market.expected_resolution_time < now,
            Market.expected_resolution_time > cutoff,
        )
    ).all()
    pending = [
        row for row in rows
        if (row.platform, row.platform_market_id) not in already_settled
    ][:limit]
    report.markets_checked = len(pending)

    if pending:
        providers: dict[Platform, Any] = {}
        try:
            if any(r.platform == Platform.KALSHI.value for r in pending):
                providers[Platform.KALSHI] = KalshiProvider()
            if any(r.platform == Platform.POLYMARKET.value for r in pending):
                providers[Platform.POLYMARKET] = PolymarketProvider()

            async def resolve(row: Market) -> tuple[Market, ResolutionInfo | None]:
                provider = providers.get(Platform(row.platform))
                if provider is None:
                    return row, None
                try:
                    return row, await provider.get_resolution(_market_from_row(row))
                except Exception as exc:  # noqa: BLE001 - one market must not stop the sync
                    report.errors.append(f"{row.platform_market_id}: {exc}")
                    return row, None

            results = await asyncio.gather(*(resolve(r) for r in pending))
            for row, info in results:
                if info is None or not info.resolved:
                    continue
                _write_settlement(session, row, info, now=now)
                report.settlements_written += 1
        finally:
            await asyncio.gather(
                *(p.aclose() for p in providers.values()), return_exceptions=True
            )

    session.flush()
    graded = grade_recommendations(session, now=now)
    report.recommendations_graded = graded[0]
    report.snapshots_graded = graded[1]
    report.states_updated = refresh_recommendation_states(session, now=now)

    log.info(
        "settlement sync: %d checked, %d settled, %d recommendations graded",
        report.markets_checked, report.settlements_written, report.recommendations_graded,
    )
    return report


def _write_settlement(
    session: Session, row: Market, info: ResolutionInfo, *, now: datetime
) -> Settlement:
    settlement = session.scalar(
        select(Settlement).where(
            Settlement.platform == row.platform,
            Settlement.platform_market_id == row.platform_market_id,
        )
    )
    if settlement is None:
        settlement = Settlement(
            platform=row.platform,
            platform_market_id=row.platform_market_id,
            created_at=now,
        )
        session.add(settlement)

    settlement.market_id = row.id
    settlement.result = result_to_enum(info.result).value
    settlement.yes_payout = info.yes_payout if info.yes_payout is not None else ZERO
    settlement.settled_at = info.settled_at or now
    settlement.settlement_source = info.settlement_source
    settlement.disputed = info.disputed
    settlement.provenance = DataProvenance.LIVE.value
    settlement.raw_payload = info.raw
    settlement.updated_at = now

    row.status = MarketStatus.SETTLED.value
    row.result = settlement.result
    row.actual_settlement_time = settlement.settled_at
    row.accepting_orders = False
    return settlement


def grade_recommendations(
    session: Session, *, now: datetime | None = None
) -> tuple[int, int]:
    """Attach realised outcomes to recommendations and their snapshots.

    Realised profit uses the cost recorded **at publication**, not the current price:
    the track record must answer "what would have happened had you acted on this when
    it was published", and re-pricing the entry would answer a different, flattering
    question.
    """
    now = now or utcnow()
    settlements = {
        s.market_id: s
        for s in session.scalars(select(Settlement).where(Settlement.market_id.is_not(None)))
    }
    if not settlements:
        return 0, 0

    graded_recs = 0
    recs = session.scalars(
        select(Recommendation).where(
            Recommendation.settlement_result.is_(None),
            Recommendation.market_id.in_(settlements.keys()),
        )
    ).all()
    for rec in recs:
        settlement = settlements.get(rec.market_id)
        if settlement is None:
            continue
        payout = payout_for_side(Side(rec.side), settlement.yes_payout)
        rec.settlement_result = settlement.result
        rec.realized_profit_per_contract = quantize_usd(
            payout - rec.total_cost_per_contract
        )
        rec.settled_at = settlement.settled_at
        rec.state = RecommendationState.SETTLED.value
        rec.state_checked_at = now
        graded_recs += 1

    graded_snapshots = 0
    snapshots = session.scalars(
        select(RecommendationSnapshot).where(
            RecommendationSnapshot.final_result.is_(None),
            RecommendationSnapshot.market_id.in_(settlements.keys()),
        )
    ).all()
    for snapshot in snapshots:
        settlement = settlements.get(snapshot.market_id)
        if settlement is None:
            continue
        payout = payout_for_side(Side(snapshot.side), settlement.yes_payout)
        profit = payout - snapshot.total_cost_at_recommendation
        snapshot.final_result = settlement.result
        snapshot.realized_profit_per_contract = quantize_usd(profit)
        # What $100 deployed at the published cost would have returned.
        if snapshot.total_cost_at_recommendation > 0:
            contracts = safe_div(D(100), snapshot.total_cost_at_recommendation)
            snapshot.realized_profit_at_100_usd = quantize_usd(profit * contracts)
        snapshot.settled_at = settlement.settled_at
        graded_snapshots += 1

    session.flush()
    return graded_recs, graded_snapshots


def refresh_recommendation_states(
    session: Session, *, now: datetime | None = None
) -> int:
    """Re-evaluate whether each open recommendation is still actionable.

    The published entry price is preserved; only ``current_*`` and ``state`` move.
    A recommendation whose edge has evaporated is marked, not deleted - hiding it
    would silently improve the apparent hit rate.
    """
    now = now or utcnow()
    open_recs = session.scalars(
        select(Recommendation).where(
            Recommendation.state.notin_(
                (RecommendationState.SETTLED.value,)
            )
        )
    ).all()
    if not open_recs:
        return 0

    market_ids = {r.market_id for r in open_recs}
    markets = {
        m.id: m for m in session.scalars(select(Market).where(Market.id.in_(market_ids)))
    }

    updated = 0
    for rec in open_recs:
        market = markets.get(rec.market_id)
        if market is None:
            continue

        if market.status == MarketStatus.SETTLED.value:
            rec.state = RecommendationState.SETTLED.value
        elif market.status in (MarketStatus.CLOSED.value, MarketStatus.PAUSED.value) or not market.accepting_orders:
            rec.state = RecommendationState.MARKET_CLOSED.value
        else:
            current = (
                market.best_yes_ask if rec.side == Side.YES.value else market.best_no_ask
            )
            rec.current_price = current
            if current is None:
                rec.state = RecommendationState.NO_LONGER_ACTIONABLE.value
            else:
                # Approximate current EV by substituting today's ask into the
                # publication-time cost stack. Fees and slippage scale with price,
                # but the difference is second-order next to the price move itself.
                delta = current - rec.entry_price
                current_cost = rec.total_cost_per_contract + delta
                current_ev = quantize_usd(rec.fair_probability - current_cost)
                rec.current_net_ev = current_ev
                if current_ev <= 0:
                    rec.state = RecommendationState.NO_LONGER_ACTIONABLE.value
                elif current_ev < rec.net_ev_per_contract * D("0.5"):
                    rec.state = RecommendationState.EDGE_REDUCED.value
                else:
                    rec.state = RecommendationState.STILL_ACTIONABLE.value

        rec.state_checked_at = now
        updated += 1

    session.flush()
    return updated
