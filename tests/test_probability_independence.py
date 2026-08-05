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

from pmvl_shared.money import D, ONE, ZERO

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


class TestCryptoPriceBands:
    """A band contract must never be priced as a one-sided threshold.

    The model's own comment names this trap, but the guard only consulted the
    venue's ``strike_type``. On the live board 96 of 344 crypto contracts are
    bands and the venue left ``strike_type`` unset on 49 of them; each fell
    through to the threshold branch, which took the FIRST number in the title and
    discarded the second. "Between $60,000 and $62,000" with spot at $63,784 was
    scored 0.999999 against a market price of 0.016 — and, being an *independent*
    component, that estimate was allowed to create edge.
    """

    def test_a_band_is_detected_from_text_when_the_venue_is_silent(self) -> None:
        from pmvl_markets.probability.categories.crypto import _text_price_band

        for headline, expected in [
            ("Will the price of Bitcoin be between $60,000 and $62,000 on July 31?",
             (D("60000"), D("62000"))),
            ("Bitcoin price on Jul 31? 60,000-62,000", (D("60000"), D("62000"))),
            ("Bitcoin $60,000 to $62,000", (D("60000"), D("62000"))),
        ]:
            assert _text_price_band(headline) == expected, headline

    def test_a_date_range_is_not_a_price_band(self) -> None:
        """The regression this nearly introduced.

        "August 3-9" matches a bare ``\\d+-\\d+``. Read as a band it yields bounds
        of 3 and 70,000, and the model then declines a contract it prices
        correctly — a quieter wrong answer than the one being fixed, but still
        wrong. Both sides of a band must look like money.
        """
        from pmvl_markets.probability.categories.crypto import _text_price_band

        assert _text_price_band("Will Bitcoin reach $70,000 August 3-9?") is None
        assert _text_price_band("Will Bitcoin reach $72,000 August 3-9?") is None
        assert _text_price_band("Will the high temp be 70-75?") is None

    def test_bounds_are_ordered_regardless_of_how_they_are_written(self) -> None:
        from pmvl_markets.probability.categories.crypto import _text_price_band

        assert _text_price_band("Bitcoin $62,000 to $60,000") == (D("60000"), D("62000"))


class TestTheGateDecidesOnTheFigureItDocuments:
    """`conservative_decision_probability` is named as the eligibility figure.

    For as long as it existed the gate read `fair_probability_low` instead - the
    market-informed ensemble's bound, which the target market's own price helps
    produce. Measuring that against the same market's ask is close to circular,
    and it is the mechanism behind this platform never publishing a single live
    recommendation: on the snapshot that exposed it, 98 of 498 decision-ready
    predictions had a higher independent bound than the one being gated on, by as
    much as 28 cents.
    """

    @staticmethod
    def _fair(**overrides):  # noqa: ANN205
        from pmvl_shared.schemas import FairProbability

        base = dict(
            fair_probability_mean=D("0.30"),
            fair_probability_low=D("0.05"),
            fair_probability_high=D("0.60"),
            model_confidence=D("0.5"),
            evidence_quality=ZERO,
            model_version="test",
            has_independent_prior=True,
            independent_probability=D("0.40"),
            independent_probability_low=D("0.25"),
            independent_probability_high=D("0.70"),
            conservative_decision_probability=D("0.22"),
            independence={"conservative_decision_probability_no": "0.18"},
        )
        base.update(overrides)
        return FairProbability(**base)

    def test_yes_uses_the_independent_decision_figure(self) -> None:
        from pmvl_shared.enums import Side
        from pmvl_markets.value.ranking import win_probability_for_side

        _mean, bound = win_probability_for_side(self._fair(), Side.YES)
        assert bound == D("0.22")

    def test_no_uses_its_own_stored_figure_not_one_minus_the_yes_one(self) -> None:
        """`1 - conservative_decision_probability` would be the OPTIMISTIC bound.

        The NO buyer's pessimistic case is YES being more likely than estimated,
        so the figure has to come from `1 - independent_high` put through the same
        shrink. Deriving it from the YES number would hand every NO candidate
        1 - 0.22 = 0.78 and overstate the side's edge enormously.
        """
        from pmvl_shared.enums import Side
        from pmvl_markets.value.ranking import win_probability_for_side

        _mean, bound = win_probability_for_side(self._fair(), Side.NO)
        assert bound == D("0.18")
        assert bound != ONE - D("0.22")

    def test_the_mean_stays_market_informed(self) -> None:
        # The mean drives display and sizing, not admission, and the
        # market-informed figure is the better calibrated of the two.
        from pmvl_shared.enums import Side
        from pmvl_markets.value.ranking import win_probability_for_side

        mean, _bound = win_probability_for_side(self._fair(), Side.YES)
        assert mean == D("0.30")

    def test_it_falls_back_when_there_is_no_independent_estimate(self) -> None:
        from pmvl_shared.enums import Side
        from pmvl_markets.value.ranking import win_probability_for_side

        fair = self._fair(
            has_independent_prior=False,
            conservative_decision_probability=None,
            independence=None,
        )
        _mean, bound = win_probability_for_side(fair, Side.YES)
        assert bound == D("0.05")  # the old market-informed bound

    def test_a_malformed_stored_figure_does_not_crash_the_gate(self) -> None:
        from pmvl_shared.enums import Side
        from pmvl_markets.value.ranking import win_probability_for_side

        fair = self._fair(independence={"conservative_decision_probability_no": "n/a"})
        _mean, bound = win_probability_for_side(fair, Side.NO)
        assert bound == ONE - D("0.60")  # falls back to 1 - high


