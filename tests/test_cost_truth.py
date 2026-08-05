"""Probability-free execution cost analysis.

The claims pinned here are the ones the product surface prints in large type, so
each test names the claim rather than the function. If any of these break, a page
is telling a reader something false about what a trade costs.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import D
from pmvl_shared.timeutil import utcnow

from pmvl_markets.pricing.cost_truth import (
    STALE_QUOTE_SECONDS,
    analyse_cost,
)

from .conftest import make_book


def one_cent_book() -> object:
    """A 1c contract with real depth behind it.

    The cheapest tick on Kalshi, and the case where fee rounding is most visible:
    the ceiled fee is a whole cent, equal to the price of the contract itself.
    """
    return make_book(yes_asks=[("0.01", "5000")])


class TestMeasuredCostIsSeparateFromModelledCost:
    """The headline figure must not be an artefact of a config default.

    The slippage pad is ``tick_size x SLIPPAGE_TICKS``, an assumption about market
    impact. At a 1c tick it is a whole cent, which on a cheap contract exceeds every
    real cost combined. If it ever leaks into ``measured_cost``, the product's
    central claim silently becomes a restatement of a constant.
    """

    def test_measured_cost_excludes_the_slippage_pad(self, kalshi_market) -> None:
        truth = analyse_cost(kalshi_market, Side.YES, book=one_cent_book())
        assert truth is not None
        entry = next(e for e in truth.ladder if e.size == D(100))

        assert entry.modelled_slippage > 0, "pad should be present to be excluded"
        assert entry.measured_cost == entry.all_in_cost - entry.modelled_slippage

    def test_breakeven_headline_uses_measured_cost_only(self, kalshi_market) -> None:
        truth = analyse_cost(kalshi_market, Side.YES, book=one_cent_book())
        assert truth is not None
        entry = next(e for e in truth.ladder if e.size == D(100))

        assert entry.breakeven_probability == entry.measured_cost
        assert entry.breakeven_probability_with_slippage == entry.all_in_cost
        assert entry.breakeven_probability < entry.breakeven_probability_with_slippage

    def test_measured_components_sum_to_measured_cost(self, kalshi_market) -> None:
        truth = analyse_cost(kalshi_market, Side.YES, book=one_cent_book())
        assert truth is not None
        entry = next(e for e in truth.ladder if e.size == D(50))
        parts = entry.as_dict()["measured_components"]

        # entry_price already contains depth impact, so the reconstruction starts
        # from nominal and adds it back rather than double-counting.
        rebuilt = (
            entry.nominal_price
            + parts["depth_impact"]
            + parts["platform_fee"]
            + parts["fee_rounding"]
            + parts["transfer_cost"]
            + parts["capital_cost"]
        )
        assert rebuilt == entry.measured_cost


class TestKalshiFeeRoundingDominatesSmallOrders:
    """The claim the product leads with, checked against the published rule.

    Kalshi ceils the fee to the whole cent on the *whole order*. A single 1c
    contract therefore pays a full cent of fee -- the fee alone doubles the cost of
    the trade -- while the same contract bought 100 at a time pays a fraction of
    that per contract.
    """

    def test_a_single_one_cent_contract_costs_two_cents(self, kalshi_market) -> None:
        truth = analyse_cost(kalshi_market, Side.YES, book=one_cent_book())
        assert truth is not None
        one = next(e for e in truth.ladder if e.size == D(1))

        assert one.nominal_price == Decimal("0.0100")
        assert one.measured_cost == Decimal("0.0200")
        assert one.measured_premium_ratio == Decimal("1")  # a clean doubling

    def test_per_contract_cost_falls_as_size_rises(self, kalshi_market) -> None:
        truth = analyse_cost(kalshi_market, Side.YES, book=one_cent_book())
        assert truth is not None
        by_size = {e.size: e.measured_cost for e in truth.ladder}

        assert by_size[D(1)] > by_size[D(10)] > by_size[D(100)]
        # The whole point: an order-of-magnitude spread in premium on one contract.
        assert by_size[D(1)] / by_size[D(100)] > Decimal("1.8")

    def test_depth_impact_is_zero_when_one_level_fills_the_order(
        self, kalshi_market
    ) -> None:
        # Distinguishes "no impact measured" from "impact unknown": with a book
        # present the figure is a real zero, not a None.
        truth = analyse_cost(kalshi_market, Side.YES, book=one_cent_book())
        assert truth is not None
        entry = next(e for e in truth.ladder if e.size == D(10))
        assert entry.depth_impact == Decimal("0.0000")


class TestDepthImpact:
    def test_walking_the_ladder_raises_entry_above_top_of_book(
        self, kalshi_market, deep_book
    ) -> None:
        truth = analyse_cost(kalshi_market, Side.YES, book=deep_book)
        assert truth is not None
        # 100 fills at 0.40; 250 must eat 0.42 and 0.45 as well.
        small = next(e for e in truth.ladder if e.size == D(100))
        large = next(e for e in truth.ladder if e.size == D(250))

        assert small.depth_impact == Decimal("0.0000")
        assert large.depth_impact > 0
        assert large.entry_price > small.entry_price

    def test_partial_fill_is_reported_not_silently_extrapolated(
        self, kalshi_market, deep_book
    ) -> None:
        # The book holds 800 contracts; 1000 cannot fill.
        truth = analyse_cost(kalshi_market, Side.YES, book=deep_book)
        assert truth is not None
        entry = next(e for e in truth.ladder if e.size == D(1000))

        assert entry.fully_filled is False
        assert entry.filled_size == D(800)
        assert entry.total_outlay == entry.measured_cost * D(800)


class TestVenueSummaryFallback:
    """A market with no captured book still gets an answer, correctly degraded."""

    def test_unknown_depth_impact_is_none_never_zero(self, kalshi_market) -> None:
        truth = analyse_cost(
            kalshi_market, Side.YES, book=None, summary_ask=D("0.34")
        )
        assert truth is not None
        assert truth.quote_source == "venue_summary"
        assert truth.depth_known is False
        assert truth.available_depth_usd is None
        assert truth.max_fillable_size is None
        assert all(e.depth_impact is None for e in truth.ladder)

    def test_fully_filled_is_never_claimed_without_depth(self, kalshi_market) -> None:
        # Claiming a fill on an unobserved book is the specific optimism this
        # module exists to remove.
        truth = analyse_cost(
            kalshi_market, Side.YES, book=None, summary_ask=D("0.34")
        )
        assert truth is not None
        assert all(e.fully_filled is False for e in truth.ladder)

    def test_the_caveat_says_the_figure_is_a_floor(self, kalshi_market) -> None:
        truth = analyse_cost(
            kalshi_market, Side.YES, book=None, summary_ask=D("0.34")
        )
        assert truth is not None
        assert any("floor" in c for c in truth.caveats)

    def test_fees_are_still_exact_without_a_book(self, kalshi_market) -> None:
        # Fee, transfer and capital cost do not need depth, so the degraded path
        # still carries real numbers rather than placeholders.
        truth = analyse_cost(
            kalshi_market, Side.YES, book=None, summary_ask=D("0.50")
        )
        assert truth is not None
        one = next(e for e in truth.ladder if e.size == D(1))
        assert one.measured_cost > one.nominal_price

    def test_an_empty_book_falls_back_rather_than_returning_nothing(
        self, kalshi_market
    ) -> None:
        truth = analyse_cost(
            kalshi_market, Side.YES, book=make_book(), summary_ask=D("0.34")
        )
        assert truth is not None
        assert truth.quote_source == "venue_summary"


class TestBreakEvenProbabilityIsAProbability:
    def test_cost_at_or_above_one_dollar_has_no_break_even(self, kalshi_market) -> None:
        """A contract paying $1 that costs $1 cannot break even at any probability.

        Reporting 1.02 here would put a number outside [0, 1] into a field the UI
        renders as a percentage.
        """
        truth = analyse_cost(
            kalshi_market, Side.YES, book=make_book(yes_asks=[("0.99", "5000")])
        )
        assert truth is not None
        one = next(e for e in truth.ladder if e.size == D(1))

        assert one.measured_cost >= Decimal("1")
        assert one.breakeven_probability is None

    def test_normal_case_is_inside_the_unit_interval(self, kalshi_market) -> None:
        truth = analyse_cost(
            kalshi_market, Side.YES, book=make_book(yes_asks=[("0.34", "5000")])
        )
        assert truth is not None
        entry = next(e for e in truth.ladder if e.size == D(100))
        assert entry.breakeven_probability is not None
        assert Decimal("0.34") < entry.breakeven_probability < Decimal("1")


class TestPolymarketTransferCost:
    def test_bridge_cost_makes_small_positions_structurally_expensive(
        self, polymarket_market
    ) -> None:
        """A $0.10 position that costs $0.50 to fund is not a cheap position."""
        book = make_book(
            platform=Platform.POLYMARKET, yes_asks=[("0.001", "100000")]
        )
        truth = analyse_cost(polymarket_market, Side.YES, book=book)
        assert truth is not None
        small = next(e for e in truth.ladder if e.size == D(100))
        large = next(e for e in truth.ladder if e.size == D(1000))

        transfer_small = small.as_dict()["measured_components"]["transfer_cost"]
        assert transfer_small > 0
        # Amortisation: ten times the size, a tenth of the per-contract bridge cost.
        assert large.measured_cost < small.measured_cost


class TestMinimumOrderSize:
    """A size the venue rejects is unplaceable, not merely expensive.

    Polymarket's minimum is 5 contracts. Amortising the fixed $0.50 bridge cost
    over a single contract yields a 50,000% premium — arithmetically right, and an
    answer to a question about an order that cannot be sent. Left in the ladder it
    dominated the ranking and looked like a finding about fees.
    """

    def _poly_truth(self, polymarket_market):  # noqa: ANN001, ANN202
        book = make_book(
            platform=Platform.POLYMARKET, yes_asks=[("0.001", "100000")]
        )
        return analyse_cost(polymarket_market, Side.YES, book=book, requested_size=D(1))

    def test_unplaceable_sizes_are_absent_from_the_ladder(
        self, polymarket_market
    ) -> None:
        truth = self._poly_truth(polymarket_market)
        assert truth is not None
        assert polymarket_market.min_order_size == D(5)
        assert all(not e.below_min_order_size for e in truth.ladder)
        assert D(1) not in {e.size for e in truth.ladder}

    def test_an_explicit_request_still_answers_but_is_flagged(
        self, polymarket_market
    ) -> None:
        # Refusing to answer would be worse: a reader who asks what one contract
        # costs deserves the number *and* the reason it is unavailable.
        truth = self._poly_truth(polymarket_market)
        assert truth is not None
        assert truth.requested is not None
        assert truth.requested.size == D(1)
        assert truth.requested.below_min_order_size is True

    def test_the_caveat_names_the_minimum(self, polymarket_market) -> None:
        truth = self._poly_truth(polymarket_market)
        assert truth is not None
        assert any("below 5" in c for c in truth.caveats)

    def test_kalshi_sub_unit_minimum_flags_nothing(self, kalshi_market) -> None:
        # Kalshi's minimum is 0.01, so every ladder size is placeable and the
        # exclusion must not quietly remove rows there.
        truth = analyse_cost(kalshi_market, Side.YES, book=one_cent_book())
        assert truth is not None
        assert all(not e.below_min_order_size for e in truth.ladder)
        assert D(1) in {e.size for e in truth.ladder}


class TestStaleness:
    def test_a_fresh_quote_is_not_flagged(self, kalshi_market) -> None:
        truth = analyse_cost(
            kalshi_market,
            Side.YES,
            book=one_cent_book(),
            quote_observed_at=utcnow() - timedelta(seconds=60),
        )
        assert truth is not None
        assert truth.is_stale is False

    def test_an_old_quote_is_flagged_and_says_so(self, kalshi_market) -> None:
        truth = analyse_cost(
            kalshi_market,
            Side.YES,
            book=one_cent_book(),
            quote_observed_at=utcnow() - timedelta(seconds=STALE_QUOTE_SECONDS + 60),
        )
        assert truth is not None
        assert truth.is_stale is True
        assert any("observed" in c for c in truth.caveats)

    def test_naive_timestamps_from_sqlite_do_not_raise(self, kalshi_market) -> None:
        # Rows read back from SQLite have no tzinfo. Subtracting one from an aware
        # `now` raises, which would take out every cost page at once.
        naive = (utcnow() - timedelta(minutes=5)).replace(tzinfo=None)
        truth = analyse_cost(
            kalshi_market, Side.YES, book=one_cent_book(), quote_observed_at=naive
        )
        assert truth is not None
        assert truth.quote_age_seconds is not None


class TestNoPriceAtAll:
    def test_returns_none_rather_than_a_zero_cost(self, kalshi_market) -> None:
        assert analyse_cost(kalshi_market, Side.YES, book=None, summary_ask=None) is None

    def test_a_zero_summary_ask_is_not_treated_as_a_price(self, kalshi_market) -> None:
        assert (
            analyse_cost(kalshi_market, Side.YES, book=None, summary_ask=D("0")) is None
        )


class TestRequestedSize:
    def test_a_requested_size_is_priced_even_when_off_the_ladder(
        self, kalshi_market
    ) -> None:
        truth = analyse_cost(
            kalshi_market, Side.YES, book=one_cent_book(), requested_size=D(7)
        )
        assert truth is not None
        assert truth.requested is not None
        assert truth.requested.size == D(7)

    def test_the_ladder_stays_the_standard_ladder(self, kalshi_market) -> None:
        # The custom size must not silently appear in the comparison ladder, or the
        # chart's x-axis changes shape per request.
        truth = analyse_cost(
            kalshi_market, Side.YES, book=one_cent_book(), requested_size=D(7)
        )
        assert truth is not None
        assert D(7) not in {e.size for e in truth.ladder}


class TestCostApiRouting:
    """The `/cost` router's static paths must outrank its parameterised one.

    `@router.get("/{market_id}")` typed as `int` is declared in the same router as
    `/cost/by-category`. FastAPI matches in declaration order, so with the
    parameterised route first, `/cost/by-category` is matched as a market id and
    fails integer parsing — a 422 on a route that exists. It is invisible in
    review and obvious in production, so it is pinned here.
    """

    @pytest.fixture()
    def client(self, clean_db):  # noqa: ANN001, ANN201
        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        return TestClient(app)

    def test_by_category_is_not_matched_as_a_market_id(self, client) -> None:  # noqa: ANN001
        response = client.get("/cost/by-category")
        assert response.status_code == 200, response.text
        assert isinstance(response.json()["data"], list)

    def test_a_numeric_market_id_still_routes_to_the_detail_endpoint(
        self, client
    ) -> None:  # noqa: ANN001
        # 404 rather than 422: the path matched and the market simply is not there.
        assert client.get("/cost/999999999").status_code == 404

    def test_an_unparseable_size_is_rejected_without_echoing_the_input(
        self, client
    ) -> None:  # noqa: ANN001
        response = client.get("/cost/by-category", params={"size": "; DROP"})
        assert response.status_code == 400
        assert "DROP" not in response.text

    def test_a_negative_size_is_rejected(self, client) -> None:  # noqa: ANN001
        assert client.get("/cost/by-category", params={"size": "-5"}).status_code == 400
