"""Depth profiling and the opportunity-class taxonomy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import (
    ARBITRAGE_LABEL_TO_CLASS,
    ArbitrageLabel,
    OpportunityClass,
    Side,
    classify_arbitrage_label,
)
from pmvl_markets.pricing.orderbook import depth_profile


class TestDepthProfile:
    def test_bands_are_cumulative(self, book_factory) -> None:  # noqa: ANN001
        book = book_factory(
            yes_asks=[("0.40", "100"), ("0.41", "200"), ("0.43", "500"), ("0.50", "900")]
        )
        p = depth_profile(book, Side.YES)
        assert p["best_price"] == Decimal("0.40")
        assert p["size_at_best"] == Decimal("100")
        # within 1c reaches 0.41, within 3c reaches 0.43
        assert p["size_within_1c"] == Decimal("300")
        assert p["size_within_3c"] == Decimal("800")
        assert p["size_at_best"] <= p["size_within_1c"] <= p["size_within_3c"]

    def test_notional_matches_price_times_size(self, book_factory) -> None:  # noqa: ANN001
        book = book_factory(yes_asks=[("0.40", "100"), ("0.41", "200")])
        p = depth_profile(book, Side.YES)
        assert p["notional_at_best"] == Decimal("40")
        assert p["notional_within_1c"] == Decimal("40") + Decimal("82")

    def test_thin_book_shows_the_cliff(self, book_factory) -> None:  # noqa: ANN001
        """A 2c edge with depth only at best is a handful of contracts."""
        book = book_factory(yes_asks=[("0.40", "5"), ("0.60", "5000")])
        p = depth_profile(book, Side.YES)
        assert p["size_at_best"] == Decimal("5")
        assert p["size_within_3c"] == Decimal("5")  # the 0.60 level is far outside

    def test_empty_side_returns_none_not_zero(self, book_factory) -> None:  # noqa: ANN001
        """'No book' and '$0 of depth' are different claims and must look different."""
        p = depth_profile(book_factory(yes_asks=[]), Side.YES)
        assert p["best_price"] is None
        assert p["size_at_best"] is None
        assert p["notional_within_3c"] is None

    def test_no_side_uses_the_no_ladder(self, book_factory) -> None:  # noqa: ANN001
        book = book_factory(
            yes_asks=[("0.40", "100")], no_asks=[("0.58", "70"), ("0.59", "30")]
        )
        p = depth_profile(book, Side.NO)
        assert p["best_price"] == Decimal("0.58")
        assert p["size_within_1c"] == Decimal("100")


class TestOpportunityClass:
    def test_only_guaranteed_earns_the_word_arbitrage(self) -> None:
        earners = [c for c in OpportunityClass if c.may_be_called_arbitrage]
        assert earners == [OpportunityClass.GUARANTEED_ARBITRAGE]

    def test_execution_constrained_is_not_arbitrage(self) -> None:
        c = OpportunityClass.EXECUTION_CONSTRAINED_ARBITRAGE
        assert not c.may_be_called_arbitrage
        assert not c.is_guaranteed

    def test_actionable_set(self) -> None:
        assert OpportunityClass.GUARANTEED_ARBITRAGE.is_actionable
        assert OpportunityClass.RELATIVE_VALUE.is_actionable
        assert not OpportunityClass.MODEL_DISAGREEMENT.is_actionable
        assert not OpportunityClass.WATCHLIST.is_actionable
        assert not OpportunityClass.REJECTED.is_actionable


class TestLabelMapping:
    def test_executable_is_the_only_guarantee(self) -> None:
        guaranteed = [
            label
            for label, cls in ARBITRAGE_LABEL_TO_CLASS.items()
            if cls is OpportunityClass.GUARANTEED_ARBITRAGE
        ]
        assert guaranteed == [ArbitrageLabel.EXECUTABLE.value]

    @pytest.mark.parametrize(
        "label",
        [
            ArbitrageLabel.THEORETICAL,
            ArbitrageLabel.RULE_MISMATCH_RISK,
            ArbitrageLabel.EXECUTION_RISK,
            ArbitrageLabel.STALE_QUOTE,
            ArbitrageLabel.INSUFFICIENT_LIQUIDITY,
            ArbitrageLabel.NOT_GUARANTEED,
            ArbitrageLabel.LOGICAL_MISPRICING,
        ],
    )
    def test_every_weaker_label_is_demoted(self, label: ArbitrageLabel) -> None:
        assert not classify_arbitrage_label(label.value).may_be_called_arbitrage

    def test_every_arbitrage_label_is_mapped(self) -> None:
        """An unmapped label would silently fall back and hide a new failure mode."""
        for label in ArbitrageLabel:
            assert label.value in ARBITRAGE_LABEL_TO_CLASS, f"{label} is unmapped"

    def test_unknown_label_defaults_to_the_weakest_claim(self) -> None:
        assert classify_arbitrage_label("something_new") is OpportunityClass.WATCHLIST


class TestArbitrageMarginTiers:
    """Risk appetite lives in configuration, and cross-venue costs more."""

    def _settings(self):  # noqa: ANN202
        from pmvl_shared.config import get_settings

        return get_settings()

    def test_cross_platform_always_demands_more(self) -> None:
        s = self._settings()
        for depth in (Decimal("5000"), Decimal("100"), None):
            same = s.min_arbitrage_edge(cross_platform=False, depth_usd=depth)
            cross = s.min_arbitrage_edge(cross_platform=True, depth_usd=depth)
            assert cross > same, f"depth={depth}: cross {cross} not above same {same}"

    def test_thin_books_demand_more_than_liquid(self) -> None:
        s = self._settings()
        for cross in (False, True):
            liquid = s.min_arbitrage_edge(cross_platform=cross, depth_usd=Decimal("5000"))
            thin = s.min_arbitrage_edge(cross_platform=cross, depth_usd=Decimal("50"))
            assert thin > liquid

    def test_unknown_depth_is_treated_as_thin(self) -> None:
        """Absent evidence of depth is not evidence of depth."""
        s = self._settings()
        for cross in (False, True):
            assert s.min_arbitrage_edge(
                cross_platform=cross, depth_usd=None
            ) == s.min_arbitrage_edge(cross_platform=cross, depth_usd=Decimal("1"))

    def test_tiers_match_the_documented_defaults(self) -> None:
        s = self._settings()
        assert s.min_edge_same_platform_liquid == Decimal("0.015")
        assert s.min_edge_same_platform_normal == Decimal("0.03")
        assert s.min_edge_cross_platform == Decimal("0.04")
        assert s.min_edge_cross_platform_illiquid == Decimal("0.05")

    def test_tiers_are_monotone(self) -> None:
        s = self._settings()
        assert (
            s.min_edge_same_platform_liquid
            < s.min_edge_same_platform_normal
            < s.min_edge_cross_platform
            < s.min_edge_cross_platform_illiquid
        )
