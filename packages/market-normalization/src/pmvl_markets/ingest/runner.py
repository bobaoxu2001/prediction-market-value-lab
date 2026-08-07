"""Ingest orchestration: fetch from both venues, normalize, persist.

Orderbook fetching is the expensive step, so markets are prioritised before books
are requested: a market with no volume and no quote cannot produce a ranked
opportunity, and spending the rate-limit budget on it starves the ones that can.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category, MarketStatus, Platform
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


#: Categories with an independent probability model behind them. A market outside
#: these can still be ingested and browsed, but it can never clear the independence
#: gate on its own, so it should not outrank a modellable market for a book fetch.
MODELLABLE_CATEGORIES = frozenset({Category.CRYPTO, Category.WEATHER, Category.FINANCE})


def modellable_categories() -> frozenset[Category]:
    """The modellable set for the current configuration.

    Sports joins it only when ``SPORTS_MODEL_ENABLED`` is on. The scoring reserve
    is for books that can produce a *score*, and while the sports model is off no
    sports contract can - so promoting them would spend the scarce half of the
    budget on markets that are then rejected for having no independent prior, which
    is precisely the mistake ``orderbook_priority`` was written to avoid.

    Their books are still fetched: the coverage reserve ranks by volume alone, and
    single-game contracts are among the highest-volume on the venue.
    """
    from pmvl_shared.config import get_settings

    if getattr(get_settings(), "sports_model_enabled", False):
        return MODELLABLE_CATEGORIES | {Category.SPORTS}
    return MODELLABLE_CATEGORIES

#: No single series may take more than this share of one cycle's orderbook budget.
MAX_SERIES_BUDGET_SHARE = 0.2
#: ...but every series that qualifies at all gets at least this many, so a small
#: board (a 12-strike temperature event) is never squeezed out entirely.
MIN_PER_SERIES_BOOKS = 14


def orderbook_priority(
    market: NormalizedMarket, *, now: datetime | None = None
) -> tuple[int, int, Decimal]:
    """Sort key deciding which markets get a book fetch, for the scoring reserve.

    Ordered by (modellable, horizon bucket, 24h volume).

    Modellability leads because the orderbook budget is the scarce resource and a
    book is only *useful* if the market can also be scored. Ranking purely by volume
    spent the entire budget on sports and politics - markets that are then rejected
    for having no independent prior, so the fetch bought nothing.

    That reasoning still holds for the *scoring* half of the budget, and this key
    still governs it. It stopped being the whole story once cost analysis existed:
    see ``coverage_priority``.
    """
    modellable = 0 if market.category in modellable_categories() else 1
    horizons = horizons_for(market.expected_resolution_time, now=now)
    bucket = {"24h": 0, "7d": 1, "30d": 2}.get(horizons[0] if horizons else "", 3)
    return (modellable, bucket, -(market.volume_24h or Decimal("0")))


def coverage_priority(market: NormalizedMarket) -> Decimal:
    """Sort key for the coverage reserve: traded volume, and nothing else.

    What a book buys is no longer only a score. Execution cost - fees at size, the
    fee-rounding rule, depth impact, transfer and capital cost - is computed without
    any probability estimate, so it has an answer for every market with a book,
    including the politics, sports and macro contracts the models decline.

    Neither modellability nor time to resolution appears here, deliberately:

    * Modellability decides whether a market can be *scored*, not whether knowing
      its cost is worth a request. Under a modellability-led key the contracts
      people most often look up sorted behind every crypto and weather market and
      the budget ran out first, so their coverage was decided by arithmetic rather
      than by judgement.
    * Time to resolution is already priced, as capital cost. A contract resolving
      in two years is not a worse cost question than one resolving tomorrow - if
      anything it is a more interesting one, because the capital drag is the
      dominant term.

    Volume is the proxy for "somebody will look this up", which is the only thing
    this reserve is trying to buy.
    """
    return -(market.volume_24h or Decimal("0"))


def select_for_orderbooks(
    markets: Sequence[NormalizedMarket],
    limit: int,
    *,
    now: datetime | None = None,
) -> list[NormalizedMarket]:
    """Pick the markets worth spending an orderbook request on.

    The budget is split into **two reserves**, filled in order.

    *Scoring reserve* (the majority) keeps the original behaviour exactly: ordered
    by ``orderbook_priority``, so a market that can be scored outranks one that
    cannot. Everything downstream of a probability estimate depends on this.

    *Coverage reserve* is then filled by ``coverage_priority`` - liquidity alone,
    modellability ignored. A book now buys execution-cost analysis as well as a
    score, and cost needs no probability, so it answers for exactly the contracts
    the models decline. Under a single modellability-led key those contracts were
    starved by construction rather than by judgement: they sorted behind every
    crypto and weather market and the budget ran out first.

    Splitting rather than reweighting is deliberate. A blended sort key would make
    the two products compete on one scale, and the losing side would be whichever
    happened to have thinner volume that day. A reserve is a floor: the scanner
    cannot be starved by liquid sports markets, and the cost surface cannot be
    starved by the scanner.

    Selection stays **event-aware** within each reserve. Once a market is chosen,
    its event siblings are pulled in with it, because multi-outcome arbitrage can
    only be assessed on a complete basket: pricing four buckets of an eleven-bucket
    temperature event is not a partial answer, it is no answer at all, and the
    scanner will (correctly) refuse to evaluate it.
    """
    settings = get_settings()

    # Tradeable at all, and liquid enough that its book means something. Common to
    # both reserves.
    tradeable = [
        m
        for m in markets
        if m.status == MarketStatus.OPEN
        and m.accepting_orders
        and (m.volume_24h or Decimal("0")) >= settings.min_volume_24h_usd
    ]

    # The scoring reserve additionally requires the market to resolve inside the
    # ranking horizon, because a recommendation it could produce would have to be
    # held that long.
    #
    # The coverage reserve does NOT, and that is the single largest effect of this
    # function. On the live universe the 30-day window is by far the tightest
    # filter here: of 11,838 open markets only 339 resolve inside it. Everything
    # else was ingested and never had a book fetched, including the most heavily
    # traded contracts on either venue - Fed rate decisions carrying millions in
    # 24h volume - purely because they settle further out than the scanner cares
    # about. Cost is computable for all of them.
    scoring_eligible = [
        m for m in tradeable if horizons_for(m.expected_resolution_time, now=now)
    ]

    by_event: dict[str, list[NormalizedMarket]] = {}
    for market in markets:
        if market.platform_event_id and market.status == MarketStatus.OPEN:
            by_event.setdefault(market.platform_event_id, []).append(market)

    # Cap how much of the budget any one series may take.
    #
    # A pure priority sort lets the deepest series monopolise everything: Kalshi's
    # daily Bitcoin board publishes hundreds of strikes that are all liquid and all
    # in the 24h bucket, so it consumed the entire allocation and every weather
    # market got zero coverage. Spending the whole budget on 125 near-identical BTC
    # strikes is also poor value in its own right - they are one bet, not 125.
    #
    # Measured against the whole budget, not each reserve: the cap is about how
    # much of one cycle's fetching goes to near-identical contracts, and that
    # concern does not change because the budget is now filled in two passes.
    per_series_cap = max(MIN_PER_SERIES_BOOKS, int(limit * MAX_SERIES_BUDGET_SHARE))
    series_used: dict[str, int] = {}
    chosen: dict[str, NormalizedMarket] = {}

    def series_key(market: NormalizedMarket) -> str:
        return market.series_ticker or market.platform_event_id or market.platform_market_id

    def take(market: NormalizedMarket) -> None:
        key = series_key(market)
        if market.platform_market_id in chosen:
            return
        chosen[market.platform_market_id] = market
        series_used[key] = series_used.get(key, 0) + 1

    def fill(ordered: list[NormalizedMarket], budget: int) -> None:
        """Fill up to ``budget`` total selections from ``ordered``."""
        for market in ordered:
            if len(chosen) >= budget:
                break
            if market.platform_market_id in chosen:
                continue
            key = series_key(market)
            if series_used.get(key, 0) >= per_series_cap:
                continue

            # Event-aware: multi-outcome arbitrage can only be assessed on a
            # complete basket, so an event is taken whole or not at all when the
            # budget allows.
            group = by_event.get(market.platform_event_id or "", [market])
            if len(chosen) + len(group) <= budget and (
                series_used.get(key, 0) + len(group) <= per_series_cap
            ):
                for sibling in group:
                    take(sibling)
            else:
                take(market)

    # The coverage reserve is expressed as the share of the budget it may not be
    # squeezed below, so the scoring pass is capped at the remainder.
    coverage_reserve = int(limit * settings.orderbook_coverage_share)
    by_score = sorted(scoring_eligible, key=lambda m: orderbook_priority(m, now=now))

    if coverage_reserve <= 0:
        # A real off switch. Without this branch a share of 0 still ran the
        # volume pass over the whole budget, so the setting documented as
        # "restores the previous allocation" would have changed it instead - and
        # an operator reaching for that switch is doing so precisely because
        # something has gone wrong and they want the old behaviour back.
        fill(by_score, limit)
    else:
        fill(by_score, max(0, limit - coverage_reserve))
        fill(sorted(tradeable, key=coverage_priority), limit)

    # Any leftover budget goes back to the scoring order, cap ignored. Reached when
    # the per-series cap blocked both passes - a universe of few, deep series - and
    # an unspent request is worth less than a duplicate-ish book.
    if len(chosen) < limit:
        for market in by_score:
            if len(chosen) >= limit:
                break
            take(market)

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
        # Modellable series FIRST. General discovery is ordered by close time and
        # event volume and never reaches the daily crypto and weather boards, which
        # are the only Kalshi markets with an independent model behind them. Fetching
        # them last-or-never is why the ranker had nothing it was allowed to
        # recommend.
        try:
            markets = await provider.list_modellable_markets()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider.platform.value} modellable series: {exc}")

        # Nested-event discovery: broad category coverage plus the authoritative
        # outcome count per event, which the multi-outcome scanner requires.
        try:
            events, nested = await provider.list_events_with_markets(
                limit=min(600, market_limit)
            )
            seen = {m.platform_market_id for m in markets}
            markets.extend(m for m in nested if m.platform_market_id not in seen)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider.platform.value} nested events: {exc}")

        # Flat time-ordered listing picks up near-dated markets whose events fell
        # outside the event page budget.
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
