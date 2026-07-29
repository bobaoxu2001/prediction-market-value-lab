"""Settlement rules must be preserved, versioned, and never silently overwritten.

Rules lived in one mutable column, so a venue editing its resolution criteria
overwrote the text every stored verdict had been derived from. Afterwards there
was no way to tell whether a match was verified against the current wording or an
older one, and no way to reproduce the parse.

Completeness matters because a normalized term with no preserved source is an
assertion nobody can check. The committed snapshot has normalized terms for all
1,850 markets and raw rules for 462 - every one synthetic demo data.
"""

from __future__ import annotations

import pytest

from pmvl_markets.matching.rule_completeness import (
    RuleCompleteness,
    classify_completeness,
    diff_rules,
    rule_hash,
)
from pmvl_markets.matching.rule_history import (
    PARSER_VERSION,
    current_rules,
    record_rule_version,
    rule_history,
)


class TestCompletenessClassification:
    def test_raw_source_and_terms_is_complete(self) -> None:
        assert (
            classify_completeness(
                raw_rules="If BTC is above 70000 at 5pm EDT...",
                settlement_source="CF Benchmarks BRTI",
                normalized_terms={"threshold": 70000},
            )
            is RuleCompleteness.COMPLETE
        )

    def test_terms_without_raw_text_is_title_only(self) -> None:
        """The state 1,388 live markets in the deployed snapshot are in: parsed
        terms whose input was not retained."""
        assert (
            classify_completeness(
                raw_rules=None, settlement_source=None, normalized_terms={"threshold": 70000}
            )
            is RuleCompleteness.TITLE_ONLY
        )

    def test_raw_text_without_a_source_is_partial(self) -> None:
        assert (
            classify_completeness(
                raw_rules="some rules", settlement_source=None, normalized_terms=None
            )
            is RuleCompleteness.PARTIAL
        )

    def test_nothing_is_unavailable(self) -> None:
        assert (
            classify_completeness(
                raw_rules="", settlement_source="", normalized_terms=None
            )
            is RuleCompleteness.UNAVAILABLE
        )

    def test_whitespace_only_rules_do_not_count_as_present(self) -> None:
        assert (
            classify_completeness(
                raw_rules="   \n  ", settlement_source="  ", normalized_terms={"a": 1}
            )
            is RuleCompleteness.TITLE_ONLY
        )


class TestCompletenessGatesEquivalenceClaims:
    def test_only_complete_rules_support_a_strict_claim(self) -> None:
        """A strict claim asserts the contracts settle identically in every case.
        That cannot be established from a parse whose source is gone - the parse
        may be right, but "may be right" is not what strict means."""
        assert RuleCompleteness.COMPLETE.supports_strict_equivalence is True
        for weaker in (
            RuleCompleteness.PARTIAL,
            RuleCompleteness.TITLE_ONLY,
            RuleCompleteness.UNAVAILABLE,
        ):
            assert weaker.supports_strict_equivalence is False

    def test_partial_rules_still_support_a_standard_claim(self) -> None:
        assert RuleCompleteness.PARTIAL.supports_standard_equivalence is True

    def test_title_only_supports_no_equivalence_claim(self) -> None:
        assert RuleCompleteness.TITLE_ONLY.supports_standard_equivalence is False


class TestRuleHashing:
    def test_reflowed_whitespace_is_not_a_rule_change(self) -> None:
        """A venue reformatting its page must not invalidate every stored verdict."""
        assert rule_hash("If BTC  is\nabove 70000") == rule_hash("If BTC is above 70000")

    def test_a_changed_comparator_is_a_different_contract(self) -> None:
        assert rule_hash("BTC above 70000") != rule_hash("BTC at or above 70000")

    def test_empty_rules_hash_to_empty(self) -> None:
        assert rule_hash(None) == ""
        assert rule_hash("   ") == ""


class TestMaterialChanges:
    def test_a_threshold_change_is_material(self) -> None:
        change = diff_rules({"threshold_value": 70000}, {"threshold_value": 75000})
        assert change.is_material is True

    def test_a_title_change_alone_is_not_material(self) -> None:
        """Presentation moved; settlement did not."""
        change = diff_rules({"raw_title": "BTC above 70k?"}, {"raw_title": "Bitcoin above $70,000?"})
        assert change.is_material is False

    def test_a_rules_text_change_is_material(self) -> None:
        change = diff_rules({"raw_rules": "a"}, {"raw_rules": "b"})
        assert change.is_material is True
        assert "raw_rules" in change.changed_fields


