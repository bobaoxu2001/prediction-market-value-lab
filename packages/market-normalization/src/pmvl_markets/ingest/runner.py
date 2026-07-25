"""Ingest orchestration: fetch from both venues, normalize, persist.

Orderbook fetching is the expensive step, so markets are prioritised before books
are requested: a market with no volume and no quote cannot produce a ranked
opportunity, and spending the rate-limit budget on it starves the ones that can.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from pmvl_shared.config import get_settings
from pmvl_shared.enums import MarketStatus, Platform
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.schemas import NormalizedMarket, OrderBook
from pmvl_shared.timeutil import horizons_for, utcnow

from ..providers.kalshi import KalshiProvider
from ..providers.polymarket import PolymarketProvider
from .store import (
    load_market_id_map,
    store_orderbooks,
    store_trades,
    upsert_events,
    upsert_markets,
)

log = get_logger(__name__)


@dataclass
class IngestReport:
    markets_fetched: int = 0
    markets_written: int = 0
    events_written: int = 0
    orderbooks_written: int = 0
    trades_written: int = 0
    by_platform: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    provider_stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "markets_fetched": self.markets_fetched,
            "markets_written": self.markets_written,
            "events_written": self.events_written,
            "orderbooks_written": self.orderbooks_written,
            "trades_written": self.trades_written,
            "by_platform": self.by_platform,
            "errors": self.errors,
            "provider_stats": self.provider_stats,
        }


def orderbook_priority(market: NormalizedMarket) -> tuple[int, Decimal]:
    """Sort key deciding which markets get a book fetch.

    Ordered by (horizon bucket, 24h volume). Markets resolving sooner are ranked
    first because their recommendations are the most time-sensitive; within a bucket,
    traded volume is the best available proxy for whether a quote is real.
    """
    horizons = horizons_for(market.expected_resolution_time)
    bucket = {"24h": 0, "7d": 1, "30d": 2}.get(horizons[0] if horizons else "", 3)
    return (bucket, -(market.volume_24h or Decimal("0")))


def select_for_orderbooks(
    markets: Sequence[NormalizedMarket], limit: int
) -> list[NormalizedMarket]:
    """Pick the markets worth spending an orderbook request on.

    Selection is **event-aware**. Once a market is chosen, its event siblings are
    pulled in with it, because multi-outcome arbitrage can only be assessed on a
    complete basket: pricing four buckets of an eleven-bucket temperature event is
    not a partial answer, it is no answer at all, and the scanner will (correctly)
    refuse to evaluate it. Volume-ranked selection alone almost never lands every
    outcome of an event, so without this the multi-outcome scanner never runs.
    """
    settings = get_settings()
    eligible = [
        m
        for m in markets
        if m.status == MarketStatus.OPEN
        and m.accepting_orders
        and horizons_for(m.expected_resolution_time)
        and (m.volume_24h or Decimal("0")) >= settings.min_volume_24h_usd
    ]
    eligible.sort(key=orderbook_priority)

    by_event: dict[str, list[NormalizedMarket]] = {}
    for market in markets:
        if market.platform_event_id and market.status == MarketStatus.OPEN:
            by_event.setdefault(market.platform_event_id, []).append(market)

    chosen: dict[str, NormalizedMarket] = {}
    for market in eligible:
        if len(chosen) >= limit:
            break
        group = by_event.get(market.platform_event_id or "", [market])
        # Only pull in the whole event if the remaining budget can cover it;
        # otherwise take the single market and leave the event for a later cycle.
        if len(chosen) + len(group) <= limit:
            for sibling in group:
                chosen.setdefault(sibling.platform_market_id, sibling)
        else:
            chosen.setdefault(market.platform_market_id, market)

    return list(chosen.values())[:limit]


async def fetch_platform(
    provider: KalshiProvider | PolymarketProvider,
    *,
    market_limit: int,
    orderbook_limit: int,
) -> tuple[list[NormalizedMarket], dict[str, OrderBook], list[Any], list[str]]:
    """Fetch markets, events and prioritised orderbooks for one venue."""
    errors: list[str] = []
    markets: list[NormalizedMarket] = []
    events: list[Any] = []
    books: dict[str, OrderBook] = {}

    if isinstance(provider, KalshiProvider):
        # Nested-event discovery first: it yields broad category coverage and the
        # authoritative outcome count per event, which the multi-outcome arbitrage
        # scanner needs. The flat time-ordered listing is then merged in to pick up
        # near-dated markets whose events fell outside the event page budget.
        try:
            events, markets = await provider.list_events_with_markets(
                limit=min(600, market_limit)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider.platform.value} nested events: {exc}")

        try:
            flat = await provider.list_active_markets(limit=market_limit)
            seen = {m.platform_market_id for m in markets}
            markets.extend(m for m in flat if m.platform_market_id not in seen)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider.platform.value} markets: {exc}")

        if not markets:
            log.error("no markets fetched for %s", provider.platform.value)
            return markets, books, events, errors
    else:
        try:
            markets = await provider.list_active_markets(limit=market_limit)
        except Exception as exc:  # noqa: BLE001 - one venue failing must not kill the run
            errors.append(f"{provider.platform.value} markets: {exc}")
            log.error("market fetch failed for %s: %s", provider.platform.value, exc)
            return markets, books, events, errors

        try:
            events = await provider.list_events(limit=min(300, market_limit))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider.platform.value} events: {exc}")

    targets = select_for_orderbooks(markets, orderbook_limit)
    if targets:
        try:
            raw_books = await provider.get_orderbooks(targets)
            books = {
                f"{provider.platform.value}:{ticker}": book
                for ticker, book in raw_books.items()
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider.platform.value} orderbooks: {exc}")
            log.error("orderbook fetch failed for %s: %s", provider.platform.value, exc)

    return markets, books, events, errors


async def run_ingest(
    session,  # noqa: ANN001 - sqlalchemy Session
    *,
    market_limit: int | None = None,
    orderbook_limit: int | None = None,
    platforms: Sequence[Platform] | None = None,
    include_trades: bool = True,
) -> IngestReport:
    """Full ingest cycle across both venues, persisted in one transaction."""
    settings = get_settings()
    market_limit = market_limit or settings.ingest_market_limit
    orderbook_limit = orderbook_limit or settings.ingest_orderbook_limit
    wanted = set(platforms or (Platform.KALSHI, Platform.POLYMARKET))

    report = IngestReport()
    providers: list[KalshiProvider | PolymarketProvider] = []
    if Platform.KALSHI in wanted:
        providers.append(KalshiProvider())
    if Platform.POLYMARKET in wanted:
        providers.append(PolymarketProvider())

    try:
        # Both venues are fetched concurrently; they share no rate-limit budget.
        results = await asyncio.gather(
            *(
                fetch_platform(
                    p,
                    market_limit=market_limit // max(1, len(providers)),
                    orderbook_limit=orderbook_limit // max(1, len(providers)),
                )
                for p in providers
            ),
            return_exceptions=True,
        )

        all_markets: list[NormalizedMarket] = []
        all_books: dict[str, OrderBook] = {}
        all_events: list[Any] = []

        for provider, result in zip(providers, results):
            if isinstance(result, Exception):
                report.errors.append(f"{provider.platform.value}: {result}")
                continue
            markets, books, events, errors = result
            all_markets.extend(markets)
            all_books.update(books)
            all_events.extend(events)
            report.errors.extend(errors)
            report.by_platform[provider.platform.value] = len(markets)
            report.provider_stats[provider.platform.value] = provider.stats

        report.markets_fetched = len(all_markets)
        report.events_written = upsert_events(session, all_events)
        id_map = upsert_markets(session, all_markets)
        report.markets_written = len(id_map)
        report.orderbooks_written = store_orderbooks(
            session, all_books, id_map, max_levels=settings.orderbook_depth
        )

        if include_trades:
            report.trades_written = await _ingest_trades(
                session, providers, all_markets, id_map
            )

    finally:
        await asyncio.gather(*(p.aclose() for p in providers), return_exceptions=True)

    log.info(
        "ingest complete: %d markets, %d orderbooks, %d trades, %d errors",
        report.markets_written, report.orderbooks_written,
        report.trades_written, len(report.errors),
    )
    return report


async def _ingest_trades(
    session,  # noqa: ANN001
    providers: Sequence[KalshiProvider | PolymarketProvider],
    markets: Sequence[NormalizedMarket],
    id_map: dict[str, int],
    *,
    per_platform: int = 25,
) -> int:
    """Pull recent prints for the most active markets only.

    Trades are used to sanity-check that quotes are live, not to drive pricing, so a
    sample of the busiest markets is sufficient and keeps the request budget small.
    """
    written = 0
    for provider in providers:
        candidates = [
            m for m in markets
            if m.platform == provider.platform and (m.volume_24h or Decimal("0")) > 0
        ]
        candidates.sort(key=lambda m: -(m.volume_24h or Decimal("0")))
        top = candidates[:per_platform]
        if not top:
            continue
        results = await asyncio.gather(
            *(provider.get_trades(m, limit=50) for m in top), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                continue
            written += store_trades(session, result, id_map)
    return written


async def refresh_orderbooks(
    session,  # noqa: ANN001
    *,
    limit: int | None = None,
) -> IngestReport:
    """Re-fetch books for already-ingested markets without re-listing markets.

    Used by the high-frequency scheduler job: market metadata changes slowly, but
    a quote from five minutes ago is not an executable price.
    """
    from sqlalchemy import select

    from ..db_models import Market

    settings = get_settings()
    limit = limit or settings.ingest_orderbook_limit
    report = IngestReport()

    rows = session.scalars(
        select(Market).where(
            Market.status == MarketStatus.OPEN.value,
            Market.accepting_orders.is_(True),
            Market.expected_resolution_time.is_not(None),
        )
    ).all()

    by_platform: dict[Platform, list[NormalizedMarket]] = {}
    for row in rows:
        market = _market_from_row(row)
        if not horizons_for(market.expected_resolution_time):
            continue
        by_platform.setdefault(market.platform, []).append(market)

    providers: dict[Platform, Any] = {}
    if Platform.KALSHI in by_platform:
        providers[Platform.KALSHI] = KalshiProvider()
    if Platform.POLYMARKET in by_platform:
        providers[Platform.POLYMARKET] = PolymarketProvider()

    try:
        id_map = load_market_id_map(session)
        share = max(1, limit // max(1, len(providers)))
        for platform, provider in providers.items():
            targets = select_for_orderbooks(by_platform[platform], share)
            if not targets:
                continue
            try:
                raw = await provider.get_orderbooks(targets)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{platform.value} orderbooks: {exc}")
                continue
            books = {f"{platform.value}:{t}": b for t, b in raw.items()}
            report.orderbooks_written += store_orderbooks(
                session, books, id_map, max_levels=settings.orderbook_depth
            )
            report.provider_stats[platform.value] = provider.stats
    finally:
        await asyncio.gather(
            *(p.aclose() for p in providers.values()), return_exceptions=True
        )

    return report


def _market_from_row(row) -> NormalizedMarket:  # noqa: ANN001
    """Rehydrate the provider-shaped model from a persisted row."""
    from pmvl_shared.enums import Category, DataProvenance

    return NormalizedMarket(
        platform=Platform(row.platform),
        platform_market_id=row.platform_market_id,
        platform_event_id=row.platform_event_id,
        series_ticker=row.series_ticker,
        title=row.title,
        subtitle=row.subtitle,
        normalized_title=row.normalized_title,
        description=row.description,
        category=Category(row.category),
        outcomes=row.outcomes or ["Yes", "No"],
        yes_token_id=row.yes_token_id,
        no_token_id=row.no_token_id,
        condition_id=row.condition_id,
        open_time=row.open_time,
        close_time=row.close_time,
        event_occurrence_time=row.event_occurrence_time,
        expected_resolution_time=row.expected_resolution_time,
        settlement_source=row.settlement_source,
        settlement_rules_raw=row.settlement_rules_raw,
        settlement_rules_normalized=row.settlement_rules_normalized,
        resolution_hash=row.resolution_hash,
        status=MarketStatus(row.status),
        result=row.result,
        accepting_orders=row.accepting_orders,
        market_type=row.market_type,
        strike_type=row.strike_type,
        floor_strike=row.floor_strike,
        cap_strike=row.cap_strike,
        tick_size=row.tick_size,
        price_level_structure=row.price_level_structure,
        min_order_size=row.min_order_size,
        fee_rate=row.fee_rate,
        maker_fee_rate=row.maker_fee_rate,
        fee_type=row.fee_type,
        best_yes_bid=row.best_yes_bid,
        best_yes_ask=row.best_yes_ask,
        best_no_bid=row.best_no_bid,
        best_no_ask=row.best_no_ask,
        spread=row.spread,
        orderbook_depth_usd=row.orderbook_depth_usd,
        volume_24h=row.volume_24h,
        total_volume=row.total_volume,
        open_interest=row.open_interest,
        last_trade_price=row.last_trade_price,
        liquidity_usd=row.liquidity_usd,
        quote_observed_at=row.quote_observed_at,
        provenance=DataProvenance(row.provenance),
    )
