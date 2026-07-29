"""An estimate may not use the target price to argue that the target price is wrong.

The ensemble pooled every component into one number and published it as
`fair_probability_mean`. One of those components, `target_market_reference`, is the
price of the market being evaluated. The published estimate therefore partly
restated the quote it was compared against, and the difference was partly the model
agreeing with itself.

These tests fix the boundary: what may enter the independent estimate, what may
enter the market-informed one, and what happens to eligibility when no independent
source exists.
"""

from __future__ import annotations

import pytest

from pmvl_shared.money import D, ZERO

from pmvl_markets.probability.independence import (
    COMPONENT_INDEPENDENCE,
    UNDECLARED,
    IndependenceClass,
    SourceType,
    classify,
    conservative_decision_probability,
    metadata_for,
    model_risk_multiplier,
)


class TestTheTargetPriceIsNeverIndependent:
    def test_target_market_reference_is_market_informed(self) -> None:
        """The whole point. This component IS the price being evaluated."""
        meta = metadata_for("target_market_reference")
        assert meta.independence_class is IndependenceClass.MARKET_INFORMED
        assert meta.uses_target_price is True
        assert not meta.is_independent

    def test_it_is_excluded_from_the_independent_set(self) -> None:
        report = classify(["target_market_reference", "research_agent"])
        assert "target_market_reference" not in report.independent_names
        assert "target_market_reference" in report.market_informed_names

    def test_it_may_still_enter_the_market_informed_set(self) -> None:
        """It is not deleted. As a prior it is well calibrated; it just cannot
        justify trading against the price it came from."""
        report = classify(["target_market_reference"])
        assert report.market_informed_names == ["target_market_reference"]

    def test_a_market_reference_alone_yields_no_independent_prior(self) -> None:
        report = classify(["target_market_reference", "cross_platform_consensus"])
        assert report.has_independent_prior is False
        assert report.effective_independent_sources == 0


class TestCrossVenuePricesAreNotIndependentEvidence:
    def test_cross_platform_consensus_is_market_informed(self) -> None:
        """A separate order flow, but arbitraged against the target venue.

        Different traders is not the same as independent evidence about the world:
        if the two venues track each other, the second quote adds little and
        pretending otherwise shrinks the interval as though evidence accumulated.
        """
        meta = metadata_for("cross_platform_consensus")
        assert meta.independence_class is IndependenceClass.MARKET_INFORMED
        assert meta.uses_cross_venue_prices is True
        assert meta.uses_target_price is False


class TestDeclaredNonPriceSourcesAreIndependent:
    @pytest.mark.parametrize(
        "name",
        ["research_agent", "equity_index_gbm_threshold", "weather_nws_threshold", "crypto_gbm_threshold"],
    )
    def test_non_price_components_are_independent(self, name: str) -> None:
        meta = metadata_for(name)
        assert meta.is_independent
        assert meta.uses_target_price is False
        assert meta.uses_same_venue_prices is False

    def test_every_declared_component_states_its_limitations(self) -> None:
        """A source with no stated weakness is a source nobody examined."""
        for name, meta in COMPONENT_INDEPENDENCE.items():
            assert meta.known_limitations.strip(), f"{name} declares no limitations"


class TestUndeclaredSourcesFailClosed:
    def test_an_unknown_component_is_treated_as_market_informed(self) -> None:
        """Defaulting the other way would let a new model silently join the
        edge-bearing estimate with nobody stating where its data comes from."""
        meta = metadata_for("some_model_added_next_quarter")
        assert meta is UNDECLARED
        assert not meta.is_independent

    def test_an_undeclared_component_cannot_create_an_independent_prior(self) -> None:
        report = classify(["some_model_added_next_quarter"])
        assert report.has_independent_prior is False


