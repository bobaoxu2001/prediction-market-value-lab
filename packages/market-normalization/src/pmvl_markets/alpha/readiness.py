"""How far the alpha engine is from being able to say anything.

The one question this module answers: **can a strategy be evaluated on real
point-in-time history yet, and if not, what exactly is missing?**

It is deliberately a *readiness* report rather than a result. The infrastructure
exists; the data does not. Reporting a strategy result computed on the data that
is currently in the database would be reporting a number about demo rows, and
that is the single most misleading thing this repository could publish.

The existing ``pmvl readiness`` answers the same shape of question for the
probability track record. This is its counterpart for the alpha layer, and it
uses the same evidence vocabulary so the two cannot disagree about what "enough"
means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmvl_shared.enums import DataProvenance
from pmvl_shared.timeutil import ensure_utc

from ..db_models import (
    OrderbookSnapshot,
    RecommendationSnapshot,
    Settlement,
    ShadowDecisionRow,
    SourceObservationRow,
    Trade,
)
from .evidence import (
    MIN_DECISIONS_FOR_EVIDENCE,
    NO_VERIFIED_BACKTEST,
    EvidenceStatus,
    assess,
    sufficient_history,
)
from .registry import REGISTRY


@dataclass
class AlphaReadiness:
    """What exists, what is missing, and what the result would be worth."""

    #: Point-in-time source observations recorded so far.
    source_observations: int
    #: Distinct markets with at least one observation.
    markets_with_observations: int
    #: Distinct observation kinds recorded.
    observation_kinds: tuple[str, ...]
    #: Span of the observation history.
    observation_span: tuple[datetime, datetime] | None

    #: Order-book captures, which a fill simulation needs.
    orderbook_snapshots: int
    #: Public trade prints, which a MAKER fill simulation needs.
    trade_prints: int
    #: Live (non-demo) settled markets, which an outcome needs.
    live_settlements: int
    #: Live recommendation snapshots - the existing walk-forward input.
    live_recommendation_snapshots: int
    #: Shadow decisions published so far.
    shadow_decisions: int

    registered_factors: int
    blockers: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    @property
    def can_run_economic_backtest(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        evidence = assess(
            decisions=self.shadow_decisions,
            settled_decisions=0 if self.blockers else self.shadow_decisions,
            synthetic=bool(self.blockers),
            held_out=False,
            parameters_fitted_on_this_period=True,
        )
        return {
            "verdict": (
                "RESEARCH INFRA READY"
                if not self.blockers
                else "RESEARCH INFRA READY (no economic data)"
            ),
            "headline": (
                NO_VERIFIED_BACKTEST
                if not self.can_run_economic_backtest
                else "point-in-time history is sufficient to attempt a run"
            ),
            "evidence_status": evidence.status.value,
            "counts": {
                "source_observations": self.source_observations,
                "markets_with_observations": self.markets_with_observations,
                "observation_kinds": list(self.observation_kinds),
                "orderbook_snapshots": self.orderbook_snapshots,
                "trade_prints": self.trade_prints,
                "live_settlements": self.live_settlements,
                "live_recommendation_snapshots": self.live_recommendation_snapshots,
                "shadow_decisions": self.shadow_decisions,
                "registered_factors": self.registered_factors,
            },
            "observation_span": (
                [self.observation_span[0].isoformat(), self.observation_span[1].isoformat()]
                if self.observation_span
                else None
            ),
            "blockers": list(self.blockers),
            "next_steps": list(self.next_steps),
        }


#: Minimum order-book captures before a fill simulation has anything to walk.
MIN_BOOKS = 500
#: Minimum public prints before a MAKER fill can be simulated at all.
#:
#: A maker fill requires traded volume through the level. With no prints the
#: conservative fill model correctly refuses every resting order, and a run
#: would report a 0% fill rate that says nothing about the strategy.
MIN_TRADE_PRINTS = 1_000
#: Minimum source observations before a source-lag factor can see a *change*.
MIN_OBSERVATIONS = 200


def assess_readiness(session: Session) -> AlphaReadiness:
    """Count what exists and name what is missing.

    Every count is of **live** rows. Demo rows are excluded rather than filtered
    later: the whole purpose of this report is to answer whether real evidence
    is possible, and a demo row is not evidence of anything.
    """

    def count(model, *filters) -> int:  # noqa: ANN001
        query = select(func.count()).select_from(model)
        for condition in filters:
            query = query.where(condition)
        return int(session.scalar(query) or 0)

    live = DataProvenance.LIVE

    source_observations = count(
        SourceObservationRow, SourceObservationRow.provenance == live
    )
    markets_with_observations = int(
        session.scalar(
            select(func.count(func.distinct(SourceObservationRow.market_id))).where(
                SourceObservationRow.provenance == live
            )
        )
        or 0
    )
    kinds = tuple(
        sorted(
            session.scalars(
                select(SourceObservationRow.kind)
                .where(SourceObservationRow.provenance == live)
                .distinct()
            ).all()
        )
    )
    bounds = session.execute(
        select(
            func.min(SourceObservationRow.available_at),
            func.max(SourceObservationRow.available_at),
        ).where(SourceObservationRow.provenance == live)
    ).one()
    span = (
        (ensure_utc(bounds[0]), ensure_utc(bounds[1]))
        if bounds[0] and bounds[1]
        else None
    )

    readiness = AlphaReadiness(
        source_observations=source_observations,
        markets_with_observations=markets_with_observations,
        observation_kinds=kinds,
        observation_span=span,
        orderbook_snapshots=count(OrderbookSnapshot, OrderbookSnapshot.provenance == live),
        trade_prints=count(Trade, Trade.provenance == live),
        live_settlements=count(Settlement, Settlement.provenance == live),
        live_recommendation_snapshots=count(
            RecommendationSnapshot, RecommendationSnapshot.provenance == live
        ),
        shadow_decisions=count(ShadowDecisionRow, ShadowDecisionRow.provenance == live),
        registered_factors=len(REGISTRY),
    )

    _diagnose(readiness)
    return readiness


def _diagnose(readiness: AlphaReadiness) -> None:
    """Turn counts into named blockers and the step that clears each one.

    Every blocker names the *specific* thing that is absent, because "not enough
    data" is not actionable and "no public trade prints, so no maker order can
    be simulated as filled" is.
    """
    if readiness.source_observations < MIN_OBSERVATIONS:
        readiness.blockers.append(
            f"{readiness.source_observations} point-in-time source observations "
            f"recorded against the {MIN_OBSERVATIONS} a source-lag factor needs "
            "before it can see a change rather than a level"
        )
        readiness.next_steps.append(
            "record NWS gridpoint forecasts and Cleveland Fed nowcasts into "
            "source_observations on every ingest, with available_at set to the "
            "moment the poll completed - not to the source's own publication time"
        )

    if readiness.trade_prints < MIN_TRADE_PRINTS:
        readiness.blockers.append(
            f"{readiness.trade_prints} public trade prints against the "
            f"{MIN_TRADE_PRINTS} a maker fill simulation needs. Without traded "
            "volume the conservative fill model correctly refuses every resting "
            "order, so a run would report a 0% fill rate that says nothing"
        )
        readiness.next_steps.append(
            "retain per-market trade prints for the research universe rather "
            "than pruning them to the display window"
        )

    if readiness.orderbook_snapshots < MIN_BOOKS:
        readiness.blockers.append(
            f"{readiness.orderbook_snapshots} order-book captures against the "
            f"{MIN_BOOKS} a fill simulation needs to walk"
        )

    if readiness.live_settlements == 0:
        readiness.blockers.append(
            "no live settlements: every position would end unresolved and no "
            "outcome could be scored"
        )

    if readiness.observation_span is not None:
        span = readiness.observation_span[1] - readiness.observation_span[0]
        ok, reason = sufficient_history(span, readiness.source_observations)
        if not ok:
            readiness.blockers.append(reason)
    else:
        readiness.blockers.append(
            "no observation history at all, so there is no period to split into "
            "train, validation and an untouched out-of-sample set"
        )
        readiness.next_steps.append(
            f"accumulate at least {MIN_DECISIONS_FOR_EVIDENCE * 3} decisions "
            "across a span long enough to divide three ways"
        )

    if not readiness.blockers:
        readiness.next_steps.append(
            "history is sufficient to attempt a run; fix the split boundaries "
            "BEFORE looking at the test period, and record them beside the result"
        )


def evidence_status_now(session: Session) -> EvidenceStatus:
    """The status any alpha result computed today would carry."""
    readiness = assess_readiness(session)
    return (
        EvidenceStatus.INSUFFICIENT_DATA
        if readiness.blockers
        else EvidenceStatus.RESEARCH_ONLY
    )
