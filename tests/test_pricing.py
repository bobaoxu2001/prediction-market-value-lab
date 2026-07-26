"""Unit tests for fees, Decimal precision, VWAP and expected value."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import D, ceil_cent, quantize_price, snap_to_tick
from pmvl_markets.pricing.execution import (
    build_cost_breakdown,
    fractional_kelly,
    gross_expected_profit,
    net_ev,
    net_roi,
    price_at_sizes,
)
from pmvl_markets.pricing.fees import (
    fee_per_contract,
    fee_rounding_cost,
    kalshi_maker_fee,
    kalshi_taker_fee,
    polymarket_taker_fee,
    taker_fee,
)
from pmvl_markets.pricing.orderbook import (
    available_size,
    complete_set_cost,
    depth_usd,
    effective_spread,
    executable_quote,
    max_complete_sets,
    walk_book,
)


class TestKalshiFees:
    """Validated against Kalshi's published schedule: $0.07-$1.75 per 100 contracts."""

    @pytest.mark.parametrize(
        "price,expected",
        [
            ("0.01", "0.07"),   # published minimum
            ("0.10", "0.63"),
            ("0.25", "1.32"),
            ("0.50", "1.75"),   # published maximum, at P=0.5
            ("0.75", "1.32"),
            ("0.90", "0.63"),
            ("0.99", "0.07"),
        ],
    )
    def test_taker_fee_matches_published_schedule(self, price: str, expected: str) -> None:
        assert kalshi_taker_fee(D(100), D(price)) == Decimal(expected)

    def test_fee_is_symmetric_around_fifty_cents(self) -> None:
        assert kalshi_taker_fee(D(100), D("0.30")) == kalshi_taker_fee(D(100), D("0.70"))

    def test_maker_fee_matches_published_range(self) -> None:
        # Published range for maker-fee series: $0.02 - $0.44 per 100 contracts.
        assert kalshi_maker_fee(D(100), D("0.50")) == Decimal("0.44")
        assert kalshi_maker_fee(D(100), D("0.01")) == Decimal("0.02")

    def test_fee_is_ceiled_to_the_cent(self) -> None:
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> ceiled to 0.02, not rounded to 0.02 by
        # coincidence: 0.0175 would round-half-up to 0.02 either way, so use a case
        # where ceiling and rounding differ.
        raw = D("0.07") * D(1) * D("0.2") * D("0.8")  # 0.0112
        assert raw < Decimal("0.02")
        assert kalshi_taker_fee(D(1), D("0.20")) == Decimal("0.02")

    def test_per_contract_fee_is_size_dependent(self) -> None:
        """The cent ceiling makes small orders disproportionately expensive."""
        one = fee_per_contract(Platform.KALSHI, D(1), D("0.50"), rate=D("0.07"))
        hundred = fee_per_contract(Platform.KALSHI, D(100), D("0.50"), rate=D("0.07"))
        assert one > hundred
        assert hundred == Decimal("0.0175")

    def test_fee_multiplier_scales_the_rate(self) -> None:
        half = kalshi_taker_fee(D(100), D("0.50"), rate=D("0.035"))
        assert half == Decimal("0.88")  # ceil(0.875)

    def test_zero_and_boundary_prices_incur_no_fee(self) -> None:
        assert kalshi_taker_fee(D(100), D("0")) == 0
        assert kalshi_taker_fee(D(100), D("1")) == 0
        assert kalshi_taker_fee(D(0), D("0.5")) == 0


class TestPolymarketFees:
    """Validated against the published per-100-share table (crypto rate 0.07)."""

    @pytest.mark.parametrize(
        "price,expected",
        [("0.01", "0.07"), ("0.10", "0.63"), ("0.25", "1.31"),
         ("0.50", "1.75"), ("0.75", "1.31"), ("0.99", "0.07")],
    )
    def test_taker_fee_matches_published_table(self, price: str, expected: str) -> None:
        fee = polymarket_taker_fee(D(100), D(price), rate=D("0.07"))
        assert fee.quantize(Decimal("0.01")) == Decimal(expected)

    def test_rounds_to_five_decimal_places(self) -> None:
        fee = polymarket_taker_fee(D(100), D("0.01"), rate=D("0.07"))
        assert fee == Decimal("0.06930")

    def test_below_minimum_rounds_to_zero(self) -> None:
        """'Anything smaller rounds to zero' per the published fee precision rule."""
        fee = polymarket_taker_fee(D("0.001"), D("0.0001"), rate=D("0.04"))
        assert fee == 0

    def test_zero_rate_market_is_free(self) -> None:
        assert polymarket_taker_fee(D(100), D("0.5"), rate=D("0")) == 0


