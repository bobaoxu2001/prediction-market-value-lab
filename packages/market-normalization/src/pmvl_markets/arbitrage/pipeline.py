"""Arbitrage orchestration: run all five scanners and persist results."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.enums import ArbitrageLabel, MarketStatus, Platform, RuleCompatibility, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ZERO
from pmvl_shared.schemas import ArbitrageResult, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import horizons_for, utcnow

from ..db_models import ArbitrageOpportunity, Event, Market, MarketMatch, PriceSnapshot
from ..ingest.runner import _market_from_row
from ..ingest.store import latest_orderbook, orderbook_from_snapshot
from ..matching.candidates import generate_candidates
from ..matching.histogram import DemotionHistogram
from ..matching.verify import MatchVerdict, verify_match
from .complete_set import scan_complete_set
from .cross_platform import scan_cross_platform
from .logical import ThresholdMarket, scan_exhaustive_sum, scan_monotonicity
from .multi_outcome import OutcomeLeg, scan_multi_outcome
from .stale import QuoteObservation, detect_stale_quote

log = get_logger(__name__)


@dataclass
class ArbitrageReport:
    batch_id: str = ""
    markets_examined: int = 0
    matches_verified: int = 0
    matches_identical: int = 0
    #: Why candidate pairs failed to reach equivalence. Zero cross-platform matches is
    #: either a finding about the venues or a gap in rule parsing, and those look
    #: identical from the outside - this is what tells them apart.
    demotion_histogram: dict[str, Any] = field(default_factory=dict)
    opportunities: dict[str, int] = field(default_factory=dict)
    by_label: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "markets_examined": self.markets_examined,
            "matches_verified": self.matches_verified,
            "matches_identical": self.matches_identical,
            "demotion_histogram": self.demotion_histogram,
            "opportunities": self.opportunities,
            "by_label": self.by_label,
            "errors": self.errors,
        }


def _load_books(
    session: Session, *, now: datetime | None = None
) -> list[tuple[Market, NormalizedMarket, OrderBook]]:
    now = now or utcnow()
    rows = session.scalars(
        select(Market).where(
            Market.status == MarketStatus.OPEN.value,
            Market.expected_resolution_time.is_not(None),
        )
    ).all()
    out: list[tuple[Market, NormalizedMarket, OrderBook]] = []
    for row in rows:
        market = _market_from_row(row)
        if not horizons_for(market.expected_resolution_time, now=now):
            continue
        snapshot = latest_orderbook(session, row.id)
        if snapshot is None:
            continue
        book = orderbook_from_snapshot(session, snapshot, row)
        if book.is_empty:
            continue
        out.append((row, market, book))
    return out


def load_matchable_markets(
    session: Session, *, now: datetime | None = None
) -> list[tuple[Market, NormalizedMarket]]:
    """Every open, in-horizon market - with or without a fetched orderbook.

    Matching deliberately does *not* require a book. A match is a statement about
    settlement rules, and its most valuable use is supplying the probability engine
    with an independent cross-venue prior, which needs only the other venue's quote.
    Restricting matching to the small subset of markets that received an orderbook
    fetch would leave most markets with no independent prior and therefore no
    possibility of ever being recommended.
    """
    now = now or utcnow()
    rows = session.scalars(
        select(Market).where(
            Market.status == MarketStatus.OPEN.value,
            Market.expected_resolution_time.is_not(None),
        )
    ).all()
    out: list[tuple[Market, NormalizedMarket]] = []
    for row in rows:
        market = _market_from_row(row)
        if not horizons_for(market.expected_resolution_time, now=now):
            continue
        out.append((row, market))
    return out


def refresh_matches(
    session: Session,
    pairs: Sequence[tuple[Market, NormalizedMarket]],
    *,
    max_pairs: int = 4000,
    histogram_out: DemotionHistogram | None = None,
) -> list[tuple[MarketMatch, MatchVerdict]]:
    """Generate, verify and persist cross-platform matches.

    Matches are upserted rather than appended so re-running does not accumulate
    duplicate pairings, and so a pair that *stops* being compatible (because a venue
    edited its rules) has its verdict updated rather than leaving a stale claim.
    """
    by_platform: dict[Platform, list[NormalizedMarket]] = defaultdict(list)
    id_by_key: dict[str, int] = {}
    for row, market in pairs:
        by_platform[market.platform].append(market)
        id_by_key[f"{market.platform.value}:{market.platform_market_id}"] = row.id

    kalshi = by_platform.get(Platform.KALSHI, [])
    polymarket = by_platform.get(Platform.POLYMARKET, [])
    if not kalshi or not polymarket:
        log.info("cross-platform matching skipped: need markets on both venues")
        return []

    candidates = generate_candidates(kalshi, polymarket)[:max_pairs]
    stored: list[tuple[MarketMatch, MatchVerdict]] = []
    histogram = histogram_out if histogram_out is not None else DemotionHistogram()

    for candidate in candidates:
        verdict = verify_match(candidate)
        # Record EVERY verdict, including rejections. The rejected pairs are the
        # informative ones: they are what distinguishes "these venues do not list the
        # same contracts" from "the parser cannot read the rules".
        histogram.add(verdict)
        if verdict.rule_compatibility == RuleCompatibility.INCOMPATIBLE:
            continue
        key_a, key_b = candidate.key
        market_a_id = id_by_key.get(key_a)
        market_b_id = id_by_key.get(key_b)
        if market_a_id is None or market_b_id is None:
            continue

        row = session.scalar(
            select(MarketMatch).where(
                MarketMatch.market_a_id == market_a_id,
                MarketMatch.market_b_id == market_b_id,
            )
        )
        if row is None:
            row = MarketMatch(
                market_a_id=market_a_id,
                market_b_id=market_b_id,
                created_at=utcnow(),
            )
            session.add(row)

        row.match_confidence = verdict.match_confidence
        row.rule_compatibility = verdict.rule_compatibility.value
        row.time_compatibility = verdict.time_compatibility
        row.source_compatibility = verdict.source_compatibility
        row.outcome_mapping = verdict.outcome_mapping
        row.polarity_inverted = verdict.polarity_inverted
        row.resolution_hash_a = verdict.resolution_hash_a
        row.resolution_hash_b = verdict.resolution_hash_b
        row.mismatch_reasons = verdict.mismatch_reasons
        row.verified_by = "deterministic"
        row.llm_assisted = False
        row.updated_at = utcnow()
        stored.append((row, verdict))

    session.flush()
    log.info(
        "verified %d cross-platform matches (%d identical)",
        len(stored),
        sum(1 for _, v in stored if v.rule_compatibility == RuleCompatibility.IDENTICAL),
    )
    return stored


def _threshold_families(
    triples: Sequence[tuple[Market, NormalizedMarket, OrderBook]]
) -> dict[tuple[str, str], list[ThresholdMarket]]:
    """Group markets into monotone strike families by (platform, event).

    Only markets with a real numeric strike and a directional comparator qualify; a
    family of one is not a family.
    """
    families: dict[tuple[str, str], list[ThresholdMarket]] = defaultdict(list)
    for row, market, book in triples:
        strike = market.floor_strike if market.floor_strike is not None else market.cap_strike
        if strike is None:
            continue
        comparator = (market.strike_type or "").lower()
        if not comparator.startswith("greater"):
            # Restrict to "above X" families so the implication direction is known.
            continue
        if not market.platform_event_id:
            continue
        families[(market.platform.value, market.platform_event_id)].append(
            ThresholdMarket(market=market, book=book, threshold=strike, market_id=row.id)
        )
    return {k: v for k, v in families.items() if len(v) >= 2}


def _previous_mid(
    session: Session, market_id: int, *, before: datetime
) -> tuple[Decimal | None, datetime | None]:
    """The most recent price snapshot strictly before ``before``."""
    row = session.scalar(
        select(PriceSnapshot)
        .where(PriceSnapshot.market_id == market_id, PriceSnapshot.observed_at < before)
        .order_by(PriceSnapshot.observed_at.desc())
        .limit(1)
    )
    if row is None:
        return None, None
    return row.mid, row.observed_at


def run_arbitrage_scan(
    session: Session, *, now: datetime | None = None
) -> tuple[ArbitrageReport, list[ArbitrageResult]]:
    """Run every scanner and persist the results."""
    now = now or utcnow()
    report = ArbitrageReport(batch_id=uuid4().hex[:16])
    triples = _load_books(session, now=now)
    report.markets_examined = len(triples)
    if not triples:
        return report, []

    results: list[ArbitrageResult] = []
    counts: dict[str, int] = defaultdict(int)

    # ---- 1. binary complete set (within one market) ------------------------
    for row, market, book in triples:
        try:
            result = scan_complete_set(market, book, market_id=row.id)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"complete_set {market.platform_market_id}: {exc}")
            continue
        if result is not None:
            results.append(result)
            counts["complete_set"] += 1

    # ---- 2. cross-platform -------------------------------------------------
    # Matched over every open market, not just those with books, so that markets
    # without a fetched orderbook still gain an independent cross-venue prior.
    histogram = DemotionHistogram()
    matches = refresh_matches(
        session, load_matchable_markets(session, now=now), histogram_out=histogram
    )
    report.matches_verified = len(matches)
    report.matches_identical = sum(
        1 for _, v in matches if v.rule_compatibility == RuleCompatibility.IDENTICAL
    )
    report.demotion_histogram = histogram.as_dict()
    log.info("cross-platform matching: %s", histogram.diagnosis)

    by_id = {row.id: (row, market, book) for row, market, book in triples}
    for match_row, verdict in matches:
        left = by_id.get(match_row.market_a_id)
        right = by_id.get(match_row.market_b_id)
        if left is None or right is None:
            continue
        try:
            result = scan_cross_platform(
                left[1], left[2], right[1], right[2], verdict,
                market_a_id=left[0].id, market_b_id=right[0].id, match_id=match_row.id,
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"cross_platform {match_row.id}: {exc}")
            continue
        if result is not None:
            results.append(result)
            counts["cross_platform"] += 1

    # ---- 3. multi-outcome --------------------------------------------------
    by_event: dict[int, list[tuple[Market, NormalizedMarket, OrderBook]]] = defaultdict(list)
    for row, market, book in triples:
        if row.event_id:
            by_event[row.event_id].append((row, market, book))

    events = {
        ev.id: ev
        for ev in session.scalars(select(Event).where(Event.id.in_(by_event.keys())))
    } if by_event else {}

    # How many outcomes each event actually has, **as reported by the venue**.
    #
    # Counting our own ingested rows is circular and produces false arbitrage: a
    # Polymarket temperature event with eleven buckets from which only four were
    # ingested looks locally complete, its four cheap legs sum to $0.89, and the
    # scanner reports guaranteed profit. In reality the seven unfetched buckets hold
    # most of the probability mass and the basket pays nothing when one of them wins.
    # This exact false positive was produced live before the venue count was used.
    #
    # An unknown count (0) blocks the scan rather than falling back to the local
    # count, because "we don't know if this set is complete" must never read as "it is".
    outcome_totals: dict[int, int] = {}
    if by_event:
        for event_id, venue_count in session.execute(
            select(Event.id, Event.outcome_count).where(Event.id.in_(by_event.keys()))
        ):
            outcome_totals[event_id] = int(venue_count or 0)

    for event_id, members in by_event.items():
        if len(members) < 2:
            continue
        event = events.get(event_id)
        if event is None:
            continue

        known_outcomes = outcome_totals.get(event_id, 0)
        if known_outcomes <= 0:
            report.errors.append(
                f"multi_outcome event {event_id} skipped: the venue's outcome count is "
                f"unknown, so completeness of the basket cannot be established"
            )
            continue
        if len(members) < known_outcomes:
            report.errors.append(
                f"multi_outcome event {event_id} skipped: priced {len(members)} of "
                f"{known_outcomes} venue-reported outcomes; a partial basket is not a "
                f"complete set"
            )
            continue

        legs = [
            OutcomeLeg(market=market, book=book, market_id=row.id)
            for row, market, book in members
        ]
        try:
            result = scan_multi_outcome(
                legs,
                event_title=event.title or "event",
                mutually_exclusive=event.mutually_exclusive,
                exhaustive=event.exhaustive,
                negative_risk=event.negative_risk,
                expected_outcome_count=known_outcomes,
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"multi_outcome event {event_id}: {exc}")
            continue
        if result is not None:
            results.append(result)
            counts["multi_outcome"] += 1

    # ---- 4. logical constraints -------------------------------------------
    for (platform, event_ticker), family in _threshold_families(triples).items():
        try:
            violations = scan_monotonicity(
                family, ascending_implies=True, family_label=f"{platform} {event_ticker}"
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"monotonicity {event_ticker}: {exc}")
            continue
        results.extend(violations)
        counts["logical_constraint"] += len(violations)

    for event_id, members in by_event.items():
        event = events.get(event_id)
        if event is None or not (event.mutually_exclusive and event.exhaustive):
            continue
        # Same completeness requirement as the multi-outcome scanner: a sum over a
        # subset of outcomes says nothing about whether the full set sums to $1.
        venue_count = outcome_totals.get(event_id, 0)
        if venue_count <= 0 or len(members) < venue_count:
            continue
        legs = [
            ThresholdMarket(market=market, book=book, threshold=ZERO, market_id=row.id)
            for row, market, book in members
        ]
        result = scan_exhaustive_sum(legs, event_title=event.title or "event")
        if result is not None:
            results.append(result)
            counts["logical_constraint"] += 1

    # ---- 5. stale quotes ---------------------------------------------------
    for match_row, verdict in matches:
        left = by_id.get(match_row.market_a_id)
        right = by_id.get(match_row.market_b_id)
        if left is None or right is None:
            continue
        observations = []
        for row, market, book in (left, right):
            mid = _mid_from_book(book)
            if mid is None:
                observations = []
                break
            prev_mid, prev_at = _previous_mid(session, row.id, before=book.observed_at)
            observations.append(
                QuoteObservation(
                    market=market, book=book, current_mid=mid,
                    previous_mid=prev_mid, previous_at=prev_at, market_id=row.id,
                )
            )
        if len(observations) != 2:
            continue
        for leader, laggard in (observations, observations[::-1]):
            result = detect_stale_quote(
                leader, laggard,
                match_confidence=match_row.match_confidence,
                rule_compatibility=verdict.rule_compatibility,
            )
            if result is not None:
                results.append(result)
                counts["stale_quote"] += 1

    report.opportunities = dict(counts)
    label_counts: dict[str, int] = defaultdict(int)
    for result in results:
        label_counts[result.label.value] += 1
    report.by_label = dict(label_counts)

    persist_opportunities(session, results, batch_id=report.batch_id, now=now)
    log.info(
        "arbitrage scan: %d markets, %d matches, %d opportunities %s",
        report.markets_examined, report.matches_verified, len(results), report.by_label,
    )
    return report, results


def _mid_from_book(book: OrderBook) -> Decimal | None:
    bid, ask = book.best_bid(Side.YES), book.best_ask(Side.YES)
    if bid is not None and ask is not None:
        return (bid + ask) / D(2)
    return ask or bid


def persist_opportunities(
    session: Session,
    results: Sequence[ArbitrageResult],
    *,
    batch_id: str,
    now: datetime | None = None,
) -> int:
    now = now or utcnow()
    for result in results:
        session.add(
            ArbitrageOpportunity(
                batch_id=batch_id,
                kind=result.kind.value,
                label=result.label.value,
                title=result.title,
                legs=[leg.model_dump(mode="json") for leg in result.legs],
                gross_edge_per_set=result.gross_edge_per_set,
                total_cost_per_set=result.total_cost_per_set,
                net_profit_per_set=result.net_profit_per_set,
                max_executable_sets=result.max_executable_sets,
                max_net_profit=result.max_net_profit,
                capital_required=result.capital_required,
                net_roi=result.net_roi,
                rule_compatibility=result.rule_compatibility.value,
                match_id=result.match_id,
                risk_flags=result.risk_flags,
                quote_age_seconds=result.quote_age_seconds,
                expected_resolution_time=result.expected_resolution_time,
                cost_breakdown=result.cost_breakdown,
                provenance=result.provenance.value,
                created_at=now,
            )
        )
    session.flush()
    return len(results)
