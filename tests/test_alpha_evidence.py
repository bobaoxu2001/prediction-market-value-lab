"""What a result is allowed to claim.

The single most important distinction in this file is between RESEARCH_ONLY and
everything above it. A backtest run on fixture data, or on a period whose
parameters were chosen by looking at that same period, is a check that the
machinery works. It is not evidence about money, and presenting it as though it
were is the failure this whole exercise exists to avoid.

So the status is computed from what is true - synthetic or not, held out or not,
enough settled decisions or not - and never asserted by whoever wrote the report.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_markets.alpha.evidence import (
    MIN_DECISIONS_FOR_EVIDENCE,
    NO_VERIFIED_BACKTEST,
    EvidenceStatus,
    ResearchRecord,
    ResearchSplit,
    assess,
    sufficient_history,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestStatus:
    def test_no_decisions_is_insufficient_data(self) -> None:
        result = assess(
            decisions=0, settled_decisions=0, synthetic=False,
            held_out=True, parameters_fitted_on_this_period=False,
        )
        assert result.status is EvidenceStatus.INSUFFICIENT_DATA

    def test_synthetic_data_caps_at_research_only_no_matter_the_volume(self) -> None:
        """A million fixture trades are a million facts about the fixture."""
        result = assess(
            decisions=1_000_000, settled_decisions=1_000_000, synthetic=True,
            held_out=True, parameters_fitted_on_this_period=False,
        )
        assert result.status is EvidenceStatus.RESEARCH_ONLY
        assert result.may_claim_profitability is False

    def test_too_few_settled_decisions_is_insufficient_data(self) -> None:
        result = assess(
            decisions=200, settled_decisions=MIN_DECISIONS_FOR_EVIDENCE - 1,
            synthetic=False, held_out=True,
            parameters_fitted_on_this_period=False,
        )
        assert result.status is EvidenceStatus.INSUFFICIENT_DATA

    def test_parameters_fitted_on_the_same_period_is_not_out_of_sample(self) -> None:
        result = assess(
            decisions=500, settled_decisions=500, synthetic=False,
            held_out=True, parameters_fitted_on_this_period=True,
        )
        assert result.status is EvidenceStatus.RESEARCH_ONLY
        assert any("in-sample" in limitation for limitation in result.limitations)

    def test_no_held_out_period_is_not_out_of_sample(self) -> None:
        result = assess(
            decisions=500, settled_decisions=500, synthetic=False,
            held_out=False, parameters_fitted_on_this_period=False,
        )
        assert result.status is EvidenceStatus.RESEARCH_ONLY

    def test_frozen_shadow_decisions_without_a_holdout_are_shadow_only(self) -> None:
        result = assess(
            decisions=500, settled_decisions=500, synthetic=False,
            held_out=False, parameters_fitted_on_this_period=False,
            shadow_frozen=True,
        )
        assert result.status is EvidenceStatus.SHADOW_ONLY

    def test_the_only_status_that_licenses_a_profit_claim_is_verified_oos(
        self,
    ) -> None:
        good = assess(
            decisions=500, settled_decisions=500, synthetic=False,
            held_out=True, parameters_fitted_on_this_period=False,
        )
        assert good.status is EvidenceStatus.VERIFIED_OOS
        assert good.may_claim_profitability is True

        weaker = [
            assess(
                decisions=0, settled_decisions=0, synthetic=False,
                held_out=True, parameters_fitted_on_this_period=False,
            ),
            assess(
                decisions=500, settled_decisions=500, synthetic=True,
                held_out=True, parameters_fitted_on_this_period=False,
            ),
            assess(
                decisions=500, settled_decisions=500, synthetic=False,
                held_out=False, parameters_fitted_on_this_period=False,
                shadow_frozen=True,
            ),
        ]
        assert [result.status for result in weaker] == [
            EvidenceStatus.INSUFFICIENT_DATA,
            EvidenceStatus.RESEARCH_ONLY,
            EvidenceStatus.SHADOW_ONLY,
        ]
        assert all(result.may_claim_profitability is False for result in weaker)

    def test_every_limitation_is_stated_not_just_the_binding_one(self) -> None:
        """A reader needs all of them: fixing one does not promote the result."""
        result = assess(
            decisions=5, settled_decisions=5, synthetic=True,
            held_out=False, parameters_fitted_on_this_period=True,
        )
        joined = " ".join(result.limitations)
        assert "synthetic" in joined
        assert "in-sample" in joined
        assert "out-of-sample" in joined
        assert "settled decisions" in joined


class TestTheHeadline:
    def test_a_weak_result_prints_the_constant_sentence(self) -> None:
        """One constant, so every surface prints the same words and none of
        them can soften it into "preliminary results" during a rewrite."""
        record = ResearchRecord(
            strategy="source_lag_convergence",
            evidence=assess(
                decisions=3, settled_decisions=3, synthetic=True,
                held_out=False, parameters_fitted_on_this_period=True,
            ),
            metrics={"net_pnl": "12.34"},
        )
        assert record.as_dict()["headline"] == NO_VERIFIED_BACKTEST

    def test_the_evidence_status_travels_WITH_the_numbers(self) -> None:
        """There is no route by which an ROI reaches a display without the label
        that says what it is worth."""
        record = ResearchRecord(
            strategy="s",
            evidence=assess(
                decisions=1, settled_decisions=1, synthetic=True,
                held_out=False, parameters_fitted_on_this_period=True,
            ),
            metrics={"return_on_deployed": "0.42"},
        )
        blob = record.as_dict()
        assert "evidence" in blob
        assert blob["evidence"]["may_claim_profitability"] is False


class TestResearchSplit:
    def test_out_of_order_boundaries_are_rejected(self) -> None:
        """Overlapping periods make every out-of-sample claim meaningless."""
        with pytest.raises(ValueError, match="out of order"):
            ResearchSplit(
                train_start=BASE,
                train_end=BASE + timedelta(days=30),
                validation_end=BASE + timedelta(days=10),
                test_end=BASE + timedelta(days=60),
            )

    def test_a_timestamp_is_placed_in_exactly_one_phase(self) -> None:
        split = ResearchSplit(
            train_start=BASE,
            train_end=BASE + timedelta(days=30),
            validation_end=BASE + timedelta(days=45),
            test_end=BASE + timedelta(days=60),
        )
        assert split.phase_of(BASE - timedelta(days=1)) == "before"
        assert split.phase_of(BASE + timedelta(days=10)) == "train"
        assert split.phase_of(BASE + timedelta(days=35)) == "validation"
        assert split.phase_of(BASE + timedelta(days=50)) == "test"
        assert split.phase_of(BASE + timedelta(days=100)) == "after"

    def test_the_boundaries_are_printable_beside_the_result(self) -> None:
        """A split nobody can see is a split nobody can check."""
        split = ResearchSplit(
            train_start=BASE,
            train_end=BASE + timedelta(days=30),
            validation_end=BASE + timedelta(days=45),
            test_end=BASE + timedelta(days=60),
        )
        described = split.describe()
        assert set(described) == {"train", "validation", "test"}


class TestSufficientHistory:
    def test_a_short_span_refuses_a_three_way_split_and_says_why(self) -> None:
        """Cutting a two-week window into three produces three windows too short
        to say anything, and calling the last one "out of sample" makes a claim
        the data cannot support."""
        ok, reason = sufficient_history(timedelta(days=14), decisions=1000)
        assert ok is False
        assert "below the 42 needed" in reason

    def test_too_few_decisions_refuses_even_with_a_long_span(self) -> None:
        ok, reason = sufficient_history(timedelta(days=200), decisions=30)
        assert ok is False
        assert str(MIN_DECISIONS_FOR_EVIDENCE * 3) in reason

    def test_enough_of_both_permits_a_split(self) -> None:
        ok, reason = sufficient_history(timedelta(days=200), decisions=1000)
        assert ok is True
        assert reason == ""


class TestTheRepositorysCurrentPosition:
    def test_todays_data_cannot_support_an_economic_claim(self) -> None:
        """The honest statement of where this repository actually stands.

        The published snapshot's 450 recommendation snapshots are all
        ``provenance='demo'`` and every live backtest run has ``n_settled=0``.
        There is no live settled history to run a strategy against, so any
        result today is machinery-checking - which is exactly what the status
        below says.
        """
        result = assess(
            decisions=0,
            settled_decisions=0,
            synthetic=True,
            held_out=False,
            parameters_fitted_on_this_period=True,
        )
        assert result.status is EvidenceStatus.INSUFFICIENT_DATA
        assert result.may_claim_profitability is False


@pytest.mark.integration
class TestAlphaReadiness:
    """The report that says what is missing, in terms somebody can act on.

    "Not enough data" is not actionable. "No public trade prints, so no maker
    order can be simulated as filled" is - it names a table and a collection
    step.
    """

    def test_an_empty_database_blocks_and_names_every_missing_input(
        self, clean_db
    ) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.readiness import assess_readiness

        report = assess_readiness(clean_db).as_dict()
        assert report["headline"] == NO_VERIFIED_BACKTEST
        assert report["evidence_status"] == EvidenceStatus.INSUFFICIENT_DATA.value
        joined = " ".join(report["blockers"])
        assert "source observations" in joined
        assert "settlements" in joined

    def test_the_registered_factor_count_is_reported(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.readiness import assess_readiness

        report = assess_readiness(clean_db).as_dict()
        assert report["counts"]["registered_factors"] > 0

    def test_every_blocker_comes_with_something_to_do_about_it(
        self, clean_db
    ) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.readiness import assess_readiness

        readiness = assess_readiness(clean_db)
        assert readiness.blockers
        assert readiness.next_steps

    def test_a_demo_row_does_not_count_as_evidence(self, clean_db) -> None:  # noqa: ANN001
        """The whole purpose of the report is whether REAL evidence is possible.

        A demo row is not evidence of anything, so it is excluded in the query
        rather than filtered later.
        """
        from pmvl_shared.enums import DataProvenance
        from pmvl_shared.timeutil import utcnow

        from pmvl_markets.alpha.readiness import assess_readiness
        from pmvl_markets.db_models import Settlement

        clean_db.add(
            Settlement(
                platform="kalshi",
                platform_market_id="DEMO-1",
                result="yes",
                provenance=DataProvenance.DEMO,
                created_at=utcnow(),
            )
        )
        clean_db.flush()
        assert assess_readiness(clean_db).live_settlements == 0