class TestDecimalPrecision:
    def test_no_float_contamination(self) -> None:
        """D(float) must go through str, not binary float."""
        assert D(0.07) == Decimal("0.07")
        assert D(0.07) * D(3) == Decimal("0.21")

    def test_ceil_cent(self) -> None:
        assert ceil_cent(Decimal("0.0101")) == Decimal("0.02")
        assert ceil_cent(Decimal("0.01")) == Decimal("0.01")

    def test_snap_to_tick_never_flatters(self) -> None:
        # Asks snap up, bids snap down, so a snapped quote is never better than real.
        assert snap_to_tick(Decimal("0.4321"), Decimal("0.01"), direction="up") == Decimal("0.44")
        assert snap_to_tick(Decimal("0.4321"), Decimal("0.01"), direction="down") == Decimal("0.43")

    def test_quantize_price_handles_deci_cent_ticks(self) -> None:
        assert quantize_price(Decimal("0.0015")) == Decimal("0.0015")


class TestOrderbookWalking:
    def test_vwap_single_level(self, deep_book) -> None:  # noqa: ANN001
        quote = executable_quote(deep_book, Side.YES, D(50))
        assert quote is not None
        assert quote.average_price == Decimal("0.40")
        assert quote.fully_filled is True

    def test_vwap_across_multiple_levels(self, deep_book) -> None:  # noqa: ANN001
        # 100 @ 0.40 + 100 @ 0.42 = 82 / 200 = 0.41
        quote = executable_quote(deep_book, Side.YES, D(200))
        assert quote is not None
        assert quote.average_price == Decimal("0.4100")
        assert quote.levels_consumed == 2
        assert quote.worst_price == Decimal("0.42")

    def test_partial_fill_is_reported_not_hidden(self, deep_book) -> None:  # noqa: ANN001
        quote = executable_quote(deep_book, Side.YES, D(5000))
        assert quote is not None
        assert quote.fully_filled is False
        assert quote.filled_size == Decimal("800")  # 100+200+500

    def test_average_price_rises_with_size(self, deep_book) -> None:  # noqa: ANN001
        small = executable_quote(deep_book, Side.YES, D(10))
        large = executable_quote(deep_book, Side.YES, D(700))
        assert large.average_price > small.average_price

    def test_empty_book_returns_none(self, book_factory) -> None:  # noqa: ANN001
        assert executable_quote(book_factory(yes_asks=[]), Side.YES, D(10)) is None

    def test_no_side_uses_no_ladder(self, deep_book) -> None:  # noqa: ANN001
        quote = executable_quote(deep_book, Side.NO, D(100))
        assert quote.average_price == Decimal("0.58")

    def test_depth_and_available_size(self, deep_book) -> None:  # noqa: ANN001
        assert available_size(deep_book, Side.YES) == Decimal("800")
        assert depth_usd(deep_book, Side.YES) == Decimal("349")  # 40+84+225

    def test_effective_spread(self, deep_book) -> None:  # noqa: ANN001
        assert effective_spread(deep_book, Side.YES) == Decimal("0.01")

    def test_walk_book_rejects_nonpositive_size(self) -> None:
        assert walk_book([], D(0)) is None


class TestCompleteSet:
    def test_cost_of_a_set(self, deep_book) -> None:  # noqa: ANN001
        assert complete_set_cost(deep_book) == Decimal("0.98")

    def test_max_sets_stops_when_pair_reaches_one_dollar(self, book_factory) -> None:  # noqa: ANN001
        book = book_factory(
            yes_asks=[("0.45", "100"), ("0.50", "500")],
            no_asks=[("0.52", "80"), ("0.54", "500")],
        )
        sets, cost = max_complete_sets(book)
        # 80 sets at 0.97, then 20 at 0.45+0.54=0.99, then 0.50+0.54=1.04 stops.
        assert sets == Decimal("100")
        assert cost == Decimal("97.40")

    def test_no_sets_when_pair_exceeds_one(self, book_factory) -> None:  # noqa: ANN001
        book = book_factory(yes_asks=[("0.60", "100")], no_asks=[("0.55", "100")])
        sets, _ = max_complete_sets(book)
        assert sets == 0

    def test_missing_side_yields_nothing(self, book_factory) -> None:  # noqa: ANN001
        book = book_factory(yes_asks=[("0.40", "100")], no_asks=[])
        assert complete_set_cost(book) is None
        assert max_complete_sets(book)[0] == 0