class TestVersionHistory:
    def _market(self, db):  # noqa: ANN001, ANN202
        from decimal import Decimal

        from pmvl_shared.enums import DataProvenance, MarketStatus, Platform
        from pmvl_shared.timeutil import utcnow

        from pmvl_markets.db_models import Market

        m = Market(
            platform=Platform.KALSHI.value,
            platform_market_id="RULES-1",
            title="Will BTC be above 70000?",
            status=MarketStatus.OPEN.value,
            provenance=DataProvenance.LIVE.value,
            created_at=utcnow(),
            volume_24h=Decimal("100"),
        )
        db.add(m)
        db.flush()
        return m

    def test_the_first_observation_creates_version_one(self, clean_db) -> None:  # noqa: ANN001
        market = self._market(clean_db)
        row = record_rule_version(
            clean_db,
            market_id=market.id,
            raw_rules="If BTC is above 70000 at 5pm.",
            raw_resolution_source="CF Benchmarks",
            normalized_terms={"threshold": 70000},
        )
        assert row.version == 1
        assert row.completeness == RuleCompleteness.COMPLETE.value
        assert row.parser_version == PARSER_VERSION
        assert row.changed_fields is None

    def test_reingesting_identical_rules_does_not_duplicate(self, clean_db) -> None:  # noqa: ANN001
        """The pipeline sees the same market every ten minutes. A row per sighting
        would bury the handful of real rewrites in tens of thousands of records."""
        market = self._market(clean_db)
        for _ in range(5):
            record_rule_version(
                clean_db,
                market_id=market.id,
                raw_rules="If BTC is above 70000 at 5pm.",
                raw_resolution_source="CF Benchmarks",
            )
        assert len(rule_history(clean_db, market.id)) == 1

    def test_reingesting_extends_last_observed_only(self, clean_db) -> None:  # noqa: ANN001
        market = self._market(clean_db)
        first = record_rule_version(
            clean_db, market_id=market.id, raw_rules="rules v1", raw_resolution_source="src"
        )
        original_first_seen = first.first_observed_at
        again = record_rule_version(
            clean_db, market_id=market.id, raw_rules="rules v1", raw_resolution_source="src"
        )
        assert again.id == first.id
        assert again.first_observed_at == original_first_seen

    def test_a_rewrite_appends_a_version_and_preserves_the_old_one(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """The failure this prevents: the old wording vanishing, taking with it
        the basis of every verdict already derived from it."""
        market = self._market(clean_db)
        record_rule_version(
            clean_db,
            market_id=market.id,
            raw_rules="If BTC is above 70000 at 5pm.",
            raw_resolution_source="CF Benchmarks",
        )
        record_rule_version(
            clean_db,
            market_id=market.id,
            raw_rules="If BTC is at or above 70000 at 5pm.",
            raw_resolution_source="CF Benchmarks",
        )

        history = rule_history(clean_db, market.id)
        assert len(history) == 2
        assert history[0].version == 1
        assert "above 70000" in history[0].raw_rules
        assert history[1].version == 2
        assert "at or above" in history[1].raw_rules
        assert current_rules(clean_db, market.id).version == 2

    def test_the_rewrite_records_which_fields_moved(self, clean_db) -> None:  # noqa: ANN001
        market = self._market(clean_db)
        record_rule_version(
            clean_db, market_id=market.id, raw_rules="v1", raw_resolution_source="src A"
        )
        second = record_rule_version(
            clean_db, market_id=market.id, raw_rules="v2", raw_resolution_source="src B"
        )
        assert set(second.changed_fields) >= {"raw_rules", "settlement_source"}

    def test_completeness_is_stored_per_version(self, clean_db) -> None:  # noqa: ANN001
        """A market whose rules were only captured later must not have its earlier,
        thinner version retroactively described as complete."""
        market = self._market(clean_db)
        thin = record_rule_version(
            clean_db, market_id=market.id, normalized_terms={"threshold": 1}
        )
        full = record_rule_version(
            clean_db,
            market_id=market.id,
            raw_rules="full text",
            raw_resolution_source="src",
            normalized_terms={"threshold": 1},
        )
        assert thin.completeness == RuleCompleteness.TITLE_ONLY.value
        assert full.completeness == RuleCompleteness.COMPLETE.value

    def test_the_source_payload_hash_is_recorded_for_reproducibility(
        self, clean_db  # noqa: ANN001
    ) -> None:
        market = self._market(clean_db)
        row = record_rule_version(
            clean_db,
            market_id=market.id,
            raw_rules="v1",
            source_endpoint="https://api.elections.kalshi.com/trade-api/v2/markets",
            source_payload={"ticker": "X", "rules_primary": "v1"},
        )
        assert row.source_payload_hash
        assert row.source_endpoint.endswith("/markets")
