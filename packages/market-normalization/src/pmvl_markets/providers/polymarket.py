"""Polymarket provider spanning the three public APIs.

Each API is wrapped by its own client because they have different hosts, rate limits
and failure modes:

* **Gamma** (``gamma-api``) - market/event discovery, rules text, fee schedule,
  negative-risk flags, tick size. The system of record for *what a market is*.
* **CLOB** (``clob``) - orderbooks, midpoint, spread, price history, keyed by
  ``token_id`` (one per outcome), not by market id.
* **Data** (``data-api``) - public analytics: trades, holders, open interest.

Venue specifics:

* A binary market has two ERC-1155 outcome tokens listed in ``clobTokenIds``, index
  0 = YES, index 1 = NO. Each has its **own** independent orderbook; YES and NO asks
  are therefore *not* algebraically linked as they are on Kalshi.
* Fees come straight from the market's ``feeSchedule.rate`` where present, so the
  per-category table in the docs never has to be hardcoded.
* ``negRisk`` events share collateral across mutually exclusive outcomes, which
  changes the complete-set arithmetic for multi-outcome arbitrage.

Docs: https://docs.polymarket.com/api-reference/market-data/
      https://docs.polymarket.com/trading/fees
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

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

from ..normalize.rules import normalize_rules
from ..normalize.text import extract_features, normalize_title
from .http import HttpClient, ProviderError

log = get_logger(__name__)

_TAG_CATEGORY_MAP = {
    "crypto": Category.CRYPTO,
    "bitcoin": Category.CRYPTO,
    "ethereum": Category.CRYPTO,
    "sports": Category.SPORTS,
    "nfl": Category.SPORTS,
    "nba": Category.SPORTS,
    "mlb": Category.SPORTS,
    "soccer": Category.SPORTS,
    "politics": Category.POLITICS,
    "elections": Category.POLITICS,
    "us-politics": Category.POLITICS,
    "geopolitics": Category.GEOPOLITICS,
    "world": Category.GEOPOLITICS,
    "economics": Category.ECONOMICS,
    "economy": Category.ECONOMICS,
    "fed": Category.ECONOMICS,
    "inflation": Category.ECONOMICS,
    "finance": Category.FINANCE,
    "stocks": Category.FINANCE,
    "business": Category.FINANCE,
    "tech": Category.TECH,
    "ai": Category.TECH,
    "science": Category.TECH,
    "culture": Category.CULTURE,
    "pop-culture": Category.CULTURE,
    "entertainment": Category.CULTURE,
    "weather": Category.WEATHER,
    "climate": Category.WEATHER,
    "mentions": Category.MENTIONS,
}

#: Fallback taker rates by category, used only when a market omits ``feeSchedule``.
#: Source: https://docs.polymarket.com/trading/fees
_FALLBACK_FEE_RATES = {
    Category.CRYPTO: Decimal("0.07"),
    Category.SPORTS: Decimal("0.05"),
    Category.ECONOMICS: Decimal("0.05"),
    Category.CULTURE: Decimal("0.05"),
    Category.WEATHER: Decimal("0.05"),
    Category.OTHER: Decimal("0.05"),
    Category.FINANCE: Decimal("0.04"),
    Category.POLITICS: Decimal("0.04"),
    Category.MENTIONS: Decimal("0.04"),
    Category.TECH: Decimal("0.04"),
    Category.GEOPOLITICS: Decimal("0"),
}


def _json_list(value: Any) -> list[Any]:
    """Gamma returns several list fields as JSON-encoded *strings*."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


