"""Filling in the event rows that markets reference but nobody fetched.

Event discovery is paged and capped; market ingestion is not. On live data 28,000
open Kalshi markets referenced an event ticker that resolved to no row. That is not a
cosmetic gap: the multi-outcome scanner refuses to price a basket whose venue-reported
outcome count is unknown - correctly, because "we do not know whether this set is
complete" must never read as "it is" - so every one of those events was silently
absent from that scanner.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from pmvl_shared.enums import Category, MarketStatus, Platform
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from pmvl_markets.db_models import Event, Market
from pmvl_markets.ingest.runner import unlinked_event_tickers
from pmvl_markets.providers.kalshi import KalshiProvider


def market(market_id: str, event_ticker: str, volume: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id=market_id,
        platform_event_id=event_ticker,
        title=f"{market_id} outcome",
        category=Category.SPORTS,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        expected_resolution_time=utcnow(),
        volume_24h=Decimal(volume),
    )


class TestFindingTheGaps:
    def test_reports_tickers_with_no_event_row(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.ingest.store import upsert_markets

        upsert_markets(clean_db, [
            market("KXA-1", "KXA", "100"),
            market("KXB-1", "KXB", "9000"),
        ])
        clean_db.flush()

        gaps = unlinked_event_tickers(clean_db)
        assert set(gaps[Platform.KALSHI]) == {"KXA", "KXB"}
        # Ranked by traded volume, because the cap means a cycle closes the most
        # valuable gaps rather than all of them.
        assert gaps[Platform.KALSHI][0] == "KXB"

    def test_a_linked_event_is_not_reported(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.ingest.store import upsert_events, upsert_markets

        from pmvl_shared.schemas import NormalizedEvent

        upsert_events(clean_db, [
            NormalizedEvent(
                platform=Platform.KALSHI,
                platform_event_id="KXA",
                title="Event A",
                outcome_count=3,
            )
        ])
        upsert_markets(clean_db, [market("KXA-1", "KXA", "100")])
        clean_db.flush()

        assert unlinked_event_tickers(clean_db).get(Platform.KALSHI, []) == []

    def test_the_per_cycle_cap_is_respected(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.ingest.store import upsert_markets

        upsert_markets(clean_db, [
            market(f"KX{i}-1", f"KX{i}", str(i)) for i in range(12)
        ])
        clean_db.flush()

        gaps = unlinked_event_tickers(clean_db, per_platform=5)
        assert len(gaps[Platform.KALSHI]) == 5
        # The five most traded, not an arbitrary five.
        assert gaps[Platform.KALSHI] == ["KX11", "KX10", "KX9", "KX8", "KX7"]

    def test_a_closed_market_is_not_worth_a_request(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.ingest.store import upsert_markets

        closed = market("KXA-1", "KXA", "100").model_copy(
            update={"status": MarketStatus.CLOSED}
        )
        upsert_markets(clean_db, [closed])
        clean_db.flush()

        assert unlinked_event_tickers(clean_db).get(Platform.KALSHI, []) == []


class TestFetchingThemByTicker:
    @pytest.mark.asyncio
    async def test_outcome_count_comes_from_the_nested_markets(self, monkeypatch) -> None:  # noqa: ANN001
        provider = KalshiProvider()
        payload = {
            "event": {
                "event_ticker": "KXNATO-99",
                "series_ticker": "KXNATO",
                "title": "Who will be the next Secretary General of NATO?",
                "category": "Politics",
                "mutually_exclusive": True,
                "markets": [
                    {"ticker": f"KXNATO-99-{i}", "status": "active"} for i in range(8)
                ],
            }
        }

        async def fake_get_json(path, **kwargs):  # noqa: ANN001, ANN202
            assert path == "/events/KXNATO-99"
            assert kwargs["params"]["with_nested_markets"] == "true"
            return payload

        monkeypatch.setattr(provider._client, "get_json", fake_get_json)  # noqa: SLF001
        events = await provider.get_events_by_ticker(["KXNATO-99"])

        assert len(events) == 1
        assert events[0].outcome_count == 8
        assert events[0].mutually_exclusive is True

    @pytest.mark.asyncio
    async def test_an_event_without_nested_markets_is_dropped(self, monkeypatch) -> None:  # noqa: ANN001
        """A row asserting zero outcomes is worse than no row at all.

        The scanner reads the count as authoritative, so a zero would turn "we could
        not establish this" into "this event has no outcomes".
        """
        provider = KalshiProvider()

        async def fake_get_json(path, **kwargs):  # noqa: ANN001, ANN202
            return {"event": {"event_ticker": "KXEMPTY-99", "title": "Empty", "markets": []}}

        monkeypatch.setattr(provider._client, "get_json", fake_get_json)  # noqa: SLF001
        assert await provider.get_events_by_ticker(["KXEMPTY-99"]) == []

    @pytest.mark.asyncio
    async def test_a_missing_event_does_not_fail_the_batch(self, monkeypatch) -> None:  # noqa: ANN001
        provider = KalshiProvider()

        async def fake_get_json(path, **kwargs):  # noqa: ANN001, ANN202
            if "GONE" in path:
                return None
            return {
                "event": {
                    "event_ticker": "KXOK-99",
                    "title": "Fine",
                    "markets": [{"ticker": "KXOK-99-A"}, {"ticker": "KXOK-99-B"}],
                }
            }

        monkeypatch.setattr(provider._client, "get_json", fake_get_json)  # noqa: SLF001
        events = await provider.get_events_by_ticker(["KXGONE-99", "KXOK-99"])
        assert [e.platform_event_id for e in events] == ["KXOK-99"]
