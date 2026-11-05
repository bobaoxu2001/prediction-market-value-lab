"""A maker order is not filled because the quote touched its price.

This is the single most common way a prediction-market backtest becomes fiction,
and it is worth stating why it is not a conservative technicality:

A resting bid at 29c and a best bid that ticks down to 29c look, in candle data,
exactly like a fill. They are not one. The order is at the *back* of the queue at
that price; everything already resting there trades first. A touch with no traded
volume is a quote that moved, not a trade that happened. And the touches that do
fill are adversely selected - an order fills fastest exactly when the market is
moving through it, which is when you would least like to own it.

So these tests pin the refusals as hard as the fills, and they pin the direction
of every rounding and every ambiguous case: a simulator that is wrong should be
wrong in the direction that leaves money on the table, never in the one that
invents it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import Platform, Side
from pmvl_shared.schemas import NormalizedMarket

from pmvl_markets.alpha.execution import (
    ConservativeFillModel,
    FillConfidence,
    LevelTrade,
    MakerExecutionModel,
    MakerQuote,
    TakerExecutionModel,
    exit_proceeds_per_contract,
    maker_limit_price,
    worst_fill_confidence,
)
from pmvl_markets.alpha.models import Liquidity, ProposedOrder

from .conftest import make_book


def quote(price: str = "0.29", size: str = "100", **kw) -> MakerQuote:  # noqa: ANN003
    return MakerQuote(
        market_key="kalshi:TEST",
        side=Side.YES,
        limit_price=Decimal(price),
        size=Decimal(size),
        **kw,
    )


def order(
    price: str = "0.29",
    size: str = "100",
    liquidity: Liquidity = Liquidity.MAKER,
) -> ProposedOrder:
    return ProposedOrder(
        market_key="kalshi:TEST",
        side=Side.YES,
        liquidity=liquidity,
        limit_price=Decimal(price),
        size=Decimal(size),
        conservative_probability=Decimal("0.42"),
        expected_edge_if_filled=Decimal("0.10"),
    )


class TestNoFillOnTouch:
    """The rule the whole module exists to enforce."""

    def test_a_touch_with_no_traded_volume_does_not_fill(self) -> None:
        result = ConservativeFillModel().simulate(quote(), trades=[])
        assert result.filled is False
        assert result.confidence is FillConfidence.TOUCH_ONLY
        assert "not a trade" in result.reason

    def test_a_cross_with_no_volume_does_not_fill_by_default(self) -> None:
        """The book trading through a price with no print is a quote update."""
        result = ConservativeFillModel().simulate(quote(), trades=[], crossed=True)
        assert result.filled is False

    def test_a_cross_may_fill_only_when_explicitly_enabled_and_is_labelled(
        self,
    ) -> None:
        """Some feeds record crosses reliably and prints unreliably.

        The escape hatch exists for those, is off by default, and labels the
        result so it can be excluded from any headline.
        """
        model = ConservativeFillModel(allow_cross_without_volume=True)
        result = model.simulate(quote(), trades=[], crossed=True)
        assert result.filled is True
        assert result.confidence is FillConfidence.CROSSED_NO_VOLUME

    def test_volume_at_a_worse_price_than_the_limit_does_not_fill(self) -> None:
        """A print at 31c does not fill a bid at 29c."""
        result = ConservativeFillModel().simulate(
            quote("0.29"), [LevelTrade(price=Decimal("0.31"), volume=Decimal("500"))]
        )
        assert result.filled is False


class TestQueuePosition:
    def test_with_a_known_queue_only_volume_beyond_it_fills(self) -> None:
        result = ConservativeFillModel().simulate(
            quote(size="100"),
            [
                LevelTrade(
                    price=Decimal("0.29"),
                    volume=Decimal("250"),
                    queue_ahead=Decimal("200"),
                )
            ],
        )
        assert result.filled_size == Decimal("50")
        assert result.confidence is FillConfidence.TRADE_CONFIRMED

    def test_a_queue_that_absorbs_the_whole_print_means_no_fill(self) -> None:
        result = ConservativeFillModel().simulate(
            quote(size="100"),
            [
                LevelTrade(
                    price=Decimal("0.29"),
                    volume=Decimal("150"),
                    queue_ahead=Decimal("400"),
                )
            ],
        )
        assert result.filled is False
        assert "absorbed the whole print" in result.reason

    def test_without_queue_data_only_a_share_of_the_print_is_assumed(self) -> None:
        """0.25, not 1.0.

        Assuming the whole print says our order was alone at the price, which on
        a venue with real participants is the least likely arrangement rather
        than the neutral one.
        """
        result = ConservativeFillModel().simulate(
            quote(size="1000"), [LevelTrade(price=Decimal("0.29"), volume=Decimal("400"))]
        )
        assert result.filled_size == Decimal("100")
        assert result.confidence is FillConfidence.TRADE_CONFIRMED_QUEUE_UNKNOWN

    def test_the_blind_share_is_configurable_and_recorded_in_the_reason(self) -> None:
        model = ConservativeFillModel(blind_queue_share=Decimal("0.5"))
        result = model.simulate(
            quote(size="1000"), [LevelTrade(price=Decimal("0.29"), volume=Decimal("400"))]
        )
        assert result.filled_size == Decimal("200")
        assert "0.5" in result.reason


class TestPartialFills:
    def test_a_fill_smaller_than_the_request_is_marked_partial(self) -> None:
        result = ConservativeFillModel().simulate(
            quote(size="500"), [LevelTrade(price=Decimal("0.29"), volume=Decimal("400"))]
        )
        assert result.partial is True
        assert result.fill_ratio < 1

    def test_a_fill_never_exceeds_the_requested_size(self) -> None:
        result = ConservativeFillModel().simulate(
            quote(size="10"),
            [LevelTrade(price=Decimal("0.29"), volume=Decimal("100000"))],
        )
        assert result.filled_size == Decimal("10")

    def test_a_fractional_fill_rounds_DOWN_to_a_whole_contract(self) -> None:
        """Down, always.

        Rounding 0.8 of a contract up to one is a free contract at a price
        nobody offered, and across a long run those accumulate in exactly the
        direction that flatters the strategy.
        """
        result = ConservativeFillModel().simulate(
            quote(size="100"), [LevelTrade(price=Decimal("0.29"), volume=Decimal("7"))]
        )
        # 7 * 0.25 = 1.75 -> 1
        assert result.filled_size == Decimal("1")


class TestAMakerOrderThatIsReallyATaker:
    def test_an_order_priced_at_the_offer_is_refused(self) -> None:
        """It would execute instantly at a worse price than the maker path claims.

        Treating it as a maker fill credits a rebate to a trade that paid the
        spread.
        """
        result = ConservativeFillModel().simulate(
            quote("0.45", posted_at_best_ask=Decimal("0.45")),
            [LevelTrade(price=Decimal("0.45"), volume=Decimal("10000"))],
        )
        assert result.filled is False
        assert "taker order" in result.reason

    def test_the_maker_execution_model_refuses_it_too(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[("0.45", "500")], yes_bids=[("0.40", "500")])
        assessment = MakerExecutionModel().price_order(
            kalshi_market, book, order("0.45")
        )
        assert assessment.fill_probability == 0
        assert "taker order" in " ".join(assessment.notes)

    def test_joining_the_bid_is_not_improving_it(self) -> None:
        """An order at the existing best bid is behind the whole queue there."""
        joined = quote("0.40", posted_at_best_bid=Decimal("0.40"))
        improved = quote("0.41", posted_at_best_bid=Decimal("0.40"))
        assert joined.improves_the_bid is False
        assert improved.improves_the_bid is True


class TestMakerPricing:
    def test_a_maker_order_rests_one_tick_inside_the_touch(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[("0.45", "500")], yes_bids=[("0.40", "500")])
        assert maker_limit_price(kalshi_market, book, Side.YES) == Decimal("0.41")

    def test_a_one_tick_book_has_nowhere_to_rest(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Posting there would be a taker order under a maker label."""
        book = make_book(yes_asks=[("0.41", "500")], yes_bids=[("0.40", "500")])
        assert maker_limit_price(kalshi_market, book, Side.YES) is None

    def test_the_maker_model_asserts_no_fill_probability_of_its_own(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A constant here would be an invented number where it matters most.

        The whole result of a maker strategy is its fill rate; supplying one at
        pricing time would make the backtest a restatement of that constant.
        """
        book = make_book(yes_asks=[("0.45", "500")], yes_bids=[("0.40", "500")])
        assessment = MakerExecutionModel().price_order(
            kalshi_market, book, order("0.41")
        )
        assert assessment.fill_probability == 0
        assert "observed flow" in " ".join(assessment.notes)

    def test_an_adverse_selection_penalty_is_charged_against_a_maker_fill(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[("0.45", "500")], yes_bids=[("0.40", "500")])
        assessment = MakerExecutionModel().price_order(
            kalshi_market, book, order("0.41")
        )
        assert assessment.adverse_selection_penalty > 0
        assert (
            assessment.unconditional_expected_edge
            <= assessment.expected_edge_conditional_on_fill
        )


class TestTakerPricing:
    def test_entry_is_the_vwap_of_the_ladder_not_top_of_book(
        self, kalshi_market: NormalizedMarket, deep_book
    ) -> None:  # noqa: ANN001
        assessment = TakerExecutionModel().price_order(
            kalshi_market, deep_book, order("0.45", size="300", liquidity=Liquidity.TAKER)
        )
        # 100@0.40 + 200@0.42 => 0.4133..., strictly worse than the 0.40 touch.
        assert assessment.expected_fill_size == Decimal("300")
        assert assessment.fill_probability == 1

    def test_a_ladder_that_cannot_fill_the_size_reports_a_partial(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[("0.40", "10")])
        assessment = TakerExecutionModel().price_order(
            kalshi_market, book, order("0.45", size="500", liquidity=Liquidity.TAKER)
        )
        assert assessment.expected_fill_size == Decimal("10")
        assert "PARTIAL" in " ".join(assessment.notes)

    def test_an_empty_ladder_fills_nothing(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[])
        assessment = TakerExecutionModel().price_order(
            kalshi_market, book, order(liquidity=Liquidity.TAKER)
        )
        assert assessment.fill_probability == 0
        assert assessment.expected_fill_size == 0

    def test_the_venue_fee_is_deducted_from_the_conditional_edge(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[("0.30", "1000")])
        assessment = TakerExecutionModel().price_order(
            kalshi_market, book, order("0.30", size="100", liquidity=Liquidity.TAKER)
        )
        # conservative 0.42 - entry 0.30 - fee > 0, and strictly less than 0.12.
        assert 0 < assessment.expected_edge_conditional_on_fill < Decimal("0.12")


class TestExitPricing:
    def test_an_exit_sells_into_the_BID_ladder(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A convergence exit marked at the ask has not modelled an exit.

        It has recorded the price somebody wishes they could sell at.
        """
        book = make_book(
            yes_asks=[("0.45", "500")], yes_bids=[("0.40", "100"), ("0.38", "400")]
        )
        proceeds = exit_proceeds_per_contract(
            kalshi_market, book, Side.YES, Decimal("100")
        )
        assert proceeds is not None
        assert proceeds < Decimal("0.40")  # net of the closing fee

    def test_size_beyond_the_top_bid_walks_deeper_and_realises_less(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(
            yes_asks=[("0.45", "500")], yes_bids=[("0.40", "100"), ("0.38", "400")]
        )
        small = exit_proceeds_per_contract(kalshi_market, book, Side.YES, Decimal("100"))
        large = exit_proceeds_per_contract(kalshi_market, book, Side.YES, Decimal("500"))
        assert large < small

    def test_no_bid_means_no_exit_price_at_all(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A position that cannot be sold has not been exited.

        Recording one at the last ask would be the most flattering possible lie:
        it converts every illiquid loser into a clean close.
        """
        book = make_book(yes_asks=[("0.45", "500")], yes_bids=[])
        assert exit_proceeds_per_contract(
            kalshi_market, book, Side.YES, Decimal("100")
        ) is None


class TestFillConfidenceAggregation:
    def test_a_run_takes_the_WORST_confidence_not_the_average(self) -> None:
        """One queue-unmodelled fill must not disappear into an aggregate.

        The same rule the existing backtest applies to ``data_quality``, for the
        same reason.
        """
        assert (
            worst_fill_confidence(
                [
                    FillConfidence.TAKER_FROM_LADDER,
                    FillConfidence.TAKER_FROM_LADDER,
                    FillConfidence.CROSSED_NO_VOLUME,
                ]
            )
            is FillConfidence.CROSSED_NO_VOLUME
        )

    def test_an_empty_set_is_the_weakest_confidence(self) -> None:
        assert worst_fill_confidence([]) is FillConfidence.TOUCH_ONLY


class TestPolymarketMakersPayNothing:
    def test_a_polymarket_maker_is_charged_no_fee(
        self, polymarket_market: NormalizedMarket
    ) -> None:
        """"Makers are never charged fees" - the venue's published schedule."""
        from pmvl_markets.alpha.execution import _maker_fee_per_contract

        assert _maker_fee_per_contract(
            polymarket_market, Decimal("100"), Decimal("0.40")
        ) == 0

    def test_a_kalshi_series_without_maker_fees_pays_nothing(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A series this code has not seen pays nothing rather than a guessed rate.

        The direction that does not manufacture edge.
        """
        from pmvl_markets.alpha.execution import _maker_fee_per_contract

        assert kalshi_market.fee_type == "quadratic"
        assert _maker_fee_per_contract(
            kalshi_market, Decimal("100"), Decimal("0.40")
        ) == 0

    def test_a_kalshi_maker_fee_series_does_pay(self) -> None:
        from pmvl_markets.alpha.execution import _maker_fee_per_contract

        market = NormalizedMarket(
            platform=Platform.KALSHI,
            platform_market_id="KXMAKER-T1",
            fee_type="quadratic_with_maker_fees",
            maker_fee_rate=Decimal("0.0175"),
        )
        assert _maker_fee_per_contract(
            market, Decimal("100"), Decimal("0.50")
        ) > 0