class PolymarketProvider:
    """Read-only adapter over Polymarket's Gamma, CLOB and Data APIs."""

    platform = Platform.POLYMARKET

    def __init__(
        self,
        gamma: HttpClient | None = None,
        clob: HttpClient | None = None,
        data: HttpClient | None = None,
    ) -> None:
        settings = get_settings()
        self._gamma = gamma or HttpClient(
            settings.polymarket_gamma_base, name="polymarket-gamma",
            rate_per_second=8.0, cache_ttl_seconds=20.0,
        )
        self._clob = clob or HttpClient(
            settings.polymarket_clob_base, name="polymarket-clob",
            rate_per_second=10.0, cache_ttl_seconds=0.0,
        )
        self._data = data or HttpClient(
            settings.polymarket_data_base, name="polymarket-data",
            rate_per_second=6.0, cache_ttl_seconds=30.0,
        )

    async def aclose(self) -> None:
        await asyncio.gather(
            self._gamma.aclose(), self._clob.aclose(), self._data.aclose(),
            return_exceptions=True,
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            **{f"gamma_{k}": v for k, v in self._gamma.stats.items()},
            **{f"clob_{k}": v for k, v in self._clob.stats.items()},
            **{f"data_{k}": v for k, v in self._data.stats.items()},
        }

    # ---------------------------------------------------------------- markets
    async def list_active_markets(self, *, limit: int = 500) -> list[NormalizedMarket]:
        """Page Gamma by offset, newest-volume first.

        Filtered to ``closed=false`` and ``active=true``; ``acceptingOrders`` is
        checked per market during normalization because a market can be active but
        paused for orders.
        """
        rows: list[dict[str, Any]] = []
        #: Gamma caps a page at 100 regardless of a larger requested limit, so the
        #: loop must page by offset rather than asking for everything at once.
        page = 100
        offset = 0
        while len(rows) < limit:
            batch = await self._gamma.get_json(
                "/markets",
                params={
                    "limit": min(page, limit - len(rows)),
                    "offset": offset,
                    "closed": "false",
                    "active": "true",
                    "order": "volume24hr",
                    "ascending": "false",
                },
                use_cache=False,
            )
            if not batch or not isinstance(batch, list):
                break
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += len(batch)

        markets = [self._normalize_market(r) for r in rows[:limit]]
        return [m for m in markets if m is not None]

    async def get_market(self, platform_market_id: str) -> NormalizedMarket | None:
        row = await self._gamma.get_json(f"/markets/{platform_market_id}", allow_404=True)
        if not row:
            return None
        if isinstance(row, list):
            row = row[0] if row else None
        return self._normalize_market(row) if row else None

    async def list_events(self, *, limit: int = 200) -> list[NormalizedEvent]:
        rows = await self._gamma.get_json(
            "/events",
            params={
                "limit": limit, "closed": "false", "active": "true",
                "order": "volume24hr", "ascending": "false",
            },
            use_cache=False,
        )
        if not isinstance(rows, list):
            return []
        return [self._normalize_event(r) for r in rows]

    def _normalize_event(self, row: dict[str, Any]) -> NormalizedEvent:
        title = row.get("title") or ""
        markets = row.get("markets") or []
        neg_risk = bool(row.get("negRisk") or row.get("negRiskAugmented"))
        return NormalizedEvent(
            platform=Platform.POLYMARKET,
            platform_event_id=str(row.get("id", "")),
            title=title,
            normalized_title=normalize_title(title),
            category=self._category_from_tags(row.get("tags"), title),
            close_time=parse_ts(row.get("endDate")),
            negative_risk=neg_risk,
            # A negative-risk event is by construction mutually exclusive AND
            # exhaustive: exactly one outcome pays out.
            mutually_exclusive=neg_risk,
            exhaustive=neg_risk,
            outcome_count=len(markets),
            market_ids=[str(m.get("id")) for m in markets if isinstance(m, dict) and m.get("id")],
            raw=row,
        )

    @staticmethod
    def _category_from_tags(tags: Any, title: str = "") -> Category:
        for tag in tags or []:
            slug = (tag.get("slug") if isinstance(tag, dict) else str(tag)) or ""
            hit = _TAG_CATEGORY_MAP.get(slug.lower())
            if hit:
                return hit
        lowered = title.lower()
        for keyword, category in _TAG_CATEGORY_MAP.items():
            if keyword in lowered:
                return category
        return Category.OTHER

    def _normalize_market(self, row: dict[str, Any]) -> NormalizedMarket | None:
        market_id = row.get("id")
        if not market_id:
            return None

        question = row.get("question") or row.get("title") or ""
        description = row.get("description") or ""
        outcomes = [str(o) for o in _json_list(row.get("outcomes"))] or ["Yes", "No"]
        token_ids = [str(t) for t in _json_list(row.get("clobTokenIds"))]

        events = row.get("events") or []
        event = events[0] if events and isinstance(events[0], dict) else {}
        category = self._category_from_tags(
            event.get("tags") or row.get("tags"), f"{question} {event.get('title', '')}"
        )

        # Fee rate: prefer the market's own schedule over the category fallback.
        fee_schedule = row.get("feeSchedule") or {}
        fee_rate = d_or_none(fee_schedule.get("rate"))
        if fee_rate is None:
            fee_rate = _FALLBACK_FEE_RATES.get(category, Decimal("0.05"))
        if row.get("feesEnabled") is False:
            fee_rate = Decimal("0")

        resolution_source = row.get("resolutionSource") or ""
        end_date = parse_ts(row.get("endDate"))
        features = extract_features(question, description=description)
        rules = normalize_rules(
            title=question,
            description=description,
            settlement_source=resolution_source or description,
            cutoff_time=end_date,
            features=features,
        )

        closed = bool(row.get("closed"))
        active = bool(row.get("active", True))
        accepting = bool(row.get("acceptingOrders", True))
        uma_statuses = _json_list(row.get("umaResolutionStatuses"))
        if closed:
            status = MarketStatus.SETTLED if uma_statuses else MarketStatus.CLOSED
        elif not active:
            status = MarketStatus.PAUSED
        else:
            status = MarketStatus.OPEN

        best_bid = d_or_none(row.get("bestBid"))
        best_ask = d_or_none(row.get("bestAsk"))
        # Gamma's bestBid/bestAsk describe the YES token. The NO token has its own
        # book; these complements are a placeholder refreshed by get_orderbook().
        no_bid = quantize_price(ONE - best_ask) if best_ask is not None else None
        no_ask = quantize_price(ONE - best_bid) if best_bid is not None else None

        outcome_prices = [d_or_none(p) for p in _json_list(row.get("outcomePrices"))]
        last_price = d_or_none(row.get("lastTradePrice")) or (
            outcome_prices[0] if outcome_prices else None
        )

        tick = d_or_none(row.get("orderPriceMinTickSize")) or Decimal("0.01")

        return NormalizedMarket(
            platform=Platform.POLYMARKET,
            platform_market_id=str(market_id),
            platform_event_id=str(event.get("id")) if event.get("id") else None,
            title=question,
            subtitle=row.get("groupItemTitle") or "",
            normalized_title=normalize_title(question, subtitle=row.get("groupItemTitle") or ""),
            description=description,
            category=category,
            outcomes=outcomes,
            yes_token_id=token_ids[0] if len(token_ids) > 0 else None,
            no_token_id=token_ids[1] if len(token_ids) > 1 else None,
            condition_id=row.get("conditionId"),
            open_time=parse_ts(row.get("startDate")),
            close_time=end_date,
            # Polymarket publishes one date; UMA resolution follows the event by a
            # dispute window, so expected resolution is not simply endDate. The
            # ingest layer applies the oracle lag - see estimate_resolution_time().
            expected_resolution_time=estimate_resolution_time(end_date, uma_statuses),
            source_timezone="UTC",
            settlement_source=resolution_source or "UMA optimistic oracle",
            settlement_rules_raw=description,
            settlement_rules_normalized=rules.summary,
            resolution_hash=rules.resolution_hash,
            status=status,
            result=None,
            accepting_orders=accepting and not closed,
            market_type="binary" if len(outcomes) == 2 else "categorical",
            tick_size=tick,
            price_level_structure="decimal",
            min_order_size=d_or_none(row.get("orderMinSize")) or Decimal("1"),
            fee_rate=fee_rate,
            maker_fee_rate=Decimal("0"),  # "Makers are never charged fees."
            fee_type=str(row.get("feeType") or "general_fees"),
            best_yes_bid=best_bid,
            best_yes_ask=best_ask,
            best_no_bid=no_bid,
            best_no_ask=no_ask,
            spread=d_or_none(row.get("spread")),
            volume_24h=d_or_none(row.get("volume24hr")),
            total_volume=d_or_none(row.get("volumeNum")) or d_or_none(row.get("volume")),
            liquidity_usd=d_or_none(row.get("liquidityNum")) or d_or_none(row.get("liquidity")),
            last_trade_price=last_price,
            quote_observed_at=parse_ts(row.get("updatedAt")) or utcnow(),
            negative_risk=bool(row.get("negRisk")),
            provenance=DataProvenance.LIVE,
            raw=row,
        )

    # -------------------------------------------------------------- orderbook
    async def get_orderbook(self, market: NormalizedMarket) -> OrderBook | None:
        """Fetch both outcome books and merge into one normalized book.

        YES and NO are independent order books on Polymarket, so both are fetched.
        A missing book for one side is normal for thin markets and is represented as
        an empty level list rather than a synthesised complement - inventing the
        other side would fabricate liquidity that cannot be hit.
        """
        if not market.yes_token_id:
            return None

        yes_book, no_book = await asyncio.gather(
            self._fetch_book(market.yes_token_id),
            self._fetch_book(market.no_token_id) if market.no_token_id else _none(),
            return_exceptions=True,
        )
        yes_book = yes_book if isinstance(yes_book, dict) else None
        no_book = no_book if isinstance(no_book, dict) else None
        if yes_book is None and no_book is None:
            return None

        yes_bids, yes_asks = _parse_book_sides(yes_book)
        no_bids, no_asks = _parse_book_sides(no_book)

        return OrderBook(
            platform=Platform.POLYMARKET,
            platform_market_id=market.platform_market_id,
            observed_at=utcnow(),
            source_timestamp=parse_ts((yes_book or no_book or {}).get("timestamp")),
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
            provenance=DataProvenance.LIVE,
            raw={"yes": yes_book, "no": no_book},
        )

    async def _fetch_book(self, token_id: str | None) -> dict[str, Any] | None:
        if not token_id:
            return None
        try:
            data = await self._clob.get_json(
                "/book", params={"token_id": token_id}, use_cache=False, allow_404=True
            )
        except ProviderError as exc:
            log.debug("clob book failed for %s: %s", token_id, exc)
            return None
        # The CLOB returns 200 with {"error": "..."} when a token has no book.
        if isinstance(data, dict) and data.get("error"):
            return None
        return data if isinstance(data, dict) else None

    async def get_orderbooks(self, markets: list[NormalizedMarket]) -> dict[str, OrderBook]:
        """Concurrent per-market fetches, bounded by the shared HTTP semaphore."""
        results = await asyncio.gather(
            *(self.get_orderbook(m) for m in markets), return_exceptions=True
        )
        out: dict[str, OrderBook] = {}
        for market, book in zip(markets, results):
            if isinstance(book, OrderBook):
                out[market.platform_market_id] = book
            elif isinstance(book, Exception):
                log.debug("orderbook error for %s: %s", market.platform_market_id, book)
        return out

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        data = await self._clob.get_json(
            "/midpoint", params={"token_id": token_id}, allow_404=True
        )
        if not isinstance(data, dict) or data.get("error"):
            return None
        return d_or_none(data.get("mid"))

    async def get_spread(self, token_id: str) -> Decimal | None:
        data = await self._clob.get_json(
            "/spread", params={"token_id": token_id}, allow_404=True
        )
        if not isinstance(data, dict) or data.get("error"):
            return None
        return d_or_none(data.get("spread"))

    # ---------------------------------------------------------------- history
    async def get_price_history(
        self,
        market: NormalizedMarket,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval_minutes: int = 60,
    ) -> list[PricePoint]:
        """YES-token mid history from the CLOB.

        These are historical *midpoints*, not executable asks. Backtests consuming
        them must record data quality accordingly.
        """
        if not market.yes_token_id:
            return []
        params: dict[str, Any] = {
            "market": market.yes_token_id,
            "fidelity": max(1, interval_minutes),
        }
        if start and end:
            params["startTs"] = int(start.timestamp())
            params["endTs"] = int(end.timestamp())
        else:
            params["interval"] = "1w"

        try:
            data = await self._clob.get_json("/prices-history", params=params, allow_404=True)
        except ProviderError as exc:
            log.debug("prices-history failed for %s: %s", market.platform_market_id, exc)
            return []
        if not isinstance(data, dict):
            return []

        points: list[PricePoint] = []
        for entry in data.get("history") or []:
            ts = parse_ts(entry.get("t"))
            price = d_or_none(entry.get("p"))
            if ts and price is not None:
                points.append(PricePoint(timestamp=ts, price=price))
        points.sort(key=lambda p: p.timestamp)
        return points

    async def get_trades(
        self, market: NormalizedMarket, *, limit: int = 100
    ) -> list[TradeTick]:
        """Public trade prints from the Data API."""
        if not market.condition_id:
            return []
        try:
            rows = await self._data.get_json(
                "/trades",
                params={"market": market.condition_id, "limit": limit, "takerOnly": "true"},
                allow_404=True,
            )
        except ProviderError as exc:
            log.debug("data trades failed for %s: %s", market.platform_market_id, exc)
            return []
        if not isinstance(rows, list):
            return []

        out: list[TradeTick] = []
        for row in rows:
            traded_at = parse_ts(row.get("timestamp"))
            price = d_or_none(row.get("price"))
            size = d_or_none(row.get("size"))
            if traded_at is None or price is None or size is None:
                continue
            out.append(
                TradeTick(
                    platform=Platform.POLYMARKET,
                    platform_trade_id=str(
                        row.get("transactionHash") or f"{market.platform_market_id}-{traded_at}"
                    ),
                    platform_market_id=market.platform_market_id,
                    traded_at=traded_at,
                    price=price,
                    size=size,
                    taker_side=str(row.get("side", "")).lower(),
                )
            )
        return out

    # ------------------------------------------------------------ resolution
    async def get_resolution(self, market: NormalizedMarket) -> ResolutionInfo | None:
        row = await self._gamma.get_json(
            f"/markets/{market.platform_market_id}", use_cache=False, allow_404=True
        )
        if isinstance(row, list):
            row = row[0] if row else None
        if not isinstance(row, dict) or not row.get("closed"):
            return None

        prices = [d_or_none(p) for p in _json_list(row.get("outcomePrices"))]
        if not prices or prices[0] is None:
            return None

        yes_payout = prices[0]
        # A resolved binary market settles at exactly 1 or 0; 0.5 is a documented
        # 50-50 resolution. Anything else means it is closed but not yet resolved.
        if yes_payout >= Decimal("0.99"):
            result, payout = "yes", ONE
        elif yes_payout <= Decimal("0.01"):
            result, payout = "no", D(0)
        elif abs(yes_payout - Decimal("0.5")) < Decimal("0.01"):
            result, payout = "fifty_fifty", Decimal("0.5")
        else:
            return None

        statuses = _json_list(row.get("umaResolutionStatuses"))
        disputed = any("disput" in str(s).lower() for s in statuses)

        return ResolutionInfo(
            platform=Platform.POLYMARKET,
            platform_market_id=market.platform_market_id,
            resolved=True,
            result=result,
            yes_payout=payout,
            settled_at=parse_ts(row.get("updatedAt")) or parse_ts(row.get("endDate")),
            settlement_source=row.get("resolutionSource") or "UMA optimistic oracle",
            disputed=disputed,
            raw=row,
        )


