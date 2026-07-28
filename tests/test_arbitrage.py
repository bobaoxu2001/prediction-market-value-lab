"""Arbitrage scanner tests, focused on the conditions that block a risk-free claim."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import ArbitrageLabel, Platform, RuleCompatibility, Side
from pmvl_shared.money import D
from pmvl_shared.timeutil import utcnow
from pmvl_markets.arbitrage.complete_set import scan_complete_set
from pmvl_markets.arbitrage.cross_platform import scan_cross_platform
from pmvl_markets.arbitrage.logical import ThresholdMarket, scan_exhaustive_sum, scan_monotonicity
from pmvl_markets.arbitrage.multi_outcome import OutcomeLeg, scan_multi_outcome
from pmvl_markets.arbitrage.stale import QuoteObservation, detect_stale_quote
from pmvl_markets.matching.verify import MatchVerdict


def verdict(compat: RuleCompatibility, inverted: bool = False) -> MatchVerdict:
    return MatchVerdict(
        rule_compatibility=compat,
        match_confidence=D("0.9"),
        time_compatibility=D("1"),
        source_compatibility=D("1"),
        polarity_inverted=inverted,
        outcome_mapping={},
    )


class TestCompleteSetArbitrage:
    def test_finds_profitable_pair(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        book = book_factory(yes_asks=[("0.40", "1000")], no_asks=[("0.45", "1000")])
        result = scan_complete_set(kalshi_market, book)
        assert result is not None
        assert result.net_profit_per_set > 0
        assert result.max_executable_sets > 0
        assert len(result.legs) == 2
        assert {l.side for l in result.legs} == {Side.YES, Side.NO}

    def test_rejects_when_pair_costs_a_dollar_or_more(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        book = book_factory(yes_asks=[("0.55", "1000")], no_asks=[("0.46", "1000")])
        assert scan_complete_set(kalshi_market, book) is None

    def test_rejects_when_fees_consume_the_edge(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        """A 0.5c gross edge does not survive two quadratic fees plus slippage."""
        book = book_factory(yes_asks=[("0.4975", "1000")], no_asks=[("0.4975", "1000")])
        assert scan_complete_set(kalshi_market, book) is None

    def test_legs_are_size_matched(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        """An unmatched leg is a naked position, not an arbitrage."""
        book = book_factory(yes_asks=[("0.40", "1000")], no_asks=[("0.45", "60")])
        result = scan_complete_set(kalshi_market, book)
        assert result is not None
        assert result.max_executable_sets == Decimal("60")
        assert all(l.size_available == Decimal("60") for l in result.legs)

    def test_missing_side_yields_nothing(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        book = book_factory(yes_asks=[("0.40", "1000")], no_asks=[])
        assert scan_complete_set(kalshi_market, book) is None

    def test_stale_book_is_downgraded(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        old = utcnow() - timedelta(hours=2)
        book = book_factory(
            yes_asks=[("0.40", "1000")], no_asks=[("0.45", "1000")], observed_at=old
        )
        result = scan_complete_set(kalshi_market, book)
        assert result is not None
        assert result.label == ArbitrageLabel.STALE_QUOTE

    def test_thin_book_is_downgraded(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        book = book_factory(yes_asks=[("0.40", "5")], no_asks=[("0.45", "5")])
        result = scan_complete_set(kalshi_market, book)
        assert result is not None
        assert result.label == ArbitrageLabel.INSUFFICIENT_LIQUIDITY


class TestCrossPlatformArbitrage:
    def test_identical_rules_can_be_executable(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        book_a = book_factory(yes_asks=[("0.30", "5000")], no_asks=[("0.71", "5000")])
        book_b = book_factory(
            platform=Platform.POLYMARKET, market_id="123456",
            yes_asks=[("0.31", "5000")], no_asks=[("0.55", "5000")],
        )
        result = scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.IDENTICAL),
        )
        assert result is not None
        assert result.net_profit_per_set > 0
        assert result.label == ArbitrageLabel.EXECUTABLE

    def test_equivalent_rules_are_never_executable(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        """Anything short of an exact rule match must carry rule-mismatch risk."""
        book_a = book_factory(yes_asks=[("0.30", "5000")], no_asks=[("0.71", "5000")])
        book_b = book_factory(
            platform=Platform.POLYMARKET, market_id="123456",
            yes_asks=[("0.31", "5000")], no_asks=[("0.55", "5000")],
        )
        result = scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.EQUIVALENT),
        )
        assert result is not None
        assert result.label != ArbitrageLabel.EXECUTABLE
        assert result.label == ArbitrageLabel.RULE_MISMATCH_RISK
        assert any("NOT a guaranteed" in flag for flag in result.risk_flags)

    def test_incompatible_rules_are_skipped_entirely(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        book_a = book_factory(yes_asks=[("0.10", "5000")])
        book_b = book_factory(platform=Platform.POLYMARKET, no_asks=[("0.10", "5000")])
        assert scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.INCOMPATIBLE),
        ) is None

    def test_execution_risk_is_charged(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        book_a = book_factory(yes_asks=[("0.30", "5000")], no_asks=[("0.71", "5000")])
        book_b = book_factory(
            platform=Platform.POLYMARKET, market_id="123456",
            yes_asks=[("0.31", "5000")], no_asks=[("0.55", "5000")],
        )
        result = scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.IDENTICAL),
        )
        assert Decimal(result.cost_breakdown["execution_risk"]) > 0
        assert Decimal(result.cost_breakdown["transfer"]) > 0

    def test_no_edge_yields_nothing(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        book_a = book_factory(yes_asks=[("0.55", "5000")], no_asks=[("0.46", "5000")])
        book_b = book_factory(
            platform=Platform.POLYMARKET, market_id="123456",
            yes_asks=[("0.55", "5000")], no_asks=[("0.46", "5000")],
        )
        assert scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.IDENTICAL),
        ) is None


class TestCrossPlatformMarginGate:
    """Cross-venue trades must clear a tiered minimum edge, not merely break even."""

    def _books(self, book_factory, yes_a: str, no_b: str, size: str = "5000"):  # noqa: ANN001
        return (
            book_factory(yes_asks=[(yes_a, size)], no_asks=[("0.99", size)]),
            book_factory(
                platform=Platform.POLYMARKET, market_id="123456",
                yes_asks=[("0.99", size)], no_asks=[(no_b, size)],
            ),
        )

    def test_thin_edge_is_rejected_even_though_it_is_profitable(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        """A fraction of a cent of modelled edge does not cover unmodelled risk.

        The cost stack cannot price a venue halting, a rule reading that turns out to
        differ, or one leg filling while the other is pulled.
        """
        book_a, book_b = self._books(book_factory, "0.49", "0.50")
        assert scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.IDENTICAL),
        ) is None

    def test_wide_edge_clears_the_gate(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        book_a, book_b = self._books(book_factory, "0.30", "0.55")
        result = scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.IDENTICAL),
        )
        assert result is not None
        assert result.net_roi >= Decimal("0.04")

    def test_binding_leg_is_named(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        """A reader needs to know WHICH venue limits the trade, not just the size."""
        book_a = book_factory(yes_asks=[("0.30", "5000")], no_asks=[("0.99", "5000")])
        book_b = book_factory(
            platform=Platform.POLYMARKET, market_id="123456",
            yes_asks=[("0.99", "5000")], no_asks=[("0.55", "120")],
        )
        result = scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.IDENTICAL),
        )
        assert result is not None
        assert result.max_executable_sets == Decimal("120")
        assert any("limited by" in flag for flag in result.risk_flags)
        assert any("polymarket" in flag for flag in result.risk_flags)

    def test_legs_are_size_matched_to_the_scarcer_side(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        """Filling one leg deeper than the other is a naked position."""
        book_a = book_factory(yes_asks=[("0.30", "5000")], no_asks=[("0.99", "5000")])
        book_b = book_factory(
            platform=Platform.POLYMARKET, market_id="123456",
            yes_asks=[("0.99", "5000")], no_asks=[("0.55", "120")],
        )
        result = scan_cross_platform(
            kalshi_market, book_a, polymarket_market, book_b,
            verdict(RuleCompatibility.IDENTICAL),
        )
        assert all(l.size_available == Decimal("120") for l in result.legs)


class TestMultiOutcomeArbitrage:
    def _legs(self, market, book_factory, prices: list[str]) -> list[OutcomeLeg]:  # noqa: ANN001
        return [
            OutcomeLeg(
                market=market.model_copy(update={"platform_market_id": f"M{i}"}),
                book=book_factory(yes_asks=[(p, "1000")]),
                market_id=i,
            )
            for i, p in enumerate(prices)
        ]

    def test_negative_risk_set_below_one_is_executable(self, polymarket_market, book_factory) -> None:  # noqa: ANN001
        legs = self._legs(polymarket_market, book_factory, ["0.20", "0.30", "0.35"])
        result = scan_multi_outcome(
            legs, event_title="Event", mutually_exclusive=True, exhaustive=True,
            negative_risk=True, expected_outcome_count=3,
        )
        assert result is not None
        assert result.label == ArbitrageLabel.EXECUTABLE
        assert result.net_profit_per_set > 0

    def test_partial_basket_is_refused(self, polymarket_market, book_factory) -> None:  # noqa: ANN001
        """Pricing 3 of 11 outcomes must never be reported as a complete set."""
        legs = self._legs(polymarket_market, book_factory, ["0.20", "0.30", "0.35"])
        assert scan_multi_outcome(
            legs, event_title="Event", mutually_exclusive=True, exhaustive=True,
            negative_risk=True, expected_outcome_count=11,
        ) is None

    def test_non_exhaustive_set_is_not_guaranteed(self, polymarket_market, book_factory) -> None:  # noqa: ANN001
        legs = self._legs(polymarket_market, book_factory, ["0.20", "0.30", "0.35"])
        result = scan_multi_outcome(
            legs, event_title="Event", mutually_exclusive=True, exhaustive=False,
            negative_risk=False, expected_outcome_count=3,
        )
        assert result is not None
        assert result.label == ArbitrageLabel.NOT_GUARANTEED
        assert any("exhaustive" in flag for flag in result.risk_flags)

    def test_set_at_or_above_one_yields_nothing(self, polymarket_market, book_factory) -> None:  # noqa: ANN001
        legs = self._legs(polymarket_market, book_factory, ["0.40", "0.35", "0.30"])
        assert scan_multi_outcome(
            legs, event_title="Event", mutually_exclusive=True, exhaustive=True,
            negative_risk=True, expected_outcome_count=3,
        ) is None

    def test_unquotable_leg_blocks_the_set(self, polymarket_market, book_factory) -> None:  # noqa: ANN001
        legs = self._legs(polymarket_market, book_factory, ["0.20", "0.30"])
        legs.append(
            OutcomeLeg(market=polymarket_market, book=book_factory(yes_asks=[]), market_id=9)
        )
        assert scan_multi_outcome(
            legs, event_title="Event", mutually_exclusive=True, exhaustive=True,
            negative_risk=True, expected_outcome_count=3,
        ) is None


class TestLogicalConstraints:
    def test_monotonicity_violation_is_detected(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        """A higher threshold priced above a lower one is impossible."""
        family = [
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": "LOW"}),
                book=book_factory(yes_asks=[("0.30", "1000")], no_asks=[("0.71", "1000")]),
                threshold=Decimal("70000"),
                market_id=1,
            ),
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": "HIGH"}),
                book=book_factory(yes_asks=[("0.45", "1000")], no_asks=[("0.56", "1000")]),
                threshold=Decimal("80000"),
                market_id=2,
            ),
        ]
        results = scan_monotonicity(family)
        assert results
        assert "impossible" in results[0].risk_flags[0]

    def test_consistent_family_produces_nothing(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        family = [
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": "LOW"}),
                book=book_factory(yes_asks=[("0.45", "1000")]),
                threshold=Decimal("70000"), market_id=1,
            ),
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": "HIGH"}),
                book=book_factory(yes_asks=[("0.30", "1000")]),
                threshold=Decimal("80000"), market_id=2,
            ),
        ]
        assert scan_monotonicity(family) == []

    def test_violation_without_a_hedge_is_only_a_mispricing(
        self, kalshi_market, book_factory
    ) -> None:  # noqa: ANN001
        """No executable hedge means no risk-free claim, only a flagged anomaly."""
        family = [
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": "LOW"}),
                book=book_factory(yes_asks=[("0.30", "1000")], no_asks=[]),
                threshold=Decimal("70000"), market_id=1,
            ),
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": "HIGH"}),
                book=book_factory(yes_asks=[("0.45", "1000")], no_asks=[]),
                threshold=Decimal("80000"), market_id=2,
            ),
        ]
        results = scan_monotonicity(family)
        assert results
        assert results[0].label == ArbitrageLabel.LOGICAL_MISPRICING
        assert results[0].max_net_profit == 0

    def test_exhaustive_sum_deviation_is_reported(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        legs = [
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": f"M{i}"}),
                book=book_factory(yes_asks=[(p, "1000")]),
                threshold=Decimal("0"), market_id=i,
            )
            for i, p in enumerate(["0.50", "0.45", "0.20"])
        ]
        result = scan_exhaustive_sum(legs, event_title="Event")
        assert result is not None
        assert result.label == ArbitrageLabel.LOGICAL_MISPRICING

    def test_balanced_set_produces_nothing(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        legs = [
            ThresholdMarket(
                market=kalshi_market.model_copy(update={"platform_market_id": f"M{i}"}),
                book=book_factory(yes_asks=[(p, "1000")]),
                threshold=Decimal("0"), market_id=i,
            )
            for i, p in enumerate(["0.50", "0.50"])
        ]
        assert scan_exhaustive_sum(legs, event_title="Event") is None


class TestStaleQuote:
    def _obs(self, market, book_factory, current, previous):  # noqa: ANN001
        return QuoteObservation(
            market=market,
            book=book_factory(yes_asks=[(str(current), "1000")], no_asks=[("0.50", "1000")]),
            current_mid=Decimal(str(current)),
            previous_mid=Decimal(str(previous)) if previous is not None else None,
            previous_at=utcnow() - timedelta(minutes=5),
        )

    def test_detects_a_lagging_venue(self, kalshi_market, polymarket_market, book_factory) -> None:  # noqa: ANN001
        leader = self._obs(kalshi_market, book_factory, "0.70", "0.60")
        laggard = self._obs(polymarket_market, book_factory, "0.60", "0.60")
        result = detect_stale_quote(leader, laggard)
        assert result is not None
        assert result.label == ArbitrageLabel.STALE_QUOTE
        # Never claims a locked-in profit.
        assert result.max_net_profit == 0
        assert any("race" in flag for flag in result.risk_flags)

    def test_requires_an_actual_move(self, kalshi_market, polymarket_market, book_factory) -> None:  # noqa: ANN001
        """A persistent price gap is a rule/liquidity difference, not staleness."""
        leader = self._obs(kalshi_market, book_factory, "0.70", "0.70")
        laggard = self._obs(polymarket_market, book_factory, "0.60", "0.60")
        assert detect_stale_quote(leader, laggard) is None

    def test_both_venues_moving_is_not_stale(
        self, kalshi_market, polymarket_market, book_factory
    ) -> None:  # noqa: ANN001
        leader = self._obs(kalshi_market, book_factory, "0.70", "0.60")
        laggard = self._obs(polymarket_market, book_factory, "0.69", "0.59")
        assert detect_stale_quote(leader, laggard) is None