class TestCorrelationGroupsPreventDoubleCounting:
    def test_components_sharing_a_group_count_once(self) -> None:
        """Two components reading the same source are one observation.

        Counting them twice shrinks the interval as though evidence accumulated,
        which is the statistical version of the circularity this module exists to
        prevent.
        """
        report = classify(["research_agent", "research_agent"])
        assert len(report.independent_names) == 2
        assert report.effective_independent_sources == 1

    def test_distinct_groups_do_accumulate(self) -> None:
        report = classify(["research_agent", "crypto_gbm_threshold"])
        assert report.effective_independent_sources == 2

    def test_fewer_distinct_sources_widens_the_band(self) -> None:
        one = classify(["research_agent"])
        two = classify(["research_agent", "crypto_gbm_threshold"])
        none = classify(["target_market_reference"])

        assert model_risk_multiplier(none) > model_risk_multiplier(one)
        assert model_risk_multiplier(one) > model_risk_multiplier(two)
        assert model_risk_multiplier(two) >= D("1")


class TestConservativeDecisionProbability:
    def test_it_is_none_without_an_independent_estimate(self) -> None:
        """Not zero. Zero is a probability; None means the question "is this market
        mispriced" has no answer here, and eligibility must fail closed rather than
        substitute a number."""
        report = classify(["target_market_reference"])
        assert (
            conservative_decision_probability(
                independent_low=D("0.7"), report=report, reliability=D("0.9")
            )
            is None
        )

    def test_it_never_exceeds_the_independent_lower_bound(self) -> None:
        """It is a penalised lower bound; a penalty that increased it would be a
        rounding in our own favour."""
        report = classify(["research_agent", "crypto_gbm_threshold"])
        low = D("0.60")
        result = conservative_decision_probability(
            independent_low=low, report=report, reliability=D("1")
        )
        assert result is not None and result <= low

    def test_staleness_reduces_it_further(self) -> None:
        report = classify(["research_agent"])
        fresh = conservative_decision_probability(
            independent_low=D("0.60"), report=report, reliability=D("1")
        )
        stale = conservative_decision_probability(
            independent_low=D("0.60"),
            report=report,
            reliability=D("1"),
            freshness_penalty=D("0.05"),
        )
        assert stale < fresh

    def test_it_stays_inside_zero_and_one(self) -> None:
        report = classify(["research_agent"])
        assert (
            conservative_decision_probability(
                independent_low=D("0.01"),
                report=report,
                reliability=D("1"),
                freshness_penalty=D("0.5"),
            )
            == ZERO
        )


class TestSourceTypeBoundary:
    @pytest.mark.parametrize(
        "source",
        [SourceType.TARGET_MARKET_PRICE, SourceType.SAME_VENUE_PRICE, SourceType.CROSS_VENUE_PRICE],
    )
    def test_every_price_derived_source_is_market_informed(self, source: SourceType) -> None:
        from pmvl_markets.probability.independence import IndependenceMetadata

        meta = IndependenceMetadata(source_type=source, correlation_group="g")
        assert meta.independence_class is IndependenceClass.MARKET_INFORMED

    @pytest.mark.parametrize(
        "source",
        [
            SourceType.EXTERNAL_REFERENCE_DATA,
            SourceType.STATISTICAL_MODEL,
            SourceType.RESEARCH_EVIDENCE,
            SourceType.HISTORICAL_BASE_RATE,
        ],
    )
    def test_every_non_price_source_can_be_independent(self, source: SourceType) -> None:
        from pmvl_markets.probability.independence import IndependenceMetadata

        meta = IndependenceMetadata(source_type=source, correlation_group="g")
        assert meta.independence_class is IndependenceClass.INDEPENDENT


