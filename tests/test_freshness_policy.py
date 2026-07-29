"""Different data types go stale at different speeds.

One threshold, `max_quote_age_seconds`, was applied to everything. That is wrong
in both directions simultaneously: a five-minute-old top-of-book quote is already
suspect, while five-minute-old rule text is perfectly current. Using one number
means either rejecting good rule data or accepting stale prices, and the codebase
chose the second.
"""

from __future__ import annotations

import pytest

from pmvl_shared.freshness import (
    POLICIES,
    DataType,
    FreshnessPolicy,
    FreshnessState,
    assess,
    blocking_inputs,
    eligible,
)


class TestThresholdsDifferByType:
    def test_prices_expire_far_sooner_than_rules(self) -> None:
        """The specific pair that made a single global threshold untenable."""
        assert (
            POLICIES[DataType.TOP_OF_BOOK].hard_stale_seconds
            < POLICIES[DataType.RULE_TEXT].hard_stale_seconds
        )

    def test_no_two_blocking_types_share_one_threshold_by_accident(self) -> None:
        blocking = [p for p in POLICIES.values() if p.blocks_eligibility]
        assert len({p.hard_stale_seconds for p in blocking}) > 1, (
            "every blocking type has the same hard threshold, which is the single "
            "global limit this module replaced"
        )

    def test_every_policy_states_its_reasoning(self) -> None:
        for data_type, policy in POLICIES.items():
            assert policy.rationale.strip(), f"{data_type} has no stated rationale"

    def test_thresholds_must_be_ordered(self) -> None:
        with pytest.raises(ValueError):
            FreshnessPolicy(
                data_type=DataType.TOP_OF_BOOK,
                target_cadence_seconds=600,
                soft_stale_seconds=300,  # soft before target
                hard_stale_seconds=1800,
                blocks_eligibility=True,
            )


class TestStateTransitions:
    @pytest.mark.parametrize(
        ("age", "expected"),
        [
            (0, FreshnessState.FRESH),
            (600, FreshnessState.FRESH),
            (601, FreshnessState.AGING),
            (1800, FreshnessState.AGING),
            (1801, FreshnessState.STALE),
        ],
    )
    def test_top_of_book_boundaries(self, age: int, expected: FreshnessState) -> None:
        assert assess(DataType.TOP_OF_BOOK, age).state is expected

    def test_never_observed_is_unavailable_not_stale(self) -> None:
        """Absent data and expired data call for different responses, and merging
        them hides which one happened."""
        assert assess(DataType.TOP_OF_BOOK, None).state is FreshnessState.UNAVAILABLE

    def test_aging_is_still_usable(self) -> None:
        assert FreshnessState.AGING.usable_for_recommendation is True

    def test_stale_and_unavailable_are_not_usable(self) -> None:
        assert FreshnessState.STALE.usable_for_recommendation is False
        assert FreshnessState.UNAVAILABLE.usable_for_recommendation is False


class TestEligibilityFailsClosed:
    def test_a_stale_blocking_input_denies_eligibility(self) -> None:
        assessments = [assess(DataType.TOP_OF_BOOK, 99999)]
        assert eligible(assessments) is False
        assert blocking_inputs(assessments) == ["top_of_book"]

    def test_missing_data_blocks_exactly_as_stale_data_does(self) -> None:
        """Treating "never fetched" as acceptable is how a missing feed becomes an
        invisible one."""
        assert eligible([assess(DataType.TOP_OF_BOOK, None)]) is False

    def test_a_stale_non_blocking_input_does_not_deny_eligibility(self) -> None:
        """Settlement records affect the track record, not live eligibility."""
        assessment = assess(DataType.SETTLEMENT_STATE, 10 * 86400)
        assert assessment.state is FreshnessState.STALE
        assert assessment.blocks_eligibility is False
        assert eligible([assessment]) is True

    def test_stale_rules_block_even_though_the_window_is_generous(self) -> None:
        """A cross-platform equivalence claim rests entirely on the rules."""
        assessment = assess(DataType.RULE_TEXT, 40 * 86400)
        assert assessment.state is FreshnessState.STALE
        assert assessment.blocks_eligibility is True

    def test_one_stale_dependency_among_fresh_ones_still_blocks(self) -> None:
        """Stale dependency propagation: the pipeline is only as fresh as the
        oldest input a recommendation actually rests on."""
        assessments = [
            assess(DataType.TOP_OF_BOOK, 60),
            assess(DataType.MODEL_PREDICTION, 60),
            assess(DataType.EXTERNAL_MODEL_INPUT, 999999),
        ]
        assert eligible(assessments) is False
        assert blocking_inputs(assessments) == ["external_model_input"]

    def test_all_fresh_inputs_are_eligible(self) -> None:
        assessments = [
            assess(DataType.TOP_OF_BOOK, 60),
            assess(DataType.MODEL_PREDICTION, 60),
            assess(DataType.RULE_TEXT, 3600),
        ]
        assert eligible(assessments) is True
        assert blocking_inputs(assessments) == []
