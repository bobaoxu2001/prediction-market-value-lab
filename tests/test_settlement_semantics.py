"""Settlement-semantics guards for cross-platform equivalence.

Every case here is a pairing that a title-similarity matcher accepts and that would
produce a fabricated "risk-free" arbitrage. Two markets can share almost every word
and still settle on different facts, so each test asserts that the pair is NOT
promoted to the level that permits a risk-free claim.

The bar is deliberately asymmetric: being wrong about equivalence creates a trade
that can lose on both legs, whereas being too strict merely means a missed
opportunity.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, Platform, RuleCompatibility
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from pmvl_markets.matching.candidates import MatchCandidate
from pmvl_markets.matching.verify import verify_match
from pmvl_markets.normalize.text import extract_features

BASE_RESOLUTION = utcnow() + timedelta(days=2)


def market(
    platform: Platform,
    market_id: str,
    title: str,
    *,
    subtitle: str = "",
    description: str = "",
    strike_type: str | None = None,
    floor: str | None = None,
    cap: str | None = None,
    settlement_source: str = "",
    resolution_offset_hours: float = 0.0,
    category: Category = Category.OTHER,
) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        platform_market_id=market_id,
        title=title,
        subtitle=subtitle,
        description=description,
        category=category,
        strike_type=strike_type,
        floor_strike=Decimal(floor) if floor is not None else None,
        cap_strike=Decimal(cap) if cap is not None else None,
        settlement_source=settlement_source,
        settlement_rules_raw=description,
        close_time=BASE_RESOLUTION + timedelta(hours=resolution_offset_hours),
        event_occurrence_time=BASE_RESOLUTION + timedelta(hours=resolution_offset_hours),
        expected_resolution_time=BASE_RESOLUTION
        + timedelta(hours=resolution_offset_hours + 1),
    )


def pair(a: NormalizedMarket, b: NormalizedMarket) -> MatchCandidate:
    """Build a candidate with deliberately generous similarity signals.

    Real candidate generation would score these lower. Feeding in high similarity
    isolates the semantic checks: if the pair is still rejected, it is the settlement
    rules doing the work rather than a low text score.
    """
    features_a = extract_features(a.title, subtitle=a.subtitle, description=a.description)
    features_b = extract_features(b.title, subtitle=b.subtitle, description=b.description)
    return MatchCandidate(
        market_a=a,
        market_b=b,
        features_a=features_a,
        features_b=features_b,
        token_similarity=0.95,
        entity_overlap=0.95,
        shared_tokens=tuple(sorted(features_a.tokens & features_b.tokens)),
    )


def assert_not_riskfree(verdict, because: str) -> None:
    assert not verdict.allows_riskfree_claim, (
        f"{because}: pair was promoted to {verdict.rule_compatibility.value}, which "
        f"permits a risk-free arbitrage claim. Reasons recorded: "
        f"{verdict.mismatch_reasons}"
    )


class TestThresholdSemantics:
    def test_strictly_above_is_not_at_least(self) -> None:
        """'above 75' excludes exactly 75; '75 or above' includes it.

        On an integer-valued quantity that single point carries real probability, so
        the two contracts can settle differently on the same observation.
        """
        a = market(
            Platform.KALSHI, "K1",
            "Will the high temp in Chicago be above 75 degrees?",
            subtitle="76 or above", strike_type="greater", floor="75",
            settlement_source="National Weather Service",
            category=Category.WEATHER,
        )
        b = market(
            Platform.POLYMARKET, "P1",
            "Will the high temp in Chicago be 75 degrees or above?",
            subtitle="75 or above", strike_type="greater_or_equal", floor="75",
            settlement_source="National Weather Service",
            category=Category.WEATHER,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "strict '>' matched against inclusive '>='")

    def test_interval_market_is_not_a_one_sided_threshold(self) -> None:
        """An 87-88 bucket pays on a band; 'above 87' pays on an unbounded tail."""
        a = market(
            Platform.KALSHI, "K2",
            "Will the high temp in NYC be 87-88 degrees?",
            subtitle="87 to 88", strike_type="between", floor="87", cap="88",
            settlement_source="National Weather Service",
            category=Category.WEATHER,
        )
        b = market(
            Platform.POLYMARKET, "P2",
            "Will the high temp in NYC be above 87 degrees?",
            subtitle="88 or above", strike_type="greater", floor="87",
            settlement_source="National Weather Service",
            category=Category.WEATHER,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "interval bucket matched against a one-sided tail")

    def test_different_thresholds_are_incompatible(self) -> None:
        a = market(
            Platform.KALSHI, "K3", "Will BTC close above $70,000?",
            strike_type="greater", floor="70000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        b = market(
            Platform.POLYMARKET, "P3", "Will BTC close above $75,000?",
            strike_type="greater", floor="75000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        verdict = verify_match(pair(a, b))
        assert verdict.rule_compatibility == RuleCompatibility.INCOMPATIBLE
        assert_not_riskfree(verdict, "different numeric thresholds")


class TestObservationBasis:
    def test_close_price_is_not_intraday_touch(self) -> None:
        """'closes above' and 'trades above at any point' are different questions.

        The touch version is strictly more likely, so treating them as one contract
        systematically misprices the pair.
        """
        a = market(
            Platform.KALSHI, "K4",
            "Will BTC close above $70,000 on Jul 31?",
            description="Settles on the closing price at 4pm ET.",
            strike_type="greater", floor="70000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        b = market(
            Platform.POLYMARKET, "P4",
            "Will BTC reach $70,000 on Jul 31?",
            description="Resolves YES if the price touches $70,000 at any point intraday.",
            strike_type="greater", floor="70000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "closing price matched against an intraday touch")

    def test_different_observation_locations_are_rejected(self) -> None:
        """Same quantity, same threshold, different weather station."""
        a = market(
            Platform.KALSHI, "K5",
            "Will the high temperature in Chicago be above 90 degrees?",
            strike_type="greater", floor="90",
            settlement_source="National Weather Service", category=Category.WEATHER,
        )
        b = market(
            Platform.POLYMARKET, "P5",
            "Will the high temperature in Denver be above 90 degrees?",
            strike_type="greater", floor="90",
            settlement_source="National Weather Service", category=Category.WEATHER,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "different observation locations")


class TestSportsScope:
    def test_regulation_time_is_not_advancement(self) -> None:
        """A 90-minute result and 'advances to the next round' differ on any draw.

        A knockout tie level after 90 minutes settles NO on the match-winner market
        and can still settle YES on advancement via extra time or penalties.
        """
        a = market(
            Platform.KALSHI, "K6",
            "Will Arsenal beat Chelsea?",
            description=(
                "Settles on the result after 90 minutes of regulation play. "
                "Extra time and penalties are not included."
            ),
            settlement_source="official league", category=Category.SPORTS,
        )
        b = market(
            Platform.POLYMARKET, "P6",
            "Will Arsenal beat Chelsea?",
            description=(
                "Resolves YES if Arsenal advances, including extra time and "
                "penalty shootouts."
            ),
            settlement_source="official league", category=Category.SPORTS,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "regulation result matched against advancement")


class TestDataRevisions:
    def test_initial_release_is_not_revised_release(self) -> None:
        """Macro prints get revised; the two contracts settle on different numbers."""
        a = market(
            Platform.KALSHI, "K7",
            "Will CPI year-over-year be above 3 percent?",
            description="Settles on the initial release published by the BLS.",
            strike_type="greater", floor="3",
            settlement_source="Bureau of Labor Statistics", category=Category.ECONOMICS,
        )
        b = market(
            Platform.POLYMARKET, "P7",
            "Will CPI year-over-year be above 3 percent?",
            description=(
                "Settles on the revised figure, using the second estimate published "
                "by the BLS."
            ),
            strike_type="greater", floor="3",
            settlement_source="Bureau of Labor Statistics", category=Category.ECONOMICS,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "initial release matched against a revision")


class TestTiming:
    def test_materially_different_cutoffs_are_rejected(self) -> None:
        a = market(
            Platform.KALSHI, "K8", "Will BTC close above $70,000?",
            strike_type="greater", floor="70000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        b = market(
            Platform.POLYMARKET, "P8", "Will BTC close above $70,000?",
            strike_type="greater", floor="70000", settlement_source="CF Benchmarks",
            resolution_offset_hours=72,  # settles three days later
            category=Category.CRYPTO,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "cutoffs three days apart")


class TestSettlementSource:
    def test_different_settlement_authorities_are_rejected(self) -> None:
        """Two index methodologies can disagree on the same underlying."""
        a = market(
            Platform.KALSHI, "K9", "Will BTC close above $70,000?",
            description="Settles on the CF Benchmarks Bitcoin Real-Time Index.",
            strike_type="greater", floor="70000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        b = market(
            Platform.POLYMARKET, "P9", "Will BTC close above $70,000?",
            description="Settles on the Binance BTC/USDT spot price.",
            strike_type="greater", floor="70000", settlement_source="Binance",
            category=Category.CRYPTO,
        )
        verdict = verify_match(pair(a, b))
        assert_not_riskfree(verdict, "different settlement authorities")


class TestGenuineEquivalence:
    """The guards must not reject everything - a real pair has to survive."""

    def test_identical_terms_survive(self) -> None:
        common = dict(
            description=(
                "Settles on the closing price at 4pm ET as published by CF Benchmarks."
            ),
            strike_type="greater",
            floor="70000",
            settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        a = market(Platform.KALSHI, "K10", "Will BTC close above $70,000 on Jul 31?", **common)
        b = market(Platform.POLYMARKET, "P10", "Will BTC close above $70,000 on Jul 31?", **common)
        verdict = verify_match(pair(a, b))
        assert verdict.rule_compatibility != RuleCompatibility.INCOMPATIBLE, (
            f"a genuinely identical pair was rejected: {verdict.mismatch_reasons}"
        )
        assert verdict.match_confidence > Decimal("0.5")


class TestReasonsAreRecorded:
    def test_every_rejection_states_why(self) -> None:
        """A rejection with no recorded reason cannot be audited or debugged."""
        a = market(
            Platform.KALSHI, "K11", "Will BTC close above $70,000?",
            strike_type="greater", floor="70000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        b = market(
            Platform.POLYMARKET, "P11", "Will ETH close above $4,000?",
            strike_type="greater", floor="4000", settlement_source="CF Benchmarks",
            category=Category.CRYPTO,
        )
        verdict = verify_match(pair(a, b))
        assert verdict.rule_compatibility == RuleCompatibility.INCOMPATIBLE
        assert verdict.mismatch_reasons, "rejection recorded no reason"
