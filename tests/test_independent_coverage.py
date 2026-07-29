"""Measure whether the independence gate makes the product permanently empty.

The live pipeline produced zero recommendations, which is the gate working: with
no independent estimate, eligibility has no answer and fails closed rather than
recommending against a price the model partly copied.

Safe is not the same as useful. If almost no market can produce an independent
estimate, the honest response is to say so and name the models that would change
it - not to loosen the gate, which restores exactly the circularity it prevents.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pmvl_shared.enums import DataProvenance, MarketStatus, Platform
from pmvl_shared.timeutil import utcnow

from independent_coverage_report import (  # noqa: E402
    INDEPENDENT_MODEL_UNAVAILABLE,
    build_report,
)


def _market(db, mid: str, category: str = "crypto"):  # noqa: ANN001, ANN202
    from pmvl_markets.db_models import Market

    m = Market(
        platform=Platform.KALSHI.value,
        platform_market_id=mid,
        title=mid,
        status=MarketStatus.OPEN.value,
        provenance=DataProvenance.LIVE.value,
        created_at=utcnow(),
        category=category,
        volume_24h=Decimal("100"),
    )
    db.add(m)
    db.flush()
    return m


def _prediction(db, market, *, independent: bool):  # noqa: ANN001
    from pmvl_markets.db_models import ModelPrediction

    db.add(
        ModelPrediction(
            market_id=market.id,
            model_version="ensemble-v1.0.0",
            fair_probability_mean=Decimal("0.5"),
            fair_probability_low=Decimal("0.3"),
            fair_probability_high=Decimal("0.7"),
            market_informed_probability=Decimal("0.5"),
            independent_probability=Decimal("0.45") if independent else None,
            has_independent_prior=independent,
            components=[
                {"name": "crypto_gbm_threshold" if independent else "target_market_reference",
                 "probability": "0.45"}
            ],
            created_at=utcnow(),
        )
    )
    db.flush()


class TestCoverageBuckets:
    def test_the_three_states_are_counted_separately(self, clean_db) -> None:  # noqa: ANN001
        _prediction(clean_db, _market(clean_db, "A"), independent=True)
        _prediction(clean_db, _market(clean_db, "B"), independent=False)
        _market(clean_db, "C")  # never scored

        report = build_report(clean_db)
        assert report["counts"]["independent"] == 1
        assert report["counts"]["market_informed_only"] == 1
        assert report["counts"]["no_model_estimate"] == 1

    def test_unscored_markets_are_excluded_from_the_gate_ratio(self, clean_db) -> None:  # noqa: ANN001
        """The distinction that makes the number mean anything.

        "the scorer has not reached this market" and "the scorer reached it and
        found nothing independent" are different facts, and only the second says
        whether the gate is the binding constraint.
        """
        _prediction(clean_db, _market(clean_db, "A"), independent=True)
        for i in range(9):
            _market(clean_db, f"UNSCORED-{i}")

        report = build_report(clean_db)
        assert report["markets_examined"] == 10
        assert report["markets_scored"] == 1
        assert report["share_of_scored_with_independent_estimate"] == 1.0
        # The all-markets denominator is still reported, but is a different figure.
        assert report["share_with_independent_estimate"] == 0.1

    def test_the_ratio_is_none_when_nothing_was_scored(self, clean_db) -> None:  # noqa: ANN001
        """Not zero. Zero would assert the gate blocked everything, when in fact
        nothing was measured."""
        _market(clean_db, "A")
        assert build_report(clean_db)["share_of_scored_with_independent_estimate"] is None


class TestBreakdowns:
    def test_coverage_is_reported_by_category_platform_and_horizon(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """Which model to build next is a per-category question: weather markets
        failing for want of a forecast feed is a different problem from sports
        markets failing for want of a ratings feed."""
        _prediction(clean_db, _market(clean_db, "A", "crypto"), independent=True)
        _prediction(clean_db, _market(clean_db, "B", "sports"), independent=False)

        report = build_report(clean_db)
        assert report["by_category"]["crypto"]["independent"] == 1
        assert report["by_category"]["sports"]["market_informed_only"] == 1
        assert report["by_platform"]["kalshi"]
        assert report["by_horizon"]

    def test_the_independent_components_that_fired_are_named(self, clean_db) -> None:  # noqa: ANN001
        """So the report says which models are carrying coverage and which are
        contributing nothing."""
        _prediction(clean_db, _market(clean_db, "A"), independent=True)
        assert build_report(clean_db)["independent_components_that_fired"] == {
            "crypto_gbm_threshold": 1
        }

    def test_a_market_informed_component_is_not_counted_as_independent(
        self, clean_db  # noqa: ANN001
    ) -> None:
        _prediction(clean_db, _market(clean_db, "B"), independent=False)
        assert build_report(clean_db)["independent_components_that_fired"] == {}


class TestTheReportRefusesToRecommendLooseningTheGate:
    def test_the_diagnostic_reason_is_named(self, clean_db) -> None:  # noqa: ANN001
        assert build_report(clean_db)["diagnostic_reason"] == INDEPENDENT_MODEL_UNAVAILABLE

    def test_the_interpretation_says_to_add_models_not_relax_the_gate(
        self, clean_db  # noqa: ANN001
    ) -> None:
        text = build_report(clean_db)["interpretation"]
        assert "not relaxing the gate" in text
        assert "cross-platform quote is not independent" in text

    def test_the_all_markets_denominator_carries_a_caveat(self, clean_db) -> None:  # noqa: ANN001
        assert "share_of_scored_" in build_report(clean_db)["caveat"]
