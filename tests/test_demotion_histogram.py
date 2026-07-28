"""The histogram must tell a parsing gap apart from a real finding."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import RuleCompatibility
from pmvl_shared.money import D
from pmvl_markets.matching.histogram import DemotionHistogram, build_histogram
from pmvl_markets.matching.verify import DemotionCode, MatchVerdict


def verdict(level: RuleCompatibility, *codes: DemotionCode) -> MatchVerdict:
    return MatchVerdict(
        rule_compatibility=level,
        match_confidence=D("0.5"),
        time_compatibility=D("1"),
        source_compatibility=D("1"),
        polarity_inverted=False,
        outcome_mapping={},
        mismatch_reasons=[c.value for c in codes],
        demotion_codes=[c.value for c in codes],
    )


class TestCodeClassification:
    def test_unknown_codes_are_missing_information(self) -> None:
        for code in (
            DemotionCode.THRESHOLD_UNKNOWN,
            DemotionCode.COMPARATOR_UNKNOWN,
            DemotionCode.CUTOFF_UNKNOWN,
            DemotionCode.SOURCE_UNKNOWN,
            DemotionCode.MEASUREMENT_BASIS_UNKNOWN,
        ):
            assert code.is_missing_information

    def test_differs_codes_are_contradictions(self) -> None:
        for code in (
            DemotionCode.THRESHOLD_DIFFERS,
            DemotionCode.SOURCE_DIFFERS,
            DemotionCode.OVERTIME_DIFFERS,
            DemotionCode.MEASUREMENT_BASIS_DIFFERS,
            DemotionCode.COMPARATOR_INCOMPATIBLE,
            DemotionCode.ENTITY_MISMATCH,
        ):
            assert not code.is_missing_information


class TestDiagnosis:
    def test_parser_gap_is_named_as_an_engineering_gap(self) -> None:
        """Pairs blocked only by missing fields are fixable, not a finding."""
        h = build_histogram(
            [
                verdict(RuleCompatibility.EQUIVALENT, DemotionCode.SOURCE_UNKNOWN),
                verdict(RuleCompatibility.EQUIVALENT, DemotionCode.CUTOFF_UNKNOWN),
            ]
        )
        assert h.blocked_only_by_missing_info == 2
        assert "engineering gap" in h.diagnosis
        assert "not yet a finding" in h.diagnosis

    def test_genuine_contradictions_are_named_as_a_result(self) -> None:
        h = build_histogram(
            [
                verdict(RuleCompatibility.INCOMPATIBLE, DemotionCode.THRESHOLD_DIFFERS),
                verdict(RuleCompatibility.INCOMPATIBLE, DemotionCode.SOURCE_DIFFERS),
            ]
        )
        assert h.blocked_only_by_missing_info == 0
        assert "result, not a bug" in h.diagnosis

    def test_a_pair_with_both_is_not_counted_as_fixable(self) -> None:
        """Better parsing cannot promote a pair that also has a real conflict."""
        h = build_histogram(
            [
                verdict(
                    RuleCompatibility.INCOMPATIBLE,
                    DemotionCode.SOURCE_UNKNOWN,
                    DemotionCode.THRESHOLD_DIFFERS,
                )
            ]
        )
        assert h.blocked_only_by_missing_info == 0
        assert h.missing_information_count == 1
        assert h.contradiction_count == 1

    def test_success_is_reported(self) -> None:
        h = build_histogram([verdict(RuleCompatibility.IDENTICAL)])
        assert h.verified_equivalent == 1
        assert "may back a cross-platform claim" in h.diagnosis

    def test_empty_scan(self) -> None:
        h = DemotionHistogram()
        assert h.pairs_examined == 0
        assert "nothing reached verification" in h.diagnosis
        assert h.missing_information_share == 0


class TestAggregation:
    def test_counts_by_code_and_level(self) -> None:
        h = build_histogram(
            [
                verdict(RuleCompatibility.INCOMPATIBLE, DemotionCode.SOURCE_DIFFERS),
                verdict(RuleCompatibility.INCOMPATIBLE, DemotionCode.SOURCE_DIFFERS),
                verdict(RuleCompatibility.SIMILAR, DemotionCode.THRESHOLD_UNKNOWN),
            ]
        )
        assert h.pairs_examined == 3
        assert h.by_code["source_differs"] == 2
        assert h.by_level["incompatible"] == 2
        assert h.by_level["similar"] == 1

    def test_share_is_a_fraction_of_demotions_not_pairs(self) -> None:
        h = build_histogram(
            [
                verdict(
                    RuleCompatibility.INCOMPATIBLE,
                    DemotionCode.SOURCE_UNKNOWN,
                    DemotionCode.THRESHOLD_DIFFERS,
                    DemotionCode.CUTOFF_DIFFERS_DAYS,
                )
            ]
        )
        assert h.missing_information_share == Decimal(1) / Decimal(3)

    def test_top_codes_are_ordered_and_tagged(self) -> None:
        h = build_histogram(
            [verdict(RuleCompatibility.INCOMPATIBLE, DemotionCode.SOURCE_DIFFERS)] * 3
            + [verdict(RuleCompatibility.SIMILAR, DemotionCode.CUTOFF_UNKNOWN)]
        )
        top = h.top_codes()
        assert top[0] == ("source_differs", 3, "contradiction")
        assert ("cutoff_unknown", 1, "missing_information") in top

    def test_unrecognised_codes_are_ignored_not_crashed_on(self) -> None:
        """A stale persisted code must not take the scan down."""
        stale = MatchVerdict(
            rule_compatibility=RuleCompatibility.SIMILAR,
            match_confidence=D("0.5"),
            time_compatibility=D("1"),
            source_compatibility=D("1"),
            polarity_inverted=False,
            outcome_mapping={},
            demotion_codes=["a_code_from_an_older_release"],
        )
        h = build_histogram([stale])
        assert h.pairs_examined == 1
        assert h.by_code == {}

    def test_serialisation_shape(self) -> None:
        h = build_histogram(
            [verdict(RuleCompatibility.INCOMPATIBLE, DemotionCode.SOURCE_DIFFERS)]
        )
        payload = h.as_dict()
        assert payload["pairs_examined"] == 1
        assert payload["by_code"][0]["kind"] == "contradiction"
        assert "diagnosis" in payload


@pytest.mark.integration
class TestVerifierEmitsCodes:
    def test_real_rejection_carries_a_code(self) -> None:
        """The codes must come from the verifier, not only from hand-built fixtures."""
        import sys

        sys.path.insert(0, "tests")
        from test_settlement_semantics import market, pair
        from pmvl_shared.enums import Category, Platform
        from pmvl_markets.matching.verify import verify_match

        a = market(
            Platform.KALSHI, "H1", "Will BTC close above $70,000?",
            description="Settles on the CF Benchmarks index.",
            strike_type="greater", floor="70000",
            settlement_source="CF Benchmarks", category=Category.CRYPTO,
        )
        b = market(
            Platform.POLYMARKET, "H2", "Will BTC close above $70,000?",
            description="Settles on the Binance spot price.",
            strike_type="greater", floor="70000",
            settlement_source="Binance", category=Category.CRYPTO,
        )
        v = verify_match(pair(a, b))
        assert DemotionCode.SOURCE_DIFFERS.value in v.demotion_codes
        # Codes and human reasons stay parallel so a count can be traced to a message.
        assert len(v.demotion_codes) == len(v.mismatch_reasons)
