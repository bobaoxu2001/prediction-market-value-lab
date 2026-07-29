"""A backtest may only use what the recommendation could have used.

Every leakage bug has the same shape and the same symptom: the strategy looks
better than it was and nothing says why. The failure is silent by construction,
because using more information than was available never raises an error.

Nine vectors are covered, and they are not variations on one mistake - a later
orderbook, a revised CPI print and a later rule version each leak through a
different code path. The last one is the least obvious: reading "the market's
rules" looks like reading a constant.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pmvl_markets.backtest.leakage import (
    IMMUTABLE_PUBLICATION_FIELDS,
    LeakageError,
    Observation,
    admissible,
    assert_no_leakage,
    assert_publication_unchanged,
    latest_admissible,
)

PUBLISHED = datetime(2026, 7, 28, 12, 0)
BEFORE = PUBLISHED - timedelta(hours=1)
AFTER = PUBLISHED + timedelta(hours=1)


class TestTheNineLeakageVectors:
    @pytest.mark.parametrize(
        "kind",
        [
            "orderbook_snapshot",
            "trade",
            "revised_economic_release",
            "weather_observation",
            "settlement_outcome",
            "rule_version",
            "model_version",
            "target_market_price",
            "external_provider_value",
        ],
    )
    def test_a_later_observation_is_inadmissible(self, kind: str) -> None:
        assert admissible([Observation(kind, AFTER)], published_at=PUBLISHED) == []

    @pytest.mark.parametrize(
        "kind",
        [
            "orderbook_snapshot",
            "trade",
            "revised_economic_release",
            "weather_observation",
            "settlement_outcome",
            "rule_version",
            "model_version",
            "target_market_price",
            "external_provider_value",
        ],
    )
    def test_an_earlier_observation_is_admissible(self, kind: str) -> None:
        assert len(admissible([Observation(kind, BEFORE)], published_at=PUBLISHED)) == 1

    def test_an_observation_at_the_exact_publication_instant_is_admissible(self) -> None:
        """The boundary is inclusive: an input observed at publication time was
        available to the call being published."""
        assert admissible(
            [Observation("orderbook_snapshot", PUBLISHED)], published_at=PUBLISHED
        )


class TestTheTemptingQueryIsTheWrongOne:
    def test_the_newest_admissible_book_is_not_the_newest_book(self) -> None:
        """`SELECT ... ORDER BY observed_at DESC LIMIT 1` returns exactly the book
        the recommendation did not have."""
        books = [
            Observation("orderbook_snapshot", BEFORE - timedelta(hours=2), value="stale"),
            Observation("orderbook_snapshot", BEFORE, value="the one it used"),
            Observation("orderbook_snapshot", AFTER, value="the future"),
        ]
        newest_overall = max(books, key=lambda o: o.observed_at)
        chosen = latest_admissible(books, published_at=PUBLISHED)

        assert newest_overall.value == "the future"
        assert chosen.value == "the one it used"

    def test_nothing_admissible_returns_none_rather_than_the_closest(self) -> None:
        """Falling back to the nearest future observation would be leakage wearing
        a helpful face."""
        assert (
            latest_admissible(
                [Observation("orderbook_snapshot", AFTER)], published_at=PUBLISHED
            )
            is None
        )


class TestMissingProvenanceFailsClosed:
    def test_an_untimestamped_observation_is_inadmissible(self) -> None:
        """Absent provenance is not evidence of freshness. Treating it as
        admissible is how a backfilled row silently becomes a look-ahead."""
        assert admissible([Observation("trade", None)], published_at=PUBLISHED) == []

    def test_assert_no_leakage_rejects_a_missing_timestamp(self) -> None:
        with pytest.raises(LeakageError, match="no timestamp"):
            assert_no_leakage([Observation("trade", None)], published_at=PUBLISHED)


class TestAssertNoLeakage:
    def test_a_clean_set_passes(self) -> None:
        assert_no_leakage(
            [Observation("orderbook_snapshot", BEFORE), Observation("trade", BEFORE)],
            published_at=PUBLISHED,
        )

    def test_one_future_observation_among_many_is_caught(self) -> None:
        with pytest.raises(LeakageError, match="settlement_outcome"):
            assert_no_leakage(
                [
                    Observation("orderbook_snapshot", BEFORE),
                    Observation("trade", BEFORE),
                    Observation("settlement_outcome", AFTER),
                ],
                published_at=PUBLISHED,
            )

    def test_the_error_names_what_leaked_and_when(self) -> None:
        with pytest.raises(LeakageError) as exc:
            assert_no_leakage(
                [Observation("revised_economic_release", AFTER)], published_at=PUBLISHED
            )
        assert "revised_economic_release" in str(exc.value)
        assert "2026-07-28T13:00" in str(exc.value)


class TestSettlementOutcomeIsTheWorstCase:
    def test_grading_input_may_not_inform_the_forecast(self) -> None:
        """A settlement is observed strictly after the market it settles. Using it
        as an input produces a perfect strategy and a worthless one."""
        settlement = Observation("settlement_outcome", AFTER, value="YES")
        assert admissible([settlement], published_at=PUBLISHED) == []


class TestRevisedDataIsADistinctVector:
    def test_the_initial_print_is_admissible_and_the_revision_is_not(self) -> None:
        """Both describe the same month. Only one existed when the call was made,
        and a naive query for "the CPI value" returns the revision."""
        initial = Observation("revised_economic_release", BEFORE, value="3.0")
        revision = Observation("revised_economic_release", AFTER, value="3.2")

        usable = admissible([initial, revision], published_at=PUBLISHED)
        assert [o.value for o in usable] == ["3.0"]


class TestRuleVersionLeakage:
    def test_a_later_rule_wording_may_not_regrade_an_earlier_call(self) -> None:
        """The least obvious vector: reading "the market's rules" looks like
        reading a constant, but the venue may have rewritten them since."""
        at_publication = Observation("rule_version", BEFORE, value="above 70000")
        rewritten = Observation("rule_version", AFTER, value="at or above 70000")

        chosen = latest_admissible([at_publication, rewritten], published_at=PUBLISHED)
        assert chosen.value == "above 70000"


class TestPublishedRecordsAreImmutable:
    def _record(self) -> dict:
        return {
            "entry_price_at_recommendation": "0.74",
            "fair_probability": "0.81",
            "independent_probability_at_publication": "0.79",
            "model_version": "ensemble-v1.0.0",
            "parser_version": "1.0.0",
            "rule_version_id": 7,
            "input_data_cutoff": "2026-07-28T12:00:00Z",
        }

    def test_an_unchanged_record_passes(self) -> None:
        record = self._record()
        assert_publication_unchanged(record, dict(record))

    def test_a_rewritten_probability_is_caught(self) -> None:
        """A newer model may produce a research comparison; it may not overwrite
        the forecast that was published. The track record's only value is that it
        cannot be revised after the outcome is known."""
        before = self._record()
        after = dict(before, fair_probability="0.88")

        with pytest.raises(LeakageError, match="fair_probability"):
            assert_publication_unchanged(before, after)

    def test_a_rewritten_model_version_is_caught(self) -> None:
        before = self._record()
        after = dict(before, model_version="ensemble-v2.0.0")

        with pytest.raises(LeakageError, match="model_version"):
            assert_publication_unchanged(before, after)

    def test_a_rewritten_parser_version_is_caught(self) -> None:
        before = self._record()
        after = dict(before, parser_version="2.0.0")

        with pytest.raises(LeakageError, match="parser_version"):
            assert_publication_unchanged(before, after)

    def test_a_moved_cutoff_is_caught(self) -> None:
        """Moving the cutoff forward would retroactively admit inputs the call
        never saw - leakage applied to the record itself."""
        before = self._record()
        after = dict(before, input_data_cutoff="2026-07-29T12:00:00Z")

        with pytest.raises(LeakageError, match="input_data_cutoff"):
            assert_publication_unchanged(before, after)

    def test_every_decision_bearing_field_is_frozen(self) -> None:
        for field in (
            "entry_price_at_recommendation",
            "fair_probability",
            "independent_probability_at_publication",
            "conservative_probability_at_publication",
            "model_version",
            "parser_version",
            "rule_version_id",
            "input_data_cutoff",
            "orderbook_snapshot",
        ):
            assert field in IMMUTABLE_PUBLICATION_FIELDS, f"{field} is not frozen"


class TestTheSnapshotTableStoresWhatIsNeeded:
    def test_the_model_carries_every_frozen_field(self) -> None:
        """A guard over fields the table does not store would pass vacuously."""
        from pmvl_markets.db_models import RecommendationSnapshot

        columns = set(RecommendationSnapshot.__table__.columns.keys())
        for field in IMMUTABLE_PUBLICATION_FIELDS:
            assert field in columns, f"{field} is guarded but never stored"
