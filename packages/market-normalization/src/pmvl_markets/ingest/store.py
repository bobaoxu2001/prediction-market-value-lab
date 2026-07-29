"""Idempotent persistence of provider output.

Every write is an upsert keyed on ``(platform, platform_market_id)`` so a re-run
never duplicates rows. Raw provider payloads are stored alongside the normalized
columns: when a venue silently renames a field, the raw blob is what makes the
regression diagnosable after the fact.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.config import get_settings
from pmvl_shared.enums import DataProvenance, Platform, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D
from pmvl_shared.schemas import NormalizedEvent, NormalizedMarket, OrderBook, TradeTick
from pmvl_shared.timeutil import utcnow

from ..db_models import (
    Event,
    Market,
    MarketRule,
    OrderbookLevel,
    OrderbookSnapshot,
    Outcome,
    PriceSnapshot,
    Trade,
)
from ..matching.rule_history import record_rule_version
from ..normalize.rules import normalize_rules

log = get_logger(__name__)


class DemoDataRejected(RuntimeError):
    """Raised when demo-provenance rows are written while ``ALLOW_DEMO_DATA`` is off."""


def _guard_provenance(provenance: DataProvenance) -> None:
    if provenance == DataProvenance.DEMO and not get_settings().allow_demo_data:
        raise DemoDataRejected(
            "refusing to persist demo-provenance data (ALLOW_DEMO_DATA=false)"
        )


def upsert_events(session: Session, events: Sequence[NormalizedEvent]) -> int:
    written = 0
    for ev in events:
        _guard_provenance(ev.provenance)
        row = session.scalar(
            select(Event).where(
                Event.platform == ev.platform.value,
                Event.platform_event_id == ev.platform_event_id,
            )
        )
        now = utcnow()
        if row is None:
            row = Event(
                platform=ev.platform.value,
                platform_event_id=ev.platform_event_id,
                created_at=now,
            )
            session.add(row)
        row.series_ticker = ev.series_ticker
        row.title = ev.title
        row.normalized_title = ev.normalized_title
        row.category = ev.category.value
        row.close_time = ev.close_time
        row.negative_risk = ev.negative_risk
        row.mutually_exclusive = ev.mutually_exclusive
        row.exhaustive = ev.exhaustive
        # Never let a later, thinner fetch erase a known count with 0 (unknown).
        if ev.outcome_count:
            row.outcome_count = ev.outcome_count
        row.provenance = ev.provenance.value
        row.raw_payload = ev.raw
        row.updated_at = now
        written += 1
    session.flush()
    return written


def upsert_markets(session: Session, markets: Sequence[NormalizedMarket]) -> dict[str, int]:
    """Upsert markets and their outcome/rule children.

    Returns a ``{platform_market_id: market_id}`` map so callers can attach
    orderbooks without a second query round-trip.
    """
    id_map: dict[str, int] = {}
    now = utcnow()

    # Resolve event FKs in one query rather than per market.
    event_keys = {
        (m.platform.value, m.platform_event_id) for m in markets if m.platform_event_id
    }
    event_ids: dict[tuple[str, str], int] = {}
    if event_keys:
        for row in session.scalars(
            select(Event).where(
                Event.platform_event_id.in_([k[1] for k in event_keys])
            )
        ):
            event_ids[(row.platform, row.platform_event_id)] = row.id

    for m in markets:
        _guard_provenance(m.provenance)
        row = session.scalar(
            select(Market).where(
                Market.platform == m.platform.value,
                Market.platform_market_id == m.platform_market_id,
            )
        )
        if row is None:
            row = Market(
                platform=m.platform.value,
                platform_market_id=m.platform_market_id,
                created_at=now,
            )
            session.add(row)

        row.event_id = event_ids.get((m.platform.value, m.platform_event_id or ""))
        row.platform_event_id = m.platform_event_id
        row.series_ticker = m.series_ticker
        row.title = m.title
        row.subtitle = m.subtitle
        row.normalized_title = m.normalized_title
        row.description = m.description
        row.category = m.category.value
        row.outcomes = m.outcomes
        row.yes_token_id = m.yes_token_id
        row.no_token_id = m.no_token_id
        row.condition_id = m.condition_id
        row.open_time = m.open_time
        row.close_time = m.close_time
        row.event_occurrence_time = m.event_occurrence_time
        row.expected_resolution_time = m.expected_resolution_time
        row.actual_settlement_time = m.actual_settlement_time
        row.source_timezone = m.source_timezone
        row.settlement_source = m.settlement_source
        row.settlement_rules_raw = m.settlement_rules_raw
        row.settlement_rules_normalized = m.settlement_rules_normalized
        row.resolution_hash = m.resolution_hash
        row.status = m.status.value
        row.result = m.result
        row.accepting_orders = m.accepting_orders
        row.market_type = m.market_type
        row.strike_type = m.strike_type
        row.floor_strike = m.floor_strike
        row.cap_strike = m.cap_strike
        row.tick_size = m.tick_size
        row.price_level_structure = m.price_level_structure
        row.min_order_size = m.min_order_size
        row.fee_rate = m.fee_rate
        row.maker_fee_rate = m.maker_fee_rate
        row.fee_type = m.fee_type
        row.best_yes_bid = m.best_yes_bid
        row.best_yes_ask = m.best_yes_ask
        row.best_no_bid = m.best_no_bid
        row.best_no_ask = m.best_no_ask
        row.spread = m.spread
        row.volume_24h = m.volume_24h
        row.total_volume = m.total_volume
        row.open_interest = m.open_interest
        row.last_trade_price = m.last_trade_price
        row.liquidity_usd = m.liquidity_usd
        row.quote_observed_at = m.quote_observed_at
        row.provenance = m.provenance.value
        row.raw_payload = m.raw
        row.updated_at = now

        session.flush()
        id_map[f"{m.platform.value}:{m.platform_market_id}"] = row.id
        _upsert_outcomes(session, row, m)
        _upsert_rule(session, row, m)

    session.flush()
    return id_map


def _upsert_outcomes(session: Session, row: Market, m: NormalizedMarket) -> None:
    existing = {o.label: o for o in session.scalars(
        select(Outcome).where(Outcome.market_id == row.id)
    )}
    labels = m.outcomes or ["Yes", "No"]
    tokens = [m.yes_token_id, m.no_token_id]
    for idx, label in enumerate(labels):
        out = existing.get(label)
        if out is None:
            out = Outcome(market_id=row.id, label=label, created_at=utcnow())
            session.add(out)
        out.index_in_market = idx
        out.is_yes = idx == 0
        out.token_id = tokens[idx] if idx < len(tokens) else None
        out.best_bid = m.best_yes_bid if idx == 0 else m.best_no_bid
        out.best_ask = m.best_yes_ask if idx == 0 else m.best_no_ask


def _upsert_rule(session: Session, row: Market, m: NormalizedMarket) -> None:
    rules = normalize_rules(
        title=m.title,
        subtitle=m.subtitle,
        description=m.settlement_rules_raw,
        settlement_source=m.settlement_source,
        cutoff_time=m.event_occurrence_time or m.expected_resolution_time or m.close_time,
        explicit_threshold=m.floor_strike if m.floor_strike is not None else m.cap_strike,
        has_structured_strike=bool(m.strike_type),
    )
    rule = session.scalar(select(MarketRule).where(MarketRule.market_id == row.id))
    if rule is None:
        rule = MarketRule(market_id=row.id, created_at=utcnow())
        session.add(rule)
    rule.settlement_source_name = m.settlement_source
    rule.threshold_semantics = rules.threshold_semantics
    rule.threshold_value = rules.threshold
    rule.comparator = rules.comparator
    rule.cutoff_time = rules.cutoff_utc
    rule.cutoff_timezone = m.source_timezone
    rule.includes_overtime = rules.includes_overtime
    rule.uses_revised_data = rules.uses_revised_data
    rule.entities = rules.entities[:12]
    rule.normalized_terms = rules.hash_payload()
    rule.resolution_hash = rules.resolution_hash

    # `rule` above is a single mutable row: a venue editing its resolution criteria
    # overwrites the text every stored verdict was derived from, and afterwards
    # nobody can tell whether a match was verified against the current wording or
    # an older one. The version history is append-only and keeps both.
    record_rule_version(
        session,
        market_id=row.id,
        raw_title=m.title,
        raw_subtitle=m.subtitle or "",
        raw_rules=m.settlement_rules_raw or "",
        raw_resolution_source=m.settlement_source or "",
        raw_cancellation_language=_extract_clause(m.settlement_rules_raw, _CANCELLATION_CUES),
        raw_postponement_language=_extract_clause(m.settlement_rules_raw, _POSTPONEMENT_CUES),
        platform_metadata={
            "platform": m.platform.value,
            "platform_market_id": m.platform_market_id,
            "market_type": m.market_type,
            "strike_type": m.strike_type,
        },
        source_endpoint=_SOURCE_ENDPOINTS.get(m.platform.value, ""),
        source_payload=m.raw,
        normalized_terms=rules.hash_payload(),
        extraction_confidence=_extraction_confidence(m, rules),
    )


#: Sentences a venue uses to describe voiding or rescheduling. Matched on the raw
#: text because neither venue exposes these as structured fields, and a
#: cancellation clause nobody captured is a settlement risk nobody can audit.
_CANCELLATION_CUES = (
    "cancel", "void", "annul", "no contest", "refund", "not resolve", "invalid",
)
_POSTPONEMENT_CUES = (
    "postpone", "delay", "reschedul", "suspend", "rain", "abandon",
)

_SOURCE_ENDPOINTS = {
    "kalshi": "https://api.elections.kalshi.com/trade-api/v2/markets",
    "polymarket": "https://gamma-api.polymarket.com/markets",
}


def _extract_clause(text: str | None, cues: tuple[str, ...]) -> str:
    """The sentences mentioning any cue, or empty when none do.

    Deliberately conservative: it returns the venue's own sentences rather than a
    paraphrase, because the point of preserving this is that a human can read what
    the venue actually committed to.
    """
    if not text:
        return ""
    sentences = [s.strip() for s in text.replace("\n", " ").split(". ") if s.strip()]
    hits = [s for s in sentences if any(cue in s.lower() for cue in cues)]
    return ". ".join(hits)[:2000]


def _extraction_confidence(m: NormalizedMarket, rules) -> Decimal:  # noqa: ANN001
    """How much of the settlement terms the parser actually pinned down.

    Not a model score - a count of the fields that were established, so a reader
    can tell a fully-parsed threshold market from one where only the title was
    understood.
    """
    signals = (
        bool((m.settlement_rules_raw or "").strip()),
        bool((m.settlement_source or "").strip()),
        rules.threshold is not None,
        bool(rules.comparator),
        rules.cutoff_utc is not None,
    )
    return (Decimal(sum(signals)) / Decimal(len(signals))).quantize(Decimal("0.0001"))


def store_orderbooks(
    session: Session,
    books: dict[str, OrderBook],
    id_map: dict[str, int],
    *,
    max_levels: int = 25,
) -> int:
    """Append orderbook snapshots and refresh each market's denormalised top-of-book.

    The market row's cached quote is overwritten from the book because the book is
    the executable truth; the venue's summary fields can lag it.
    """
    written = 0
    for key, book in books.items():
        market_id = id_map.get(key)
        if market_id is None:
            continue
        _guard_provenance(book.provenance)

        yes_depth = book.depth_notional(Side.YES)
        no_depth = book.depth_notional(Side.NO)
        snapshot = OrderbookSnapshot(
            market_id=market_id,
            observed_at=book.observed_at,
            source_timestamp=book.source_timestamp,
            best_yes_bid=book.best_bid(Side.YES),
            best_yes_ask=book.best_ask(Side.YES),
            best_no_bid=book.best_bid(Side.NO),
            best_no_ask=book.best_ask(Side.NO),
            yes_depth_usd=yes_depth,
            no_depth_usd=no_depth,
            provenance=book.provenance.value,
            raw_payload=None,  # levels are stored relationally below
        )
        session.add(snapshot)
        session.flush()

        for side, is_ask, levels in (
            (Side.YES, False, book.yes_bids),
            (Side.YES, True, book.yes_asks),
            (Side.NO, False, book.no_bids),
            (Side.NO, True, book.no_asks),
        ):
            for idx, lvl in enumerate(levels[:max_levels]):
                session.add(
                    OrderbookLevel(
                        snapshot_id=snapshot.id,
                        side=side.value,
                        is_ask=is_ask,
                        price=lvl.price,
                        size=lvl.size,
                        level_index=idx,
                    )
                )

        market = session.get(Market, market_id)
        if market is not None:
            market.best_yes_bid = snapshot.best_yes_bid
            market.best_yes_ask = snapshot.best_yes_ask
            market.best_no_bid = snapshot.best_no_bid
            market.best_no_ask = snapshot.best_no_ask
            if snapshot.best_yes_ask is not None and snapshot.best_yes_bid is not None:
                market.spread = snapshot.best_yes_ask - snapshot.best_yes_bid
            market.orderbook_depth_usd = yes_depth + no_depth
            market.quote_observed_at = book.observed_at

            session.add(
                PriceSnapshot(
                    market_id=market_id,
                    observed_at=book.observed_at,
                    yes_bid=snapshot.best_yes_bid,
                    yes_ask=snapshot.best_yes_ask,
                    mid=_mid(snapshot.best_yes_bid, snapshot.best_yes_ask),
                    last_trade_price=market.last_trade_price,
                    volume_24h=market.volume_24h,
                    source="orderbook",
                    provenance=book.provenance.value,
                )
            )
        written += 1
    session.flush()
    return written


def _mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / D(2)


def store_trades(
    session: Session, trades: Iterable[TradeTick], id_map: dict[str, int]
) -> int:
    """Insert trade prints, skipping any already stored (idempotent by trade id)."""
    written = 0
    for t in trades:
        key = f"{t.platform.value}:{t.platform_market_id}"
        market_id = id_map.get(key)
        exists = session.scalar(
            select(Trade.id).where(
                Trade.platform == t.platform.value,
                Trade.platform_trade_id == t.platform_trade_id,
            )
        )
        if exists:
            continue
        session.add(
            Trade(
                platform=t.platform.value,
                platform_trade_id=t.platform_trade_id,
                market_id=market_id,
                traded_at=t.traded_at,
                price=t.price,
                size=t.size,
                taker_side=t.taker_side,
                provenance=t.provenance.value,
            )
        )
        written += 1
    session.flush()
    return written


def load_market_id_map(session: Session, platform: Platform | None = None) -> dict[str, int]:
    stmt = select(Market.platform, Market.platform_market_id, Market.id)
    if platform:
        stmt = stmt.where(Market.platform == platform.value)
    return {f"{p}:{pid}": mid for p, pid, mid in session.execute(stmt)}


def prune_orderbook_snapshots(session: Session, *, keep_days: int = 30) -> int:
    """Drop snapshots older than the retention window.

    High-frequency book captures dominate storage. Recommendation snapshots and
    settlements - the audit trail - are never pruned.
    """
    from sqlalchemy import delete

    cutoff = utcnow() - _timedelta_days(keep_days)
    ids = list(
        session.scalars(
            select(OrderbookSnapshot.id).where(OrderbookSnapshot.observed_at < cutoff)
        )
    )
    if not ids:
        return 0
    session.execute(delete(OrderbookLevel).where(OrderbookLevel.snapshot_id.in_(ids)))
    session.execute(delete(OrderbookSnapshot).where(OrderbookSnapshot.id.in_(ids)))
    return len(ids)


def _timedelta_days(days: int):  # noqa: ANN202
    from datetime import timedelta

    return timedelta(days=days)


def latest_orderbook(session: Session, market_id: int) -> OrderbookSnapshot | None:
    return session.scalar(
        select(OrderbookSnapshot)
        .where(OrderbookSnapshot.market_id == market_id)
        .order_by(OrderbookSnapshot.observed_at.desc())
        .limit(1)
    )


def orderbook_from_snapshot(
    session: Session, snapshot: OrderbookSnapshot, market: Market
) -> OrderBook:
    """Rebuild an in-memory :class:`OrderBook` from persisted levels."""
    from pmvl_shared.schemas import BookLevel

    levels = session.scalars(
        select(OrderbookLevel)
        .where(OrderbookLevel.snapshot_id == snapshot.id)
        .order_by(OrderbookLevel.level_index)
    ).all()

    buckets: dict[tuple[str, bool], list[BookLevel]] = {}
    for lvl in levels:
        buckets.setdefault((lvl.side, lvl.is_ask), []).append(
            BookLevel(price=lvl.price, size=lvl.size)
        )

    return OrderBook(
        platform=Platform(market.platform),
        platform_market_id=market.platform_market_id,
        observed_at=snapshot.observed_at,
        source_timestamp=snapshot.source_timestamp,
        yes_bids=buckets.get(("yes", False), []),
        yes_asks=buckets.get(("yes", True), []),
        no_bids=buckets.get(("no", False), []),
        no_asks=buckets.get(("no", True), []),
        provenance=DataProvenance(snapshot.provenance),
    )


def as_datetime(value: datetime | None) -> datetime | None:
    from pmvl_shared.timeutil import ensure_utc

    return ensure_utc(value)