class TestExpectedValue:
    def test_gross_edge(self) -> None:
        assert gross_expected_profit(D("0.60"), D("0.55")) == Decimal("0.0500")

    def test_net_ev_subtracts_full_cost(self) -> None:
        assert net_ev(D("0.60"), D("0.58")) == Decimal("0.0200")

    def test_negative_ev_stays_negative(self) -> None:
        assert net_ev(D("0.40"), D("0.55")) < 0

    def test_net_roi_uses_all_in_cost(self) -> None:
        assert net_roi(D("0.60"), D("0.50")) == Decimal("0.2000")

    def test_roi_zero_cost_guard(self) -> None:
        assert net_roi(D("0.60"), D("0")) == 0

    def test_kelly_positive_edge(self) -> None:
        # f* = (p - c) / (1 - c) = (0.6-0.5)/0.5 = 0.2, quarter Kelly -> 0.05
        assert fractional_kelly(D("0.60"), D("0.50"), fraction=D("0.25")) == Decimal("0.0500")

    def test_kelly_zero_on_no_edge(self) -> None:
        assert fractional_kelly(D("0.45"), D("0.50")) == 0

    def test_kelly_zero_at_or_above_par(self) -> None:
        assert fractional_kelly(D("0.99"), D("1.00")) == 0


class TestCostBreakdown:
    def test_all_components_present_and_summed(self, kalshi_market, deep_book) -> None:  # noqa: ANN001
        cost = build_cost_breakdown(kalshi_market, deep_book, Side.YES, D(100))
        assert cost is not None
        assert cost.entry_price == Decimal("0.4000")
        assert cost.platform_fee > 0
        assert cost.estimated_slippage > 0
        assert cost.transfer_cost == 0        # Kalshi has no bridge cost
        assert cost.total_cost > cost.entry_price

    def test_capital_cost_scales_with_time_to_resolution(self, kalshi_market, deep_book) -> None:  # noqa: ANN001
        """Capital cost is negligible intraday and material over a month.

        Over the fixture's 6-hour horizon, 5%/yr on a $0.40 contract is ~$0.0000137,
        which is below the centicent precision money is carried at, so it correctly
        rounds to zero. It must become visible at a 30-day horizon.
        """
        from datetime import timedelta

        from pmvl_shared.timeutil import utcnow

        short = build_cost_breakdown(kalshi_market, deep_book, Side.YES, D(100))
        assert short.capital_cost == 0

        long_dated = kalshi_market.model_copy(
            update={"expected_resolution_time": utcnow() + timedelta(days=30)}
        )
        long_cost = build_cost_breakdown(long_dated, deep_book, Side.YES, D(100))
        assert long_cost.capital_cost > 0
        assert long_cost.total_cost > short.total_cost

    def test_polymarket_carries_transfer_cost(self, polymarket_market, book_factory) -> None:  # noqa: ANN001
        book = book_factory(platform=Platform.POLYMARKET, yes_asks=[("0.40", "1000")])
        cost = build_cost_breakdown(polymarket_market, book, Side.YES, D(100))
        assert cost.transfer_cost > 0

    def test_transfer_cost_amortises_over_size(self, polymarket_market, book_factory) -> None:  # noqa: ANN001
        book = book_factory(platform=Platform.POLYMARKET, yes_asks=[("0.40", "10000")])
        small = build_cost_breakdown(polymarket_market, book, Side.YES, D(10))
        large = build_cost_breakdown(polymarket_market, book, Side.YES, D(1000))
        assert small.transfer_cost > large.transfer_cost

    def test_unfillable_book_returns_none(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        assert build_cost_breakdown(
            kalshi_market, book_factory(yes_asks=[]), Side.YES, D(10)
        ) is None

    def test_sized_quotes_cover_ten_fifty_hundred(self, kalshi_market, deep_book) -> None:  # noqa: ANN001
        quotes = price_at_sizes(kalshi_market, deep_book, Side.YES, D("0.60"))
        assert [q.size for q in quotes] == [D(10), D(50), D(100)]
        # Cost per contract must be non-decreasing as size grows into the book.
        assert quotes[0].average_price <= quotes[2].average_price
