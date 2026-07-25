"""The provider contract every venue adapter implements."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pmvl_shared.enums import Platform
from pmvl_shared.schemas import (
    NormalizedEvent,
    NormalizedMarket,
    OrderBook,
    PricePoint,
    ResolutionInfo,
    TradeTick,
)


@runtime_checkable
class PredictionMarketProvider(Protocol):
    """Uniform read interface over a prediction-market venue.

    Deliberately read-only. Order placement is *not* part of this protocol: a future
    execution service gets its own interface so that research code cannot trade even
    by accident.
    """

    platform: Platform

    async def list_active_markets(self, *, limit: int = 500) -> list[NormalizedMarket]: ...

    async def list_events(self, *, limit: int = 200) -> list[NormalizedEvent]: ...

    async def get_market(self, platform_market_id: str) -> NormalizedMarket | None: ...

    async def get_orderbook(self, market: NormalizedMarket) -> OrderBook | None: ...

    async def get_orderbooks(
        self, markets: list[NormalizedMarket]
    ) -> dict[str, OrderBook]: ...

    async def get_price_history(
        self,
        market: NormalizedMarket,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval_minutes: int = 60,
    ) -> list[PricePoint]: ...

    async def get_trades(
        self, market: NormalizedMarket, *, limit: int = 100
    ) -> list[TradeTick]: ...

    async def get_resolution(self, market: NormalizedMarket) -> ResolutionInfo | None: ...

    async def aclose(self) -> None: ...


class ProviderStats(dict):
    """Free-form counters a provider exposes to /system."""

    def merge(self, other: dict[str, Any], prefix: str = "") -> None:
        for k, v in other.items():
            self[f"{prefix}{k}"] = v