async def _none() -> None:
    return None


def _parse_book_sides(book: dict[str, Any] | None) -> tuple[list[BookLevel], list[BookLevel]]:
    """Split a CLOB book payload into (bids desc, asks asc)."""
    if not book:
        return [], []
    bids = _levels_from(book.get("bids"))
    asks = _levels_from(book.get("asks"))
    bids.sort(key=lambda l: l.price, reverse=True)
    asks.sort(key=lambda l: l.price)
    return bids, asks


def _levels_from(raw: Any) -> list[BookLevel]:
    levels: list[BookLevel] = []
    if not isinstance(raw, list):
        return levels
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        price = d_or_none(entry.get("price"))
        size = d_or_none(entry.get("size"))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        levels.append(BookLevel(price=quantize_price(price), size=size))
    return levels


def estimate_resolution_time(
    end_date: datetime | None, uma_statuses: list[Any] | None = None
) -> datetime | None:
    """Expected settlement time = event end + UMA oracle latency.

    Polymarket exposes only ``endDate`` (when the question's window closes). Payout
    follows an optimistic-oracle proposal plus a challenge period. Treating
    ``endDate`` as the resolution time would put markets into the 24h bucket that
    cannot actually pay out for another day, so a conservative 2-hour lag is added.
    Markets already in dispute get a longer allowance.
    """
    if end_date is None:
        return None
    if uma_statuses and any("disput" in str(s).lower() for s in uma_statuses):
        return end_date + timedelta(hours=48)
    return end_date + timedelta(hours=2)
