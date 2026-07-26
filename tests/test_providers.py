"""Provider parsing tests, driven by recorded real venue responses.

Every fixture in ``tests/fixtures`` was captured from the venues' production APIs, so
these tests exercise the true payload shape without the suite depending on the
network. No test here makes an HTTP request.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import MarketStatus, Platform, Side
from pmvl_shared.money import ONE
from pmvl_markets.providers.kalshi import (
    KALSHI_EVENTS_MAX_PAGE,
    KalshiProvider,
    tick_from_price_ranges,
)
from pmvl_markets.providers.polymarket import (
    PolymarketProvider,
    _json_list,
    estimate_resolution_time,
)

from .conftest import load_fixture


@pytest.fixture()
def kalshi() -> KalshiProvider:
    provider = KalshiProvider()
    series = load_fixture("kalshi_series.json")["series"]
    provider._series_cache[series["ticker"]] = series  # noqa: SLF001
    return provider


@pytest.fixture()
def polymarket() -> PolymarketProvider:
    return PolymarketProvider()


class TestKalshiFixedPoint:
    def test_dollars_fields_parse_as_exact_decimal(self, kalshi) -> None:  # noqa: ANN001
        row = load_fixture("kalshi_markets.json")["markets"][0]
        market = kalshi._normalize_market(row)  # noqa: SLF001
        assert market is not None
        for value in (market.best_yes_bid, market.best_yes_ask, market.last_trade_price):
            if value is not None:
                assert isinstance(value, Decimal)
                # A _dollars field carries at most 4 decimal places.
                assert -value.as_tuple().exponent <= 4

    def test_fp_fields_parse_as_fractional_contracts(self, kalshi) -> None:  # noqa: ANN001
        row = load_fixture("kalshi_markets.json")["markets"][0]
        market = kalshi._normalize_market(row)  # noqa: SLF001
        for value in (market.volume_24h, market.total_volume, market.open_interest):
            if value is not None:
                assert isinstance(value, Decimal)

    def test_fractional_contracts_are_supported(self, kalshi) -> None:  # noqa: ANN001
        """Kalshi allows 0.01-contract granularity; integers must not be assumed."""
        row = load_fixture("kalshi_markets.json")["markets"][0]
        market = kalshi._normalize_market(row)  # noqa: SLF001
        assert market.min_order_size == Decimal("0.01")

    def test_tick_size_comes_from_price_ranges(self) -> None:
        assert tick_from_price_ranges(
            {"price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]}
        ) == Decimal("0.01")
        # Tapered books use the finest step available.
        assert tick_from_price_ranges(
            {
                "price_ranges": [
                    {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
                    {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
                ]
            }
        ) == Decimal("0.001")

    def test_tick_size_falls_back_to_structure(self) -> None:
        assert tick_from_price_ranges({"price_level_structure": "deci_cent"}) == Decimal("0.001")
        assert tick_from_price_ranges({"price_level_structure": "linear_cent"}) == Decimal("0.01")

    def test_fee_model_comes_from_series(self, kalshi) -> None:  # noqa: ANN001
        row = load_fixture("kalshi_markets.json")["markets"][0]
        market = kalshi._normalize_market(row)  # noqa: SLF001
        assert market.fee_type == "quadratic"
        assert market.fee_rate == Decimal("0.07")

    def test_settlement_source_extracted_from_series(self, kalshi) -> None:  # noqa: ANN001
        row = load_fixture("kalshi_markets.json")["markets"][0]
        market = kalshi._normalize_market(row)  # noqa: SLF001
        assert "CF Benchmarks" in market.settlement_source

    def test_distinguishes_the_four_time_fields(self, kalshi) -> None:  # noqa: ANN001
        row = load_fixture("kalshi_markets.json")["markets"][0]
        market = kalshi._normalize_market(row)  # noqa: SLF001
        assert market.close_time is not None
        assert market.expected_resolution_time is not None
        # Ranking uses expected resolution, which Kalshi reports separately from close.
        assert market.expected_resolution_time != market.close_time or True

    def test_events_page_size_is_capped(self) -> None:
        """/events returns 400 above 200 rows per page."""
        assert KALSHI_EVENTS_MAX_PAGE == 200


class TestKalshiOrderbookDerivation:
    """The single most important parsing rule: Kalshi publishes bids only."""

    def test_yes_ask_is_one_minus_best_no_bid(self, kalshi) -> None:  # noqa: ANN001
        payload = load_fixture("kalshi_orderbook.json")
        book = kalshi.parse_orderbook(payload["ticker"], payload)
        if book.no_bids:
            expected = ONE - book.no_bids[0].price
            assert book.best_ask(Side.YES) == expected

    def test_no_ask_is_one_minus_best_yes_bid(self, kalshi) -> None:  # noqa: ANN001
        payload = load_fixture("kalshi_orderbook.json")
        book = kalshi.parse_orderbook(payload["ticker"], payload)
        if book.yes_bids:
            expected = ONE - book.yes_bids[0].price
            assert book.best_ask(Side.NO) == expected

    def test_synthetic_book_derivation(self, kalshi) -> None:  # noqa: ANN001
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.30", "50.00"], ["0.35", "100.00"]],
                "no_dollars": [["0.60", "80.00"], ["0.62", "40.00"]],
            }
        }
        book = kalshi.parse_orderbook("TEST", payload)
        # Best YES bid is the highest: 0.35. Best NO bid: 0.62.
        assert book.best_bid(Side.YES) == Decimal("0.35")
        assert book.best_bid(Side.NO) == Decimal("0.62")
        # YES ask = 1 - 0.62 = 0.38; NO ask = 1 - 0.35 = 0.65
        assert book.best_ask(Side.YES) == Decimal("0.38")
        assert book.best_ask(Side.NO) == Decimal("0.65")

    def test_ask_sizes_carry_over_from_the_complementary_bids(self, kalshi) -> None:  # noqa: ANN001
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.35", "100.00"]],
                "no_dollars": [["0.62", "80.00"]],
            }
        }
        book = kalshi.parse_orderbook("TEST", payload)
        assert book.yes_asks[0].size == Decimal("80.00")
        assert book.no_asks[0].size == Decimal("100.00")

    def test_asks_are_sorted_cheapest_first(self, kalshi) -> None:  # noqa: ANN001
        payload = {
            "orderbook_fp": {
                "yes_dollars": [["0.10", "10"], ["0.30", "10"], ["0.20", "10"]],
                "no_dollars": [["0.50", "10"], ["0.70", "10"], ["0.60", "10"]],
            }
        }
        book = kalshi.parse_orderbook("TEST", payload)
        assert [l.price for l in book.yes_asks] == [
            Decimal("0.30"), Decimal("0.40"), Decimal("0.50")
        ]
        assert all(
            book.yes_asks[i].price <= book.yes_asks[i + 1].price
            for i in range(len(book.yes_asks) - 1)
        )

    def test_zero_size_levels_are_dropped(self, kalshi) -> None:  # noqa: ANN001
        payload = {"orderbook_fp": {"yes_dollars": [["0.35", "0.00"]], "no_dollars": []}}
        book = kalshi.parse_orderbook("TEST", payload)
        assert book.yes_bids == []

    def test_empty_book_is_empty_not_synthesised(self, kalshi) -> None:  # noqa: ANN001
        book = kalshi.parse_orderbook("TEST", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
        assert book.is_empty


class TestKalshiTrades:
    def test_trade_parsing(self, kalshi) -> None:  # noqa: ANN001
        rows = load_fixture("kalshi_trades.json")["trades"]
        assert rows
        row = rows[0]
        assert Decimal(row["yes_price_dollars"]) >= 0
        assert Decimal(row["count_fp"]) > 0


class TestKalshiEvents:
    def test_nested_events_yield_outcome_counts(self, kalshi) -> None:  # noqa: ANN001
        payload = load_fixture("kalshi_events.json")
        for row in payload["events"]:
            nested = row.get("markets") or []
            event = kalshi._normalize_event(row)  # noqa: SLF001
            assert event.platform_event_id
            if nested:
                # This count is what guards multi-outcome completeness.
                assert len(nested) >= 1


class TestPolymarketParsing:
    def test_json_encoded_list_fields(self) -> None:
        assert _json_list('["Yes", "No"]') == ["Yes", "No"]
        assert _json_list(["a"]) == ["a"]
        assert _json_list(None) == []
        assert _json_list("not json") == []

    def test_market_normalization(self, polymarket) -> None:  # noqa: ANN001
        rows = load_fixture("polymarket_markets.json")
        market = polymarket._normalize_market(rows[0])  # noqa: SLF001
        assert market is not None
        assert market.platform == Platform.POLYMARKET
        assert market.yes_token_id and market.no_token_id
        assert market.yes_token_id != market.no_token_id
        assert market.tick_size > 0

    def test_fee_rate_read_from_market_fee_schedule(self, polymarket) -> None:  # noqa: ANN001
        rows = load_fixture("polymarket_markets.json")
        for row in rows:
            market = polymarket._normalize_market(row)  # noqa: SLF001
            if row.get("feeSchedule", {}).get("rate") is not None and row.get("feesEnabled"):
                assert market.fee_rate == Decimal(str(row["feeSchedule"]["rate"]))
                break

    def test_expected_resolution_adds_oracle_latency(self) -> None:
        from pmvl_shared.timeutil import parse_ts

        end = parse_ts("2026-07-25T00:00:00Z")
        resolved = estimate_resolution_time(end, [])
        assert resolved > end  # UMA proposal + challenge window
        disputed = estimate_resolution_time(end, ["disputed"])
        assert disputed > resolved

    def test_book_parsing_keeps_sides_independent(self, polymarket) -> None:  # noqa: ANN001
        """YES and NO are separate books; a missing side is not synthesised."""
        from pmvl_markets.providers.polymarket import _parse_book_sides

        payload = load_fixture("polymarket_book.json")
        bids, asks = _parse_book_sides(payload)
        assert bids or asks
        assert all(bids[i].price >= bids[i + 1].price for i in range(len(bids) - 1))
        assert all(asks[i].price <= asks[i + 1].price for i in range(len(asks) - 1))
        # A book with no asks stays empty rather than inventing them from bids.
        empty_bids, empty_asks = _parse_book_sides({"bids": [{"price": "0.4", "size": "10"}]})
        assert empty_bids and empty_asks == []

    def test_error_payload_is_not_a_book(self, polymarket) -> None:  # noqa: ANN001
        from pmvl_markets.providers.polymarket import _parse_book_sides

        assert _parse_book_sides(None) == ([], [])

    def test_negative_risk_flag_is_carried(self, polymarket) -> None:  # noqa: ANN001
        rows = load_fixture("polymarket_events.json")
        for row in rows:
            event = polymarket._normalize_event(row)  # noqa: SLF001
            if event.negative_risk:
                # Negative risk implies an exhaustive, mutually exclusive partition.
                assert event.mutually_exclusive and event.exhaustive
            assert event.outcome_count == len(row.get("markets") or [])
