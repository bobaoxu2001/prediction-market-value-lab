"""Cross-platform matching, rule normalization, timezone handling and horizons."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, Platform, RuleCompatibility, SettlementResult, Side
from pmvl_shared.money import D, ONE
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import (
    ensure_utc,
    horizon_for,
    horizons_for,
    hours_until,
    parse_ts,
    utcnow,
)
from pmvl_markets.matching.candidates import generate_candidates
from pmvl_markets.matching.verify import (
    compare_thresholds,
    comparators_match,
    proper_nouns,
    unmatched_proper_nouns,
    verify_match,
)
from pmvl_markets.normalize.rules import normalize_rules, source_family
from pmvl_markets.normalize.text import (
    extract_features,
    extract_numbers,
    normalize_title,
    numbers_after_comparator,
)


class TestTimeHandling:
    def test_parses_iso_with_z(self) -> None:
        parsed = parse_ts("2026-07-25T13:00:00Z")
        assert parsed.tzinfo is not None
        assert parsed.hour == 13

    def test_parses_fractional_seconds(self) -> None:
        assert parse_ts("2026-07-25T13:00:00.123456Z") is not None

    def test_parses_unix_seconds_and_milliseconds(self) -> None:
        seconds = parse_ts(1784952000)
        millis = parse_ts(1784952000000)
        assert seconds == millis

    def test_naive_datetimes_are_treated_as_utc(self) -> None:
        """SQLite drops tzinfo on round-trip; we only ever write UTC."""
        naive = datetime(2026, 7, 25, 13, 0, 0)
        assert ensure_utc(naive).tzinfo == timezone.utc

    def test_non_utc_input_is_converted(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        aware = datetime(2026, 7, 25, 9, 0, 0, tzinfo=eastern)
        assert ensure_utc(aware).hour == 13

    def test_garbage_returns_none(self) -> None:
        assert parse_ts("not a date") is None
        assert parse_ts(None) is None
        assert parse_ts("") is None


class TestHorizonBucketing:
    def test_buckets_by_expected_resolution(self) -> None:
        now = utcnow()
        assert horizon_for(now + timedelta(hours=6), now=now) == "24h"
        assert horizon_for(now + timedelta(days=3), now=now) == "7d"
        assert horizon_for(now + timedelta(days=20), now=now) == "30d"

    def test_beyond_thirty_days_is_unbucketed(self) -> None:
        now = utcnow()
        assert horizon_for(now + timedelta(days=90), now=now) is None

    def test_past_resolution_is_unbucketed(self) -> None:
        now = utcnow()
        assert horizon_for(now - timedelta(hours=1), now=now) is None

    def test_a_24h_market_also_appears_in_7d_and_30d(self) -> None:
        now = utcnow()
        assert horizons_for(now + timedelta(hours=6), now=now) == ("24h", "7d", "30d")
        assert horizons_for(now + timedelta(days=10), now=now) == ("30d",)

    def test_boundary_is_inclusive(self) -> None:
        now = utcnow()
        assert horizon_for(now + timedelta(hours=24), now=now) == "24h"

    def test_missing_resolution_time(self) -> None:
        assert horizon_for(None) is None
        assert horizons_for(None) == ()


class TestTextNormalization:
    def test_expands_entity_aliases(self) -> None:
        assert "new york city" in normalize_title("Will the high temp in NYC exceed 87?")
        assert "bitcoin" in normalize_title("Will BTC hit 100k?")

    def test_strips_interrogative_boilerplate(self) -> None:
        assert not normalize_title("Will the Fed cut rates?").startswith("will the")

    def test_magnitude_suffix_requires_a_currency_marker(self) -> None:
        """'3M Open' is a tournament name, not a $3,000,000 threshold."""
        assert Decimal("3000000") not in extract_numbers("3M Open: will X finish top 10?")
        assert Decimal("150000") in extract_numbers("Will BTC be above $150k?")

    def test_spelled_out_magnitudes_are_honoured(self) -> None:
        assert Decimal("2000000") in extract_numbers("more than 2 million viewers")

    def test_threshold_read_after_the_comparator(self) -> None:
        nums = numbers_after_comparator("3M Open: will the score exceed 12?")
        assert nums == [Decimal("12")]

    def test_features_pick_the_right_threshold(self) -> None:
        features = extract_features("Will the high temp in NYC be above 87 on Jul 25, 2026?")
        assert features.primary_threshold == Decimal("87")
        assert features.comparator == "gt"


class TestRuleNormalization:
    def test_known_settlement_sources_map_to_families(self) -> None:
        assert source_family("CF Benchmarks BRTI") == "cf_benchmarks"
        assert source_family("National Weather Service") == "nws"
        assert source_family("UMA optimistic oracle") == "uma_oracle"

    def test_unknown_source_stays_unknown(self) -> None:
        """No first-word fallback: 'This market will resolve...' must not become a family."""
        assert source_family("This market will resolve according to the outcome.") == ""

    def test_structured_strike_suppresses_text_extraction(self) -> None:
        """A venue comparator with no numeric strike must not borrow one from the title."""
        rules = normalize_rules(
            title="3M Open: Will Chandler Phillips finish top 20?",
            explicit_comparator="gt",
            has_structured_strike=True,
        )
        assert rules.threshold is None

    def test_explicit_threshold_wins(self) -> None:
        rules = normalize_rules(
            title="Will BTC be above $70,000?",
            explicit_threshold=Decimal("74749.99"),
            has_structured_strike=True,
        )
        assert rules.threshold == Decimal("74749.99")

    def test_resolution_hash_is_deterministic(self) -> None:
        kwargs = dict(
            title="Will BTC close above $70,000 on Jul 25?",
            settlement_source="CF Benchmarks",
            explicit_threshold=Decimal("70000"),
            explicit_comparator="gt",
        )
        assert normalize_rules(**kwargs).resolution_hash == normalize_rules(**kwargs).resolution_hash

    def test_hash_changes_when_a_material_term_changes(self) -> None:
        base = normalize_rules(
            title="Will BTC close above $70,000?", settlement_source="CF Benchmarks",
            explicit_threshold=Decimal("70000"), explicit_comparator="gt",
        )
        other = normalize_rules(
            title="Will BTC close above $80,000?", settlement_source="CF Benchmarks",
            explicit_threshold=Decimal("80000"), explicit_comparator="gt",
        )
        assert base.resolution_hash != other.resolution_hash


class TestThresholdComparison:
    def test_three_way_result(self) -> None:
        assert compare_thresholds(D(10), D(10)) == "match"
        assert compare_thresholds(D(10), D(20)) == "differ"
        # Unknown is not a contradiction.
        assert compare_thresholds(None, D(10)) == "unknown"
        assert compare_thresholds(None, None) == "unknown"

    def test_relative_tolerance(self) -> None:
        assert compare_thresholds(D("70000"), D("70000.01")) == "match"
        assert compare_thresholds(D("70000"), D("71000")) == "differ"


class TestComparatorMatching:
    def test_identical_comparators(self) -> None:
        assert comparators_match("gt", "gt", continuous=True) == (True, False)

    def test_strict_and_inclusive_equivalent_for_continuous_quantities(self) -> None:
        assert comparators_match("gt", "gte", continuous=True) == (True, False)

    def test_strict_and_inclusive_differ_for_discrete_counts(self) -> None:
        assert comparators_match("gt", "gte", continuous=False) == (False, False)

    def test_opposite_comparators_invert_polarity(self) -> None:
        assert comparators_match("gt", "lte", continuous=True) == (True, True)


class TestProperNouns:
    def test_extracts_identifying_names(self) -> None:
        nouns = proper_nouns("3M Open: Will Michael Brennan finish top 10 in Round 3?")
        assert "brennan" in nouns and "michael" in nouns
        # Generic furniture is excluded.
        assert "will" not in nouns and "round" not in nouns

    def test_different_people_are_flagged(self) -> None:
        a = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="a",
            title="3M Open: Will Michael Brennan finish top 10 in Round 3?",
        )
        b = NormalizedMarket(
            platform=Platform.POLYMARKET, platform_market_id="b",
            title="Will Michael Kim finish in the Top 10 at the 2026 3M Open?",
        )
        assert "brennan" in unmatched_proper_nouns(a, b)

    def test_same_person_is_not_flagged(self) -> None:
        a = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="a",
            title="Will Scottie Scheffler win the 3M Open?",
        )
        b = NormalizedMarket(
            platform=Platform.POLYMARKET, platform_market_id="b",
            title="Will Scottie Scheffler win the 2026 3M Open?",
        )
        assert unmatched_proper_nouns(a, b) == set()

    def test_name_present_only_in_rules_still_counts(self) -> None:
        a = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="a",
            title="Will Scheffler win?",
        )
        b = NormalizedMarket(
            platform=Platform.POLYMARKET, platform_market_id="b",
            title="Will the favourite win?",
            settlement_rules_raw="Resolves YES if Scheffler wins the tournament.",
        )
        assert unmatched_proper_nouns(a, b) == set()


class TestMatchVerification:
    def _pair(self, **overrides):  # noqa: ANN202
        now = utcnow() + timedelta(hours=12)
        base = dict(
            category=Category.CRYPTO,
            expected_resolution_time=now,
            settlement_source="CF Benchmarks",
            floor_strike=Decimal("70000"),
            strike_type="greater",
        )
        a = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="a",
            title="Will BTC close above $70,000 on Jul 25?", **base,
        )
        b = NormalizedMarket(
            platform=Platform.POLYMARKET, platform_market_id="b",
            title="Will BTC close above $70,000 on Jul 25?",
            **{**base, **overrides},
        )
        candidates = generate_candidates([a], [b])
        return candidates[0] if candidates else None

    def test_matching_markets_are_not_incompatible(self) -> None:
        candidate = self._pair()
        assert candidate is not None
        result = verify_match(candidate)
        assert result.rule_compatibility != RuleCompatibility.INCOMPATIBLE

    def test_different_threshold_is_incompatible(self) -> None:
        candidate = self._pair(floor_strike=Decimal("80000"))
        if candidate is None:
            pytest.skip("blocking rejected the pair before verification, which is also correct")
        result = verify_match(candidate)
        assert result.rule_compatibility == RuleCompatibility.INCOMPATIBLE
        assert any("threshold" in r for r in result.mismatch_reasons)

    def test_far_apart_cutoffs_are_incompatible(self) -> None:
        candidate = self._pair(
            expected_resolution_time=utcnow() + timedelta(days=12)
        )
        if candidate is None:
            pytest.skip("blocking rejected the pair on time plausibility, which is correct")
        result = verify_match(candidate)
        assert result.rule_compatibility == RuleCompatibility.INCOMPATIBLE

    def test_different_settlement_source_downgrades(self) -> None:
        candidate = self._pair(settlement_source="Coinbase spot")
        assert candidate is not None
        result = verify_match(candidate)
        assert result.rule_compatibility != RuleCompatibility.IDENTICAL
        assert any("settlement source" in r for r in result.mismatch_reasons)

    def test_verification_never_promotes(self) -> None:
        """Every check can only lower compatibility."""
        candidate = self._pair(settlement_source="Coinbase spot")
        result = verify_match(candidate)
        assert result.rule_compatibility in (
            RuleCompatibility.SIMILAR, RuleCompatibility.INCOMPATIBLE
        )


class TestSettlementPayout:
    def test_yes_side_payout(self) -> None:
        from pmvl_markets.backtest.settlement import payout_for_side

        assert payout_for_side(Side.YES, ONE) == ONE
        assert payout_for_side(Side.YES, D(0)) == 0

    def test_no_side_payout_is_the_complement(self) -> None:
        from pmvl_markets.backtest.settlement import payout_for_side

        assert payout_for_side(Side.NO, ONE) == 0
        assert payout_for_side(Side.NO, D(0)) == ONE

    def test_fifty_fifty_pays_both_sides_half(self) -> None:
        from pmvl_markets.backtest.settlement import payout_for_side

        assert payout_for_side(Side.YES, D("0.5")) == Decimal("0.5")
        assert payout_for_side(Side.NO, D("0.5")) == Decimal("0.5")

    def test_result_mapping(self) -> None:
        from pmvl_markets.backtest.settlement import result_to_enum

        assert result_to_enum("yes") == SettlementResult.YES
        assert result_to_enum("NO") == SettlementResult.NO
        assert result_to_enum(None) == SettlementResult.UNKNOWN


class TestConservativeBounds:
    def test_no_side_bound_uses_one_minus_high(self) -> None:
        """Using 1-low for NO would be the optimistic bound and overstate edge."""
        from pmvl_shared.schemas import FairProbability
        from pmvl_markets.value.ranking import win_probability_for_side

        fair = FairProbability(
            fair_probability_mean=D("0.60"),
            fair_probability_low=D("0.50"),
            fair_probability_high=D("0.70"),
            model_confidence=D("0.5"),
        )
        yes_mean, yes_low = win_probability_for_side(fair, Side.YES)
        no_mean, no_low = win_probability_for_side(fair, Side.NO)

        assert (yes_mean, yes_low) == (D("0.60"), D("0.50"))
        assert no_mean == D("0.40")
        assert no_low == D("0.30")           # 1 - high
        assert no_low < D("0.50")            # NOT 1 - low = 0.50