class TestTheEnsembleOutputSurvivesPersistence:
    """Whatever the ensemble puts in `independence` must reach a JSON column.

    The NO-side decision figure was first stored as a `Decimal`, which
    `json.dumps` cannot encode. Every `score` run then died with "Object of type
    Decimal is not JSON serializable" and the pipeline refused to publish.

    The unit test alongside it passed throughout, because its fixture was written
    by hand with a string in that slot instead of round-tripping the real
    ensemble output. A test that builds its own input cannot catch a defect in
    how the input is produced.
    """

    @pytest.mark.asyncio
    async def test_the_independence_report_is_json_serialisable(self) -> None:
        import json
        from datetime import timedelta

        from pmvl_shared.enums import Category, MarketStatus, Platform
        from pmvl_shared.schemas import NormalizedMarket
        from pmvl_shared.timeutil import utcnow

        from pmvl_markets.probability.base import (
            ModelContext,
            ModelEstimate,
            ProbabilityModel,
        )
        from pmvl_markets.probability.ensemble import ProbabilityEnsemble

        class StubIndependentModel(ProbabilityModel):
            """Borrows a registered independent name so `classify` treats it as one.

            Independence is decided by the component's registered NAME, not by the
            estimate's own flag, so an invented name would be classified
            market-informed and the field under test would stay None - which is
            exactly how the first version of this test passed against the bug.
            """

            name = "crypto_gbm_threshold"
            independent = True

            async def estimate(self, ctx: ModelContext) -> ModelEstimate:
                return ModelEstimate(
                    probability=D("0.40"),
                    confidence=D("0.5"),
                    stdev=D("0.10"),
                    independent=True,
                    detail="stub",
                )

        market = NormalizedMarket(
            platform=Platform.KALSHI,
            platform_market_id="KXTEST-JSON",
            title="Will the test market settle above 100?",
            category=Category.OTHER,
            status=MarketStatus.OPEN,
            accepting_orders=True,
            tick_size=D("0.01"),
            min_order_size=D("1"),
            fee_rate=D("0.07"),
            fee_type="quadratic",
            expected_resolution_time=utcnow() + timedelta(hours=6),
            volume_24h=D("50000"),
        )

        ensemble = ProbabilityEnsemble(models=[StubIndependentModel()])
        try:
            output = await ensemble.estimate(ModelContext(market=market))
        finally:
            await ensemble.aclose()

        report = output.fair.independence
        assert report is not None
        # The field must actually be populated, or this test proves nothing.
        assert report["conservative_decision_probability_no"] is not None

        # The exact operation SQLAlchemy performs on the way into the column.
        json.dumps(report)
        json.dumps(output.fair.component_independence)
