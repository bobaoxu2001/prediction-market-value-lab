"""Rule versions must be written by the real ingestion path, not only the recorder.

The table, the recorder and their unit tests existed while live ingestion still
wrote nothing but the single mutable column. The mechanism was present and inert,
which is the most expensive kind of half-finished: it looks done from the outside
and every stored verdict still rests on a wording nobody kept.

These tests go through `upsert_markets`, the function the providers actually call.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import DataProvenance, MarketStatus, Platform
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from pmvl_markets.ingest.store import upsert_markets
from pmvl_markets.matching.rule_completeness import RuleCompleteness
from pmvl_markets.matching.rule_history import current_rules, rule_history


def _market(**overrides) -> NormalizedMarket:  # noqa: ANN003
    base = dict(
        platform=Platform.KALSHI,
        platform_market_id="RULEV-1",
        title="Will BTC be above $70,000 on Jul 31, 2026?",
        subtitle="Bitcoin price",
        status=MarketStatus.OPEN,
        provenance=DataProvenance.LIVE,
        settlement_source="CF Benchmarks BRTI",
        settlement_rules_raw=(
            "If the BRTI is above 70000 at 5 PM EDT on Jul 31, 2026, the market "
            "resolves Yes. If the index is unavailable the market will be voided "
            "and all orders refunded. If the settlement is delayed the market may "
            "be postponed to the next business day."
        ),
        volume_24h=Decimal("1000"),
        quote_observed_at=utcnow(),
        raw={"ticker": "RULEV-1", "rules_primary": "v1"},
    )
    base.update(overrides)
    return NormalizedMarket(**base)


def _market_id(db, key: str = "kalshi:RULEV-1") -> int:  # noqa: ANN001
    return upsert_markets(db, [_market()])[key]


class TestIngestionWritesVersions:
    def test_first_ingest_creates_version_one(self, clean_db) -> None:  # noqa: ANN001
        ids = upsert_markets(clean_db, [_market()])
        market_id = ids["kalshi:RULEV-1"]

        history = rule_history(clean_db, market_id)
        assert len(history) == 1
        assert history[0].version == 1

    def test_every_required_field_is_captured(self, clean_db) -> None:  # noqa: ANN001
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        version = current_rules(clean_db, market_id)

        assert version.raw_title.startswith("Will BTC")
        assert version.raw_subtitle == "Bitcoin price"
        assert "BRTI is above 70000" in version.raw_rules
        assert version.raw_resolution_source == "CF Benchmarks BRTI"
        assert version.source_endpoint.endswith("/markets")
        assert version.fetched_at is not None
        assert version.source_payload_hash
        assert version.parser_version
        assert version.normalized_terms
        assert version.normalized_rule_hash
        assert version.rule_hash
        assert version.platform_metadata["platform"] == "kalshi"

    def test_cancellation_and_postponement_language_is_extracted(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """Neither venue exposes these as structured fields, and a cancellation
        clause nobody captured is a settlement risk nobody can audit."""
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        version = current_rules(clean_db, market_id)

        assert "voided" in version.raw_cancellation_language
        assert "postponed" in version.raw_postponement_language

    def test_a_market_with_full_rules_is_complete(self, clean_db) -> None:  # noqa: ANN001
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        assert current_rules(clean_db, market_id).completeness == RuleCompleteness.COMPLETE.value

    def test_a_market_without_rules_text_is_title_only(self, clean_db) -> None:  # noqa: ANN001
        ids = upsert_markets(
            clean_db,
            [_market(platform_market_id="NORULES", settlement_rules_raw="", settlement_source="")],
        )
        version = current_rules(clean_db, ids["kalshi:NORULES"])
        assert version.completeness == RuleCompleteness.TITLE_ONLY.value

    def test_extraction_confidence_reflects_what_was_pinned_down(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """A fully-parsed threshold market must score above one where only the
        title was understood."""
        full = current_rules(clean_db, upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"])
        thin_id = upsert_markets(
            clean_db,
            [_market(platform_market_id="THIN", settlement_rules_raw="", settlement_source="")],
        )["kalshi:THIN"]
        thin = current_rules(clean_db, thin_id)

        assert full.extraction_confidence > thin.extraction_confidence


class TestIdempotentReingestion:
    def test_an_identical_reingest_creates_no_duplicate(self, clean_db) -> None:  # noqa: ANN001
        """Ingest sees each market every publisher run. A row per sighting would
        bury the handful of real rewrites in thousands of identical records."""
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        for _ in range(4):
            upsert_markets(clean_db, [_market()])

        assert len(rule_history(clean_db, market_id)) == 1

    def test_a_provider_retry_is_idempotent(self, clean_db) -> None:  # noqa: ANN001
        """Retrying is the safe response to a transient error; it must not also be
        the thing that duplicates rule history."""
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        upsert_markets(clean_db, [_market()])
        upsert_markets(clean_db, [_market()])

        history = rule_history(clean_db, market_id)
        assert len(history) == 1
        assert history[0].version == 1

    def test_reingestion_extends_last_observed_without_editing_history(
        self, clean_db  # noqa: ANN001
    ) -> None:
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        first_seen = current_rules(clean_db, market_id).first_observed_at

        upsert_markets(clean_db, [_market()])
        assert current_rules(clean_db, market_id).first_observed_at == first_seen


class TestRewrites:
    def test_changed_wording_creates_version_two_and_keeps_version_one(
        self, clean_db  # noqa: ANN001
    ) -> None:
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        upsert_markets(
            clean_db,
            [_market(settlement_rules_raw="If the BRTI is at or above 70000 at 5 PM EDT, Yes.")],
        )

        history = rule_history(clean_db, market_id)
        assert len(history) == 2
        assert "above 70000" in history[0].raw_rules
        assert "at or above" in history[1].raw_rules
        assert current_rules(clean_db, market_id).version == 2

    def test_a_raw_rewrite_is_kept_even_when_the_normalized_terms_match(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """The case that matters most for reproducibility.

        If the parser extracts the same threshold from reworded text, the
        normalized terms are identical and it is tempting to treat the change as
        noise. But the parse may be right today and wrong after the next parser
        change, and without the new wording there is nothing to re-derive it from.
        """
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        original_terms = current_rules(clean_db, market_id).normalized_rule_hash

        upsert_markets(
            clean_db,
            [
                _market(
                    settlement_rules_raw=(
                        "Resolves Yes if the BRTI is above 70000 at 5 PM EDT on "
                        "Jul 31, 2026. Void and refund if the index is unavailable. "
                        "May be postponed on a delayed settlement."
                    )
                )
            ],
        )

        history = rule_history(clean_db, market_id)
        assert len(history) == 2, "a reworded rule set was discarded as unchanged"
        assert history[1].normalized_rule_hash == original_terms
        assert history[1].raw_rules != history[0].raw_rules


class TestPartialProviderFailure:
    def test_a_healthy_provider_keeps_its_versions_when_another_fails(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """A venue outage must degrade the run, not erase the other venue's
        rule history."""
        kalshi = _market(platform_market_id="K-1")
        poly = _market(
            platform=Platform.POLYMARKET,
            platform_market_id="P-1",
            settlement_source="Polymarket UMA",
        )
        ids = upsert_markets(clean_db, [kalshi, poly])

        # The next run only carries Kalshi, as it would if Polymarket 503'd.
        upsert_markets(clean_db, [kalshi])

        assert len(rule_history(clean_db, ids["kalshi:K-1"])) == 1
        assert len(rule_history(clean_db, ids["polymarket:P-1"])) == 1, (
            "the failed provider's existing rule history was lost"
        )


class TestCompletenessGatesStrictEquivalence:
    def test_title_only_rules_cannot_back_a_strict_claim(self, clean_db) -> None:  # noqa: ANN001
        ids = upsert_markets(
            clean_db,
            [_market(platform_market_id="THIN", settlement_rules_raw="", settlement_source="")],
        )
        version = current_rules(clean_db, ids["kalshi:THIN"])
        completeness = RuleCompleteness(version.completeness)

        assert completeness.supports_strict_equivalence is False
        assert completeness.supports_standard_equivalence is False

    def test_complete_rules_can(self, clean_db) -> None:  # noqa: ANN001
        market_id = upsert_markets(clean_db, [_market()])["kalshi:RULEV-1"]
        completeness = RuleCompleteness(current_rules(clean_db, market_id).completeness)
        assert completeness.supports_strict_equivalence is True