class TestTheEnsemblePublishesThreeDistinctNumbers:
    """The split has to survive the real pooling path, not just the classifier.

    Models are injected so the test is deterministic and offline: the real
    external-data models (weather, crypto, equity) need network access and
    correctly return no opinion without it.
    """

    @staticmethod
    def _fixed_model(component_name: str, probability: str, confidence: str = "0.8"):  # noqa: ANN205
        from pmvl_markets.probability.base import ModelEstimate, ProbabilityModel

        class _Fixed(ProbabilityModel):
            name = component_name
            categories = ()
            independent = True

            def applies_to(self, category) -> bool:  # noqa: ANN001
                return True

            async def estimate(self, ctx) -> ModelEstimate:  # noqa: ANN001
                return ModelEstimate(
                    probability=D(probability),
                    confidence=D(confidence),
                    stdev=D("0.05"),
                    detail=f"fixed {probability}",
                )

        return _Fixed()

    @pytest.fixture()
    def market(self, kalshi_market):  # noqa: ANN001, ANN201
        from pmvl_shared.timeutil import utcnow

        return kalshi_market.model_copy(
            update={
                "best_yes_bid": D("0.72"),
                "best_yes_ask": D("0.74"),
                "spread": D("0.02"),
                "quote_observed_at": utcnow(),
            }
        )

    async def _estimate(self, market, models):  # noqa: ANN001, ANN202
        from pmvl_shared.timeutil import utcnow

        from pmvl_markets.probability import ModelContext, ProbabilityEnsemble

        ensemble = ProbabilityEnsemble(models=models)
        try:
            return await ensemble.estimate(ModelContext(market=market, now=utcnow()))
        finally:
            await ensemble.aclose()

    async def test_independent_and_market_informed_differ(self, market) -> None:  # noqa: ANN001
        """`weather_nws_threshold` is declared independent; the target price is not.

        With the two disagreeing, the independent estimate must follow the model
        and the market-informed estimate must be pulled toward the quote. If they
        come out equal, the market price has leaked into the independent figure.
        """
        from pmvl_markets.probability.consensus import ReferencePrior

        out = await self._estimate(
            market,
            [
                ReferencePrior(),
                self._fixed_model("weather_nws_threshold", "0.30"),
            ],
        )
        f = out.fair

        assert f.independent_probability is not None
        assert f.market_informed_probability is not None
        assert f.independent_probability != f.market_informed_probability, (
            "the two estimates are identical, so the target price reached the "
            "independent figure"
        )
        # The independent estimate follows only the model that never saw the quote.
        assert f.independent_probability == pytest.approx(D("0.30"), abs=D("0.02"))
        # The market-informed estimate sits between the model and the market.
        assert D("0.30") < f.market_informed_probability < D("0.74")

    async def test_the_legacy_field_is_the_market_informed_one(self, market) -> None:  # noqa: ANN001
        """`fair_probability_mean` always was the blended number. Naming it is the
        correction; changing what it means would silently move existing clients."""
        from pmvl_markets.probability.consensus import ReferencePrior

        out = await self._estimate(
            market,
            [ReferencePrior(), self._fixed_model("weather_nws_threshold", "0.30")],
        )
        assert out.fair.fair_probability_mean == out.fair.market_informed_probability

    async def test_conservative_probability_sits_below_the_independent_estimate(
        self, market  # noqa: ANN001
    ) -> None:
        from pmvl_markets.probability.consensus import ReferencePrior

        out = await self._estimate(
            market,
            [ReferencePrior(), self._fixed_model("weather_nws_threshold", "0.30")],
        )
        f = out.fair
        assert f.conservative_decision_probability is not None
        assert f.conservative_decision_probability <= f.independent_probability_low
        assert f.independent_probability_low < f.independent_probability

    async def test_price_only_components_yield_no_independent_estimate(
        self, market  # noqa: ANN001
    ) -> None:
        """The failure mode that matters: eligibility must have no answer, rather
        than quietly falling back to the blended number."""
        from pmvl_markets.probability.consensus import ReferencePrior

        out = await self._estimate(market, [ReferencePrior()])
        f = out.fair

        assert f.market_informed_probability is not None
        assert f.independent_probability is None
        assert f.conservative_decision_probability is None
        assert f.has_independent_prior is False

    async def test_every_component_declares_its_source_in_the_output(
        self, market  # noqa: ANN001
    ) -> None:
        from pmvl_markets.probability.consensus import ReferencePrior

        out = await self._estimate(
            market,
            [ReferencePrior(), self._fixed_model("weather_nws_threshold", "0.30")],
        )
        declared = out.fair.component_independence or {}
        assert "target_market_reference" in declared
        assert declared["target_market_reference"]["uses_target_price"] is True
        assert declared["weather_nws_threshold"]["independence_class"] == "independent"
