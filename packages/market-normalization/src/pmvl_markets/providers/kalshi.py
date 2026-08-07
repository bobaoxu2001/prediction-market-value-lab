"""Kalshi Trade API v2 provider.

Public market data needs no authentication. Venue specifics handled here:

* **Fixed point.** ``*_dollars`` fields are decimal *strings* with up to 4 dp;
  ``*_fp`` fields are fractional contract counts with 2 dp. Both are parsed as
  ``Decimal`` from the string, never via float.
* **Bid-only orderbook.** ``GET /markets/{ticker}/orderbook`` returns YES bids and NO
  bids only. Asks are derived from the complementary side:
  ``YES ask = $1 - best NO bid`` and ``NO ask = $1 - best YES bid``.
* **Tick size** varies per market (``linear_cent`` / ``deci_cent`` /
  ``tapered_deci_cent``); the authoritative intervals are in ``price_ranges``.
* **Fees** are ``ceil_to_cent(0.07 * multiplier * C * P * (1-P))`` for takers, with
  the multiplier and fee type published per *series*, which this provider caches.

Docs: https://docs.kalshi.com/getting_started/orderbook_responses
      https://docs.kalshi.com/getting_started/fixed_point_migration
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category, DataProvenance, MarketStatus, Platform
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ONE, d_or_none, quantize_price
from pmvl_shared.schemas import (
    BookLevel,
    NormalizedEvent,
    NormalizedMarket,
    OrderBook,
    PricePoint,
    ResolutionInfo,
    TradeTick,
)
from pmvl_shared.timeutil import parse_ts, utcnow

from ..normalize.rules import KALSHI_STRIKE_COMPARATORS, normalize_rules
from ..normalize.text import extract_features, normalize_title
from .http import HttpClient, ProviderError

log = get_logger(__name__)

#: Kalshi ``category`` fragments mapped onto our controlled vocabulary, matched as
#: **substrings** and tried longest-first.
#:
#: Exact-match lookup silently failed on Kalshi's real label "Climate and Weather",
#: dropping the entire NWS temperature board into ``other``. That cost those markets
#: their orderbook-fetch priority and left the one category with a genuinely strong
#: independent model unscored. Substring matching survives that class of relabelling.
_CATEGORY_FRAGMENTS: tuple[tuple[str, Category], ...] = (
    ("science and technology", Category.TECH),
    ("climate and weather", Category.WEATHER),
    ("transportation", Category.OTHER),
    ("entertainment", Category.CULTURE),
    ("technology", Category.TECH),
    ("financials", Category.FINANCE),
    ("economics", Category.ECONOMICS),
    ("elections", Category.POLITICS),
    ("companies", Category.FINANCE),
    ("politics", Category.POLITICS),
    ("climate", Category.WEATHER),
    ("weather", Category.WEATHER),
    ("culture", Category.CULTURE),
    ("finance", Category.FINANCE),
    ("health", Category.OTHER),
    ("crypto", Category.CRYPTO),
    ("sports", Category.SPORTS),
    ("world", Category.GEOPOLITICS),
)


def map_category(raw: str | None) -> Category:
    """Map a venue category label onto the controlled vocabulary."""
    text = (raw or "").strip().lower()
    if not text:
        return Category.OTHER
    for fragment, category in _CATEGORY_FRAGMENTS:
        if fragment in text:
            return category
    return Category.OTHER

_STATUS_MAP = {
    "active": MarketStatus.OPEN,
    "open": MarketStatus.OPEN,
    "initialized": MarketStatus.OPEN,
    "paused": MarketStatus.PAUSED,
    "closed": MarketStatus.CLOSED,
    "determined": MarketStatus.CLOSED,
    "finalized": MarketStatus.SETTLED,
    "settled": MarketStatus.SETTLED,
}

#: Default taker rate for ``quadratic`` fee series, per Kalshi's published schedule
#: ("$0.07 - $1.75 per 100 contracts" == 0.07 * C * P * (1-P), ceiled to the cent).
KALSHI_TAKER_RATE = Decimal("0.07")
#: Maker rate for ``quadratic_with_maker_fees`` series ("$0.02 - $0.44 per 100").
KALSHI_MAKER_RATE = Decimal("0.0175")

#: Maximum rows per page accepted by /events. Larger values return 400 bad_request.
KALSHI_EVENTS_MAX_PAGE = 200

#: Series the platform has a genuine *independent* probability model for.
#:
#: General discovery is ordered by close time and by event volume, and in practice
#: returns thousands of politics/sports/culture markets while never surfacing the
#: daily crypto and weather boards - the only two categories with a real model behind
#: them. Fetching these explicitly is what makes it possible to produce a recommendation
#: at all; without it the independence gate correctly rejects everything.
#:
#: Deliberately a short, auditable list rather than a category sweep: adding a series
#: here is a claim that a model can price it, and that claim should be reviewed.
MODELLABLE_SERIES: tuple[str, ...] = (
    # Crypto daily/hourly settlement boards -> CryptoThresholdModel (Coinbase spot + RV)
    "KXBTCD", "KXBTC", "KXETHD", "KXETH", "KXSOLD", "KXXRPD", "KXDOGED",
    # NWS daily high-temperature boards -> WeatherThresholdModel (api.weather.gov)
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN",
    "KXHIGHLAX", "KXHIGHPHIL",
    # Equity index boards -> EquityIndexThresholdModel (Yahoo chart API, trading-time)
    "KXINXU", "KXINX", "KXNASDAQ100", "KXINXY",
)

#: Head-to-head single-game winner boards -> SportsBaseRateModel (ESPN records).
#:
#: Fetched unconditionally, and *not* conditional on ``SPORTS_MODEL_ENABLED``, for
#: two reasons that hold whether or not the model ever earns its keep:
#:
#: 1. These are among the highest-volume contracts on the venue, and execution cost
#:    is computable for them with no probability model at all. The coverage reserve
#:    in ``ingest.runner`` exists for exactly this case.
#: 2. The sports model cannot be *measured* until game markets have been ingested
#:    and have settled. Gating ingestion on the flag, and the flag on the
#:    measurement, would be a loop with no entry point.
#:
#: Leagues where a match can end level are excluded: the model declines them, and a
#: two-outcome record prior does not describe a three-outcome result.
HEAD_TO_HEAD_SPORTS_SERIES: tuple[str, ...] = (
    "KXMLBGAME", "KXNBAGAME", "KXWNBAGAME", "KXNFLGAME", "KXNHLGAME",
)

#: What ``list_modellable_markets`` fetches by default.
TARGETED_SERIES: tuple[str, ...] = MODELLABLE_SERIES + HEAD_TO_HEAD_SPORTS_SERIES


def _dollars(payload: dict[str, Any], key: str) -> Decimal | None:
    """Read a ``*_dollars`` fixed-point string field."""
    return d_or_none(payload.get(key))


def _fp(payload: dict[str, Any], key: str) -> Decimal | None:
    """Read a ``*_fp`` fractional-contract field (2 dp, min granularity 0.01)."""
    return d_or_none(payload.get(key))


def tick_from_price_ranges(payload: dict[str, Any]) -> Decimal:
    """Smallest step in ``price_ranges``.

    A market can be tapered (finer ticks in the tails). Using the *smallest* step is
    the conservative choice: it never claims a price is invalid when it is legal.
    """
    ranges = payload.get("price_ranges") or []
    steps = [d_or_none(r.get("step")) for r in ranges if isinstance(r, dict)]
    steps = [s for s in steps if s and s > 0]
    if steps:
        return min(steps)
    structure = payload.get("price_level_structure", "linear_cent")
    return Decimal("0.001") if "deci_cent" in str(structure) else Decimal("0.01")


class KalshiProvider:
    """Read-only adapter over the Kalshi Trade API v2."""

    platform = Platform.KALSHI

    def __init__(self, client: HttpClient | None = None) -> None:
        settings = get_settings()
        self._client = client or HttpClient(
            settings.kalshi_api_base,
            name="kalshi",
            # Comfortably inside the basic tier's read budget.
            rate_per_second=9.0,
            cache_ttl_seconds=20.0,
        )
        #: series ticker -> {fee_type, fee_multiplier, category, settlement_sources}
        self._series_cache: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def stats(self) -> dict[str, Any]:
        return {**self._client.stats, "series_cached": len(self._series_cache)}

    # ------------------------------------------------------------- pagination
    async def _paginate(
        self,
        path: str,
        *,
        key: str,
        params: dict[str, Any],
        limit: int,
        max_page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """Follow Kalshi's opaque cursor until ``limit`` rows or exhaustion.

        ``max_page_size`` differs per endpoint: ``/markets`` accepts up to 1000 but
        ``/events`` rejects anything above 200 with a bare ``400 bad_request``. That
        400 is not retryable, but the surrounding retry budget still gets consumed
        and the follow-up request is then rate limited - so the cap has to be correct
        per endpoint rather than discovered at runtime.

        Kalshi returns an empty ``cursor`` at the end, but has also been observed
        echoing the same cursor back; the seen-set guards against looping forever.
        """
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_size = min(max_page_size, max(1, limit))

        while len(out) < limit:
            page_params = {**params, "limit": min(page_size, limit - len(out))}
            if cursor:
                page_params["cursor"] = cursor
            data = await self._client.get_json(path, params=page_params, use_cache=False)
            if not data:
                break
            rows = data.get(key) or []
            out.extend(rows)
            cursor = data.get("cursor") or None
            if not cursor or cursor in seen_cursors or not rows:
                break
            seen_cursors.add(cursor)
        return out[:limit]

    # ----------------------------------------------------------------- series
    async def get_series(self, series_ticker: str) -> dict[str, Any] | None:
        """Fetch and cache series metadata (fee model, category, settlement sources)."""
        if not series_ticker:
            return None
        if series_ticker in self._series_cache:
            return self._series_cache[series_ticker]
        try:
            data = await self._client.get_json(f"/series/{series_ticker}", allow_404=True)
        except ProviderError as exc:
            log.debug("series %s unavailable: %s", series_ticker, exc)
            return None
        series = (data or {}).get("series")
        if series:
            self._series_cache[series_ticker] = series
        return series

    async def prefetch_series(self, tickers: Iterable[str]) -> None:
        """Warm the series cache concurrently before normalising a batch."""
        pending = {t for t in tickers if t and t not in self._series_cache}
        if not pending:
            return
        await asyncio.gather(*(self.get_series(t) for t in pending), return_exceptions=True)

    @staticmethod
    def series_ticker_from_event(event_ticker: str | None) -> str:
        """Kalshi event tickers are ``SERIES-SUFFIX``; the series is the head."""
        if not event_ticker:
            return ""
        return event_ticker.split("-", 1)[0]

    # ---------------------------------------------------------------- markets
    async def list_active_markets(
        self, *, limit: int = 500, max_close_days: int = 30
    ) -> list[NormalizedMarket]:
        """List open markets closing within ``max_close_days``.

        Two filters matter here and both are load-bearing:

        ``max_close_ts``
            Without it, ``/markets?status=open`` is dominated by Kalshi's
            auto-generated multivariate parlay markets, which have zero volume and
            would consume the entire page budget before reaching a tradeable market.
            Bounding by close time also scopes the fetch to exactly the 24h/7d/30d
            windows the platform ranks.

        Multivariate exclusion
            Markets carrying ``mve_collection_ticker`` are combinatorial parlays over
            other Kalshi markets. They have no independent settlement source, cannot
            be matched cross-platform, and their "price" is a derived joint
            probability - so they are excluded from the research universe.
        """
        max_close_ts = int(utcnow().timestamp() + max_close_days * 86400)
        rows = await self._paginate(
            "/markets",
            key="markets",
            params={"status": "open", "max_close_ts": max_close_ts},
            limit=limit,
        )
        rows = [r for r in rows if not r.get("mve_collection_ticker")]
        await self.prefetch_series(
            {self.series_ticker_from_event(r.get("event_ticker")) for r in rows}
        )
        markets = [self._normalize_market(r) for r in rows]
        return [m for m in markets if m is not None]

    async def list_series_by_category(self, category: str) -> list[str]:
        """Series tickers in one Kalshi category."""
        try:
            data = await self._client.get_json(
                "/series", params={"category": category}, allow_404=True
            )
        except ProviderError as exc:
            log.debug("series listing failed for %s: %s", category, exc)
            return []
        series = (data or {}).get("series") or []
        out = []
        for entry in series:
            if isinstance(entry, dict) and entry.get("ticker"):
                self._series_cache.setdefault(entry["ticker"], entry)
                out.append(entry["ticker"])
        return out

    async def list_modellable_markets(
        self,
        *,
        series: Sequence[str] = TARGETED_SERIES,
        per_series_limit: int = 300,
        max_close_days: int = 30,
    ) -> list[NormalizedMarket]:
        """Fetch every open market in the series the platform targets directly.

        See :data:`MODELLABLE_SERIES` and :data:`HEAD_TO_HEAD_SPORTS_SERIES`. One
        request per series, run concurrently, so the whole set costs roughly two
        dozen requests.
        """
        max_close_ts = int(utcnow().timestamp() + max_close_days * 86400)

        async def fetch(ticker: str) -> list[dict[str, Any]]:
            try:
                return await self._paginate(
                    "/markets",
                    key="markets",
                    params={
                        "status": "open",
                        "series_ticker": ticker,
                        "max_close_ts": max_close_ts,
                    },
                    limit=per_series_limit,
                )
            except ProviderError as exc:
                # A retired or renamed series must not fail the whole scan.
                log.debug("modellable series %s unavailable: %s", ticker, exc)
                return []

        batches = await asyncio.gather(*(fetch(t) for t in series), return_exceptions=True)
        rows: list[dict[str, Any]] = []
        for batch in batches:
            if isinstance(batch, list):
                rows.extend(r for r in batch if not r.get("mve_collection_ticker"))

        await self.prefetch_series(
            {self.series_ticker_from_event(r.get("event_ticker")) for r in rows}
        )
        markets = [self._normalize_market(r) for r in rows]
        return [m for m in markets if m is not None]

    async def list_events_with_markets(
        self,
        *,
        limit: int = 400,
        categories: Sequence[str] | None = None,
    ) -> tuple[list[NormalizedEvent], list[NormalizedMarket]]:
        """Discover events and their nested markets in one pass.

        Preferred over the flat ``/markets`` listing for three reasons:

        1. **Category coverage.** The flat listing is ordered by close time and is
           dominated by whatever expires soonest - in practice hundreds of golf and
           motorsport player props with no Polymarket counterpart. A scan built only
           from those finds no cross-platform matches and therefore no independent
           priors, so nothing can ever be recommended.
        2. **Efficiency.** Fanning out per series is not viable - Kalshi publishes
           over two thousand series in Politics alone. Nested events return up to a
           full page of markets per request.
        3. **Completeness.** The nested response gives the true number of outcomes in
           each event, which the multi-outcome arbitrage scanner requires before it
           can assert a basket is a complete set.
        """
        wanted = {c.lower() for c in categories} if categories else None
        rows = await self._paginate(
            "/events",
            key="events",
            params={"status": "open", "with_nested_markets": "true"},
            limit=limit,
            max_page_size=KALSHI_EVENTS_MAX_PAGE,
        )

        events: list[NormalizedEvent] = []
        markets: list[NormalizedMarket] = []
        market_rows: list[dict[str, Any]] = []

        for row in rows:
            if wanted and str(row.get("category", "")).lower() not in wanted:
                continue
            nested = [
                m for m in (row.get("markets") or [])
                if isinstance(m, dict) and not m.get("mve_collection_ticker")
            ]
            if not nested:
                continue
            event = self._normalize_event(row)
            event.outcome_count = len(nested)
            event.market_ids = [m.get("ticker", "") for m in nested]
            events.append(event)
            market_rows.extend(nested)

        await self.prefetch_series(
            {self.series_ticker_from_event(r.get("event_ticker")) for r in market_rows}
        )
        for row in market_rows:
            market = self._normalize_market(row)
            if market is not None:
                markets.append(market)

        return events, markets

    async def get_market(self, platform_market_id: str) -> NormalizedMarket | None:
        data = await self._client.get_json(f"/markets/{platform_market_id}", allow_404=True)
        row = (data or {}).get("market")
        if not row:
            return None
        await self.get_series(self.series_ticker_from_event(row.get("event_ticker")))
        return self._normalize_market(row)

    async def list_events(self, *, limit: int = 200) -> list[NormalizedEvent]:
        rows = await self._paginate(
            "/events",
            key="events",
            params={"status": "open", "with_nested_markets": "false"},
            limit=limit,
            max_page_size=KALSHI_EVENTS_MAX_PAGE,
        )
        return [self._normalize_event(r) for r in rows]

    def _normalize_event(self, row: dict[str, Any]) -> NormalizedEvent:
        title = row.get("title") or row.get("sub_title") or ""
        category = map_category(row.get("category"))
        return NormalizedEvent(
            platform=Platform.KALSHI,
            platform_event_id=row.get("event_ticker", ""),
            series_ticker=row.get("series_ticker") or self.series_ticker_from_event(
                row.get("event_ticker")
            ),
            title=title,
            normalized_title=normalize_title(title),
            category=category,
            close_time=parse_ts(row.get("strike_date")),
            # Kalshi events group related strikes; whether they are mutually
            # exclusive is series-dependent and asserted by the multi-outcome
            # scanner, not assumed here.
            mutually_exclusive=bool(row.get("mutually_exclusive", False)),
            exhaustive=False,
            raw=row,
        )

    def _normalize_market(self, row: dict[str, Any]) -> NormalizedMarket | None:
        ticker = row.get("ticker")
        if not ticker:
            return None

        event_ticker = row.get("event_ticker") or ""
        series_ticker = self.series_ticker_from_event(event_ticker)
        series = self._series_cache.get(series_ticker) or {}

        title = row.get("title") or ""
        subtitle = row.get("yes_sub_title") or row.get("subtitle") or ""
        rules_primary = row.get("rules_primary") or ""
        rules_secondary = row.get("rules_secondary") or ""
        rules_raw = "\n\n".join(p for p in (rules_primary, rules_secondary) if p)

        category = map_category(series.get("category"))

        settlement_sources = series.get("settlement_sources") or []
        settlement_source = ", ".join(
            s.get("name", "") for s in settlement_sources if isinstance(s, dict)
        )

        # Structured strike fields are authoritative; text extraction is the fallback.
        strike_type = row.get("strike_type") or ""
        floor_strike = d_or_none(row.get("floor_strike"))
        cap_strike = d_or_none(row.get("cap_strike"))
        explicit_threshold = floor_strike if floor_strike is not None else cap_strike

        # expected_expiration_time is when Kalshi expects to settle; expiration_time
        # is the outer bound if settlement data is delayed. Rank on the expectation.
        expected_resolution = parse_ts(row.get("expected_expiration_time")) or parse_ts(
            row.get("expiration_time")
        )
        close_time = parse_ts(row.get("close_time"))
        occurrence = parse_ts(row.get("occurrence_datetime"))

        features = extract_features(title, subtitle=subtitle, description=rules_raw)
        rules = normalize_rules(
            title=title,
            subtitle=subtitle,
            description=rules_raw,
            settlement_source=settlement_source,
            cutoff_time=occurrence or expected_resolution or close_time,
            explicit_threshold=explicit_threshold,
            explicit_comparator=KALSHI_STRIKE_COMPARATORS.get(strike_type, ""),
            has_structured_strike=bool(strike_type),
            features=features,
        )

        status = _STATUS_MAP.get(str(row.get("status", "")).lower(), MarketStatus.UNKNOWN)

        fee_type = str(series.get("fee_type") or "quadratic")
        fee_multiplier = d_or_none(series.get("fee_multiplier"))
        if fee_multiplier is None:
            fee_multiplier = ONE
        fee_rate = KALSHI_TAKER_RATE * fee_multiplier
        maker_rate = (
            KALSHI_MAKER_RATE * fee_multiplier
            if fee_type == "quadratic_with_maker_fees"
            else Decimal("0")
        )
        if fee_type == "flat":
            # Flat-fee series are rare; treat the multiplier as the per-contract fee
            # and let the fee model branch on fee_type rather than guessing here.
            fee_rate = fee_multiplier

        yes_bid = _dollars(row, "yes_bid_dollars")
        yes_ask = _dollars(row, "yes_ask_dollars")
        no_bid = _dollars(row, "no_bid_dollars")
        no_ask = _dollars(row, "no_ask_dollars")

        # An ask of exactly $1.00 with no size means "no offers", not "priced at par".
        if yes_ask is not None and yes_ask >= ONE and not _fp(row, "yes_ask_size_fp"):
            yes_ask = None
        spread = (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None

        volume_24h = _fp(row, "volume_24h_fp")
        total_volume = _fp(row, "volume_fp")
        last_price = _dollars(row, "last_price_dollars")

        return NormalizedMarket(
            platform=Platform.KALSHI,
            platform_market_id=ticker,
            platform_event_id=event_ticker,
            series_ticker=series_ticker,
            title=title,
            subtitle=subtitle,
            normalized_title=normalize_title(title, subtitle=subtitle),
            description=rules_raw,
            category=category,
            outcomes=["Yes", "No"],
            open_time=parse_ts(row.get("open_time")),
            close_time=close_time,
            event_occurrence_time=occurrence,
            expected_resolution_time=expected_resolution,
            source_timezone="UTC",
            settlement_source=settlement_source,
            settlement_rules_raw=rules_raw,
            settlement_rules_normalized=rules.summary,
            resolution_hash=rules.resolution_hash,
            status=status,
            result=(row.get("result") or None),
            accepting_orders=status == MarketStatus.OPEN,
            market_type=row.get("market_type") or "binary",
            strike_type=strike_type or None,
            floor_strike=floor_strike,
            cap_strike=cap_strike,
            tick_size=tick_from_price_ranges(row),
            price_level_structure=str(row.get("price_level_structure") or "linear_cent"),
            #: Kalshi supports fractional contracts down to 0.01.
            min_order_size=Decimal("0.01"),
            fee_rate=fee_rate,
            maker_fee_rate=maker_rate,
            fee_type=fee_type,
            best_yes_bid=yes_bid,
            best_yes_ask=yes_ask,
            best_no_bid=no_bid,
            best_no_ask=no_ask,
            spread=spread,
            #: Kalshi's liquidity_dollars is resting notional on the book.
            liquidity_usd=_dollars(row, "liquidity_dollars"),
            volume_24h=volume_24h,
            total_volume=total_volume,
            open_interest=_fp(row, "open_interest_fp"),
            last_trade_price=last_price,
            quote_observed_at=parse_ts(row.get("updated_time")) or utcnow(),
            provenance=DataProvenance.LIVE,
            raw=row,
        )

    # -------------------------------------------------------------- orderbook
    async def get_orderbook(self, market: NormalizedMarket) -> OrderBook | None:
        settings = get_settings()
        data = await self._client.get_json(
            f"/markets/{market.platform_market_id}/orderbook",
            params={"depth": settings.orderbook_depth},
            use_cache=False,
            allow_404=True,
        )
        if not data:
            return None
        return self.parse_orderbook(market.platform_market_id, data)

    async def get_orderbooks(
        self, markets: list[NormalizedMarket]
    ) -> dict[str, OrderBook]:
        """Fetch books for many markets, using the batch endpoint only when it works.

        The batch endpoint is validated rather than trusted. Given a comma-joined
        ``tickers`` value it has been observed responding ``200`` with a *single*
        empty book keyed by the joined string, instead of erroring. Accepting that
        response would record every market in the chunk as having no liquidity, which
        silently suppresses real opportunities - a far worse failure than a slower
        fetch. Any entry whose ticker was not requested invalidates the whole chunk
        and triggers the per-market fallback.
        """
        settings = get_settings()
        out: dict[str, OrderBook] = {}
        batch_size = 20

        for i in range(0, len(markets), batch_size):
            chunk = markets[i : i + batch_size]
            requested = {m.platform_market_id for m in chunk}
            parsed: dict[str, OrderBook] = {}

            if len(chunk) > 1:
                try:
                    data = await self._client.get_json(
                        "/markets/orderbooks",
                        params={
                            "tickers": ",".join(sorted(requested)),
                            "depth": settings.orderbook_depth,
                        },
                        use_cache=False,
                        allow_404=True,
                    )
                except ProviderError as exc:
                    log.debug("batch orderbook failed, falling back: %s", exc)
                    data = None

                entries = (data or {}).get("orderbooks")
                if isinstance(entries, list) and entries:
                    for entry in entries:
                        ticker = entry.get("ticker") or entry.get("market_ticker")
                        if ticker in requested:
                            parsed[ticker] = self.parse_orderbook(ticker, entry)
                    if len(parsed) != len(requested):
                        log.debug(
                            "batch orderbook returned %d/%d valid tickers; using fallback",
                            len(parsed), len(requested),
                        )
                        parsed = {}

            if parsed:
                out.update(parsed)
                continue

            results = await asyncio.gather(
                *(self.get_orderbook(m) for m in chunk), return_exceptions=True
            )
            for market, book in zip(chunk, results):
                if isinstance(book, OrderBook):
                    out[market.platform_market_id] = book
                elif isinstance(book, Exception):
                    log.debug("orderbook error for %s: %s", market.platform_market_id, book)
        return out

    def parse_orderbook(self, ticker: str, payload: dict[str, Any]) -> OrderBook:
        """Convert Kalshi's bid-only book into a two-sided normalized book.

        Kalshi returns ``orderbook_fp.yes_dollars`` and ``orderbook_fp.no_dollars``,
        each a list of ``[price, size]`` **bids** sorted ascending by price. Per the
        docs, a YES bid at X is a NO ask at $1-X, so:

            YES asks = {(1 - no_bid_price, no_bid_size)}
            NO  asks = {(1 - yes_bid_price, yes_bid_size)}

        Sizes carry over unchanged: the resting NO bid *is* the YES liquidity.
        """
        book = payload.get("orderbook_fp") or payload.get("orderbook") or payload
        yes_raw = book.get("yes_dollars") or book.get("yes") or []
        no_raw = book.get("no_dollars") or book.get("no") or []

        yes_bids = _levels_from(yes_raw)
        no_bids = _levels_from(no_raw)

        yes_asks = [
            BookLevel(price=quantize_price(ONE - lvl.price), size=lvl.size)
            for lvl in no_bids
            if ONE - lvl.price > 0
        ]
        no_asks = [
            BookLevel(price=quantize_price(ONE - lvl.price), size=lvl.size)
            for lvl in yes_bids
            if ONE - lvl.price > 0
        ]

        # Bids descend (best first); asks ascend (cheapest first).
        yes_bids.sort(key=lambda l: l.price, reverse=True)
        no_bids.sort(key=lambda l: l.price, reverse=True)
        yes_asks.sort(key=lambda l: l.price)
        no_asks.sort(key=lambda l: l.price)

        return OrderBook(
            platform=Platform.KALSHI,
            platform_market_id=ticker,
            observed_at=utcnow(),
            source_timestamp=parse_ts(payload.get("timestamp")),
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
            provenance=DataProvenance.LIVE,
            raw=payload,
        )

    # ----------------------------------------------------------------- trades
    async def get_trades(
        self, market: NormalizedMarket, *, limit: int = 100
    ) -> list[TradeTick]:
        rows = await self._paginate(
            "/markets/trades",
            key="trades",
            params={"ticker": market.platform_market_id},
            limit=limit,
        )
        out: list[TradeTick] = []
        for row in rows:
            traded_at = parse_ts(row.get("created_time"))
            price = d_or_none(row.get("yes_price_dollars"))
            size = d_or_none(row.get("count_fp"))
            if traded_at is None or price is None or size is None:
                continue
            out.append(
                TradeTick(
                    platform=Platform.KALSHI,
                    platform_trade_id=str(row.get("trade_id", "")),
                    platform_market_id=row.get("ticker", market.platform_market_id),
                    traded_at=traded_at,
                    price=price,
                    size=size,
                    taker_side=str(row.get("taker_side", "")),
                )
            )
        return out

    # ---------------------------------------------------------------- history
    async def get_price_history(
        self,
        market: NormalizedMarket,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval_minutes: int = 60,
    ) -> list[PricePoint]:
        """Candlesticks.

        NOTE: candles are OHLC summaries, **not** executable quotes. Callers that
        feed backtests must tag the resulting data quality as ``CANDLE``.
        """
        end = end or utcnow()
        start = start or datetime.fromtimestamp(end.timestamp() - 7 * 86400, tz=end.tzinfo)
        series = market.series_ticker or self.series_ticker_from_event(market.platform_event_id)
        if not series:
            return []
        try:
            data = await self._client.get_json(
                f"/series/{series}/markets/{market.platform_market_id}/candlesticks",
                params={
                    "start_ts": int(start.timestamp()),
                    "end_ts": int(end.timestamp()),
                    "period_interval": max(1, interval_minutes),
                },
                allow_404=True,
            )
        except ProviderError as exc:
            log.debug("candlesticks unavailable for %s: %s", market.platform_market_id, exc)
            return []
        if not data:
            return []

        points: list[PricePoint] = []
        for candle in data.get("candlesticks") or []:
            ts = parse_ts(candle.get("end_period_ts") or candle.get("ts"))
            price_block = candle.get("price") or {}
            close = (
                d_or_none(price_block.get("close_dollars"))
                or d_or_none(price_block.get("mean_dollars"))
                or d_or_none((candle.get("yes_bid") or {}).get("close_dollars"))
            )
            if ts and close is not None:
                points.append(PricePoint(timestamp=ts, price=close))
        points.sort(key=lambda p: p.timestamp)
        return points

    async def get_historical_cutoff(self) -> datetime | None:
        """Boundary before which data must come from the historical endpoints."""
        try:
            data = await self._client.get_json("/historical/cutoff_timestamps", allow_404=True)
        except ProviderError:
            return None
        if not data:
            return None
        return parse_ts(
            data.get("markets_cutoff_ts")
            or data.get("cutoff_ts")
            or data.get("trades_cutoff_ts")
        )

    async def list_historical_markets(
        self, *, limit: int = 500, min_close_ts: int | None = None
    ) -> list[dict[str, Any]]:
        """Archived markets, used to seed settlement history for backtests."""
        params: dict[str, Any] = {}
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        try:
            return await self._paginate(
                "/historical/markets", key="markets", params=params, limit=limit
            )
        except ProviderError as exc:
            log.info("historical markets unavailable: %s", exc)
            return []

    # ------------------------------------------------------------ resolution
    async def get_resolution(self, market: NormalizedMarket) -> ResolutionInfo | None:
        data = await self._client.get_json(
            f"/markets/{market.platform_market_id}", use_cache=False, allow_404=True
        )
        row = (data or {}).get("market")
        if not row:
            return None
        status = str(row.get("status", "")).lower()
        result = (row.get("result") or "").lower()
        resolved = status in {"finalized", "settled"} and result in {"yes", "no", "void", ""}
        if not resolved or not result:
            return None

        yes_payout = {"yes": ONE, "no": D(0), "void": D(0)}.get(result)
        return ResolutionInfo(
            platform=Platform.KALSHI,
            platform_market_id=market.platform_market_id,
            resolved=True,
            result=result,
            yes_payout=yes_payout,
            settled_at=parse_ts(row.get("settled_time") or row.get("close_time")),
            settlement_source=market.settlement_source,
            disputed=False,
            raw=row,
        )


def _levels_from(raw: Any) -> list[BookLevel]:
    """Parse ``[[price, size], ...]`` where both entries are fixed-point strings."""
    levels: list[BookLevel] = []
    if not isinstance(raw, list):
        return levels
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        price = d_or_none(entry[0])
        size = d_or_none(entry[1])
        if price is None or size is None or size <= 0 or price <= 0:
            continue
        levels.append(BookLevel(price=quantize_price(price), size=size))
    return levels
