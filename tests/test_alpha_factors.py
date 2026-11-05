"""Factors must refuse rather than default, and mean what their names say.

The property under test throughout is the one that separates a research system
from a plausible one: **a factor that has no data returns nothing, not zero.**

A factor returning 0.0 when it has no input is indistinguishable, downstream,
from one that measured zero. A strategy fitted on that mixture has learned the
shape of the missing data, and nothing in the output says so - the aggregate
simply looks like a weaker signal than it is, or a stronger one, depending on
which way the gaps lean.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.enums import Platform, Side
from pmvl_shared.schemas import NormalizedMarket

from pmvl_markets.alpha.factors import (
    DEFAULT_PRICE_BUCKETS,
    KIND_MARKET_PROBABILITY,
    KIND_SOURCE_ESTIMATE,
    KIND_SOURCE_POINT_VALUE,
)
from pmvl_markets.alpha.pit import AsOfView, observation
from pmvl_markets.alpha.registry import (
    REGISTRY,
    FactorContext,
    FactorFamily,
    Unavailable,
)

from .conftest import make_book

BASE = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def context(
    market: NormalizedMarket,
    *,
    observations=(),  # noqa: ANN001
    decision_at: int = 60,
    book=None,  # noqa: ANN001
    fair: str | None = "0.42",
    low: str | None = "0.38",
    high: str | None = "0.46",
    executable: str | None = "0.30",
    **kw,  # noqa: ANN003
) -> FactorContext:
    return FactorContext(
        market=market,
        view=AsOfView.build(at(decision_at), observations),
        book=book,
        model_fair_probability=Decimal(fair) if fair else None,
        model_fair_probability_low=Decimal(low) if low else None,
        model_fair_probability_high=Decimal(high) if high else None,
        executable_probability=Decimal(executable) if executable else None,
        **kw,
    )


def estimate(minutes: int, value: str, source: str = "nws"):  # noqa: ANN201
    return observation(
        source_id=source,
        kind=KIND_SOURCE_ESTIMATE,
        value=Decimal(value),
        available_at=at(minutes),
    )


def market_print(minutes: int, value: str):  # noqa: ANN201
    return observation(
        source_id="venue",
        kind=KIND_MARKET_PROBABILITY,
        value=Decimal(value),
        available_at=at(minutes),
    )


def run(name: str, ctx: FactorContext):  # noqa: ANN201
    return REGISTRY.spec(name)(ctx)


# ---------------------------------------------------------------------------


class TestTheRegistryItself:
    def test_every_factor_declares_a_family_and_a_version(self) -> None:
        for spec in REGISTRY.specs():
            assert isinstance(spec.family, FactorFamily)
            assert spec.version
            assert spec.description

    def test_the_registry_stamps_its_own_version_onto_results(self) -> None:
        """So a factor cannot report a version it does not have.

        Bumping a version is then one edit in the registration rather than two
        that can disagree.
        """
        market = NormalizedMarket(platform=Platform.KALSHI, platform_market_id="T")
        value = run("source_divergence", context(market))
        assert value.version == REGISTRY.spec("source_divergence").version

    def test_registering_a_duplicate_name_is_an_error(self) -> None:
        """A silent overwrite means two computations shipping under one name.

        A stored historical value could then not be attributed to either.
        """
        from pmvl_markets.alpha.registry import FactorRegistry, FactorSpec

        registry = FactorRegistry()
        spec = FactorSpec(
            name="dup",
            family=FactorFamily.MICROSTRUCTURE,
            version="v0",
            description="",
            compute=lambda ctx: run("source_divergence", ctx),
        )
        registry.register(spec)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(spec)

    def test_a_vector_reports_its_refusals_not_just_its_values(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """"We had no NWS forecast" is the finding that says what to collect."""
        vector = REGISTRY.compute(context(kalshi_market))
        assert vector.unavailable
        assert Unavailable.NO_ORDER_BOOK in vector.unavailable.values()

    def test_number_returns_none_for_an_unavailable_factor_never_a_default(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        vector = REGISTRY.compute(context(kalshi_market))
        assert vector.number("book_spread") is None

    def test_coverage_counts_availability_and_reasons(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Answers "is this strategy narrow because the edge is, or the data is?"

        Two very different findings that look identical in an aggregate P&L.
        """
        vectors = [REGISTRY.compute(context(kalshi_market)) for _ in range(3)]
        coverage = REGISTRY.coverage(vectors)
        assert coverage["book_spread"]["unavailable"] == 3
        assert coverage["book_spread"]["reason:no_order_book"] == 3

    def test_a_factor_that_raises_is_a_bug_and_propagates(self) -> None:
        """Swallowing it would report thin coverage where the code is broken."""
        from pmvl_markets.alpha.registry import FactorRegistry, FactorSpec

        registry = FactorRegistry()

        def explode(ctx: FactorContext):  # noqa: ANN202
            raise RuntimeError("boom")

        registry.register(
            FactorSpec(
                name="boom",
                family=FactorFamily.MICROSTRUCTURE,
                version="v0",
                description="",
                compute=explode,
            )
        )
        market = NormalizedMarket(platform=Platform.KALSHI, platform_market_id="T")
        with pytest.raises(RuntimeError, match="boom"):
            registry.compute(context(market))


class TestSourceDivergence:
    def test_the_gap_is_measured_against_the_EXECUTABLE_probability(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Not the midpoint.

        A midpoint gap is an arithmetic fact about a screen; on a wide book the
        two differ by more than any edge this system has measured.
        """
        value = run("source_divergence", context(kalshi_market, executable="0.30"))
        assert value.value == Decimal("0.1200")
        assert value.components["executable_probability"] == Decimal("0.3000")

    def test_sign_and_magnitude_are_both_kept(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        value = run("source_divergence", context(kalshi_market, executable="0.55"))
        assert value.value < 0
        assert value.components["magnitude"] > 0
        assert value.labels["direction"] == "no"

    def test_no_quote_means_unavailable_not_zero(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        value = run("source_divergence", context(kalshi_market, executable=None))
        assert value.available is False
        assert value.reason is Unavailable.NO_QUOTE

    def test_the_conservative_gap_uses_low_for_yes(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        value = run(
            "source_divergence_conservative",
            context(kalshi_market, fair="0.42", low="0.38", executable="0.30"),
        )
        assert value.value == Decimal("0.0800")  # 0.38 - 0.30

    def test_the_conservative_gap_uses_ONE_MINUS_HIGH_for_no(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Never ``1 - low``.

        That is the optimistic bound and would systematically overstate NO-side
        edge - the same error the value pipeline documents refusing.
        """
        ctx = context(kalshi_market, fair="0.30", low="0.24", high="0.36", executable="0.45")
        value = run("source_divergence_conservative", ctx)
        # NO side: (1 - 0.36) - (1 - 0.45) = 0.64 - 0.55 = 0.09
        assert value.value == Decimal("0.0900")
        assert value.labels["side"] == "no"


class TestSourceLag:
    def test_lag_is_measured_from_availability(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(kalshi_market, observations=[estimate(0, "0.40")], decision_at=90)
        value = run("source_update_lag_seconds", ctx)
        assert value.value == Decimal("5400")

    def test_change_magnitude_needs_two_observations(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        one = context(kalshi_market, observations=[estimate(0, "0.40")])
        assert run("source_change_magnitude", one).reason is Unavailable.INSUFFICIENT_SOURCES

        two = context(
            kalshi_market, observations=[estimate(0, "0.30"), estimate(30, "0.42")]
        )
        value = run("source_change_magnitude", two)
        assert value.value == Decimal("0.1200")
        assert value.components["before"] == Decimal("0.3000")

    def test_market_reaction_needs_prints_on_BOTH_sides_of_the_update(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Assuming the pre-update price is the current one would fabricate the
        exact "market has not reacted" reading the strategy is looking for -
        the most expensive possible place to guess."""
        only_after = context(
            kalshi_market,
            observations=[estimate(10, "0.42"), market_print(20, "0.31")],
        )
        assert (
            run("market_reaction_to_source", only_after).reason
            is Unavailable.INSUFFICIENT_SOURCES
        )

    def test_absorbed_fraction_is_the_quantity_the_thesis_is_about(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            observations=[
                estimate(0, "0.30"),
                market_print(5, "0.30"),
                estimate(10, "0.42"),
                market_print(20, "0.33"),
            ],
            decision_at=30,
        )
        value = run("market_reaction_to_source", ctx)
        assert value.components["market_move"] == Decimal("0.0300")
        assert value.components["source_change"] == Decimal("0.1200")
        assert value.components["absorbed_fraction"] == Decimal("0.2500")

    def test_a_stale_source_fails_the_freshness_gate(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Stale-but-divergent is the ANTI-signal.

        If the market has had a day to absorb a forecast and still disagrees, the
        reasonable reading is that the market knows something the model does not.
        """
        ctx = context(
            kalshi_market, observations=[estimate(0, "0.42")], decision_at=60 * 24
        )
        value = run("source_freshness_ok", ctx)
        assert value.value == 0
        assert value.labels["verdict"] == "stale_source"

    def test_a_fresh_source_passes(self, kalshi_market: NormalizedMarket) -> None:
        ctx = context(kalshi_market, observations=[estimate(0, "0.42")], decision_at=60)
        assert run("source_freshness_ok", ctx).value == 1


class TestThresholdSemantics:
    def test_distance_is_estimate_minus_threshold_in_the_contracts_own_units(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            observations=[
                observation(
                    source_id="nws",
                    kind=KIND_SOURCE_POINT_VALUE,
                    value=Decimal("87.5"),
                    available_at=at(0),
                )
            ],
            threshold_value=Decimal("85"),
            threshold_comparator=">=",
        )
        value = run("threshold_distance", ctx)
        assert value.value == Decimal("2.5")
        assert value.labels["comparator"] == ">="

    def test_no_parsed_threshold_means_unavailable(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(kalshi_market, threshold_value=None)
        assert run("threshold_distance", ctx).reason is Unavailable.NO_THRESHOLD

    def test_the_normalised_z_REFUSES_without_a_measured_error_scale(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """The refusal is the point.

        Dividing by an assumed scale produces a number that looks comparable
        across markets and is not, and a z of 2.0 carries an implied tail
        probability a reader will use even when the docstring says not to.
        """
        ctx = context(
            kalshi_market,
            observations=[
                observation(
                    source_id="nws",
                    kind=KIND_SOURCE_POINT_VALUE,
                    value=Decimal("87.5"),
                    available_at=at(0),
                )
            ],
            threshold_value=Decimal("85"),
        )
        value = run("threshold_distance_z", ctx)
        assert value.available is False
        assert value.reason is Unavailable.NO_ERROR_SCALE

    def test_with_a_measured_scale_it_computes_and_flags_normality(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Even a measured sigma does not license reading z as a Gaussian tail.

        ``normality_validated`` is carried as a label rather than assumed.
        """
        ctx = context(
            kalshi_market,
            observations=[
                observation(
                    source_id="nws",
                    kind=KIND_SOURCE_POINT_VALUE,
                    value=Decimal("87.5"),
                    available_at=at(0),
                )
            ],
            threshold_value=Decimal("85"),
            extra={"error_scale": Decimal("2.5")},
        )
        value = run("threshold_distance_z", ctx)
        assert value.value == Decimal("1")
        assert value.labels["normality_validated"] == "false"

    def test_a_zero_or_negative_scale_is_refused(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            observations=[
                observation(
                    source_id="nws",
                    kind=KIND_SOURCE_POINT_VALUE,
                    value=Decimal("87.5"),
                    available_at=at(0),
                )
            ],
            threshold_value=Decimal("85"),
            extra={"error_scale": Decimal("0")},
        )
        assert run("threshold_distance_z", ctx).reason is Unavailable.NO_ERROR_SCALE


class TestForecastDispersion:
    def test_repeated_pulls_of_one_source_do_not_count_as_agreement(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            observations=[estimate(0, "0.40"), estimate(30, "0.41"), estimate(50, "0.42")],
        )
        value = run("forecast_dispersion", ctx)
        assert value.available is False
        assert value.reason is Unavailable.INSUFFICIENT_SOURCES

    def test_declared_mirrors_are_collapsed_onto_their_upstream(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A vendor republishing NWS is not a second opinion about the weather."""
        ctx = context(
            kalshi_market,
            observations=[
                estimate(0, "0.40", source="nws"),
                estimate(10, "0.40", source="vendor"),
            ],
            extra={"source_mirrors": {"vendor": "nws"}},
        )
        assert run("forecast_dispersion", ctx).reason is Unavailable.INSUFFICIENT_SOURCES

    def test_two_genuinely_independent_sources_produce_a_dispersion(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            observations=[
                estimate(0, "0.40", source="nws"),
                estimate(10, "0.50", source="ecmwf"),
            ],
        )
        value = run("forecast_dispersion", ctx)
        assert value.available is True
        assert value.components["range"] == Decimal("0.1000")
        assert value.components["independent_sources"] == 2

    def test_directional_consensus_counts_sources_against_the_market(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            observations=[
                estimate(0, "0.40", source="a"),
                estimate(10, "0.50", source="b"),
            ],
            executable="0.30",
        )
        value = run("forecast_dispersion", ctx)
        assert value.components["directional_consensus"] == Decimal("1.0000")


class TestMakerVersusTaker:
    def test_they_are_separate_factors_with_different_values(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Trading at the ask is not the same act as posting a maker order."""
        book = make_book(
            yes_asks=[("0.30", "1000")], yes_bids=[("0.26", "1000")]
        )
        ctx = context(kalshi_market, book=book, executable="0.30")
        taker = run("taker_edge", ctx)
        maker = run("maker_proposed_edge", ctx)
        assert taker.available and maker.available
        assert maker.value > taker.value

    def test_the_maker_edge_is_explicitly_NOT_fill_adjusted(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """"Proposed" is load-bearing: this is an edge conditional on a fill
        that has not been shown to happen."""
        book = make_book(yes_asks=[("0.30", "1000")], yes_bids=[("0.26", "1000")])
        value = run("maker_proposed_edge", context(kalshi_market, book=book))
        assert value.labels["fill_applied"] == "false"

    def test_the_fee_difference_is_reported_on_its_own(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """So a strategy earning its edge from the spread can be told apart from
        one earning it from the fee schedule. Different capacities, different
        futures."""
        book = make_book(yes_asks=[("0.50", "1000")], yes_bids=[("0.46", "1000")])
        value = run("maker_taker_fee_difference", context(kalshi_market, book=book))
        assert value.value > 0  # taker pays, this series' maker does not

    def test_the_taker_fee_is_size_dependent(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Kalshi ceils to the whole cent on the whole order.

        A one-lot pays several times the per-contract rate a hundred-lot pays,
        and quoting the large-order rate would understate the burden on exactly
        the trades a research run is most likely to simulate.
        """
        book = make_book(yes_asks=[("0.50", "1000")], yes_bids=[("0.46", "1000")])
        small = run(
            "taker_edge",
            context(kalshi_market, book=book, extra={"research_size": Decimal("1")}),
        )
        large = run(
            "taker_edge",
            context(kalshi_market, book=book, extra={"research_size": Decimal("100")}),
        )
        assert (
            small.components["fee_per_contract"] > large.components["fee_per_contract"]
        )


class TestMicrostructure:
    def test_book_slope_shows_what_size_costs(
        self, kalshi_market: NormalizedMarket, deep_book
    ) -> None:  # noqa: ANN001
        ctx = context(
            kalshi_market, book=deep_book, extra={"research_size": Decimal("300")}
        )
        value = run("book_slope", ctx)
        assert value.value > 0
        assert value.components["top_of_book"] == Decimal("0.40")

    def test_a_partial_fill_is_reported_not_extrapolated(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[("0.40", "10")], yes_bids=[("0.38", "10")])
        ctx = context(
            kalshi_market, book=book, extra={"research_size": Decimal("500")}
        )
        assert run("book_slope", ctx).labels["fully_filled"] == "false"

    def test_imbalance_is_measured_in_dollars_not_contracts(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A contract at 5c and one at 95c commit twenty times different capital."""
        book = make_book(
            yes_asks=[("0.90", "100")], yes_bids=[("0.10", "100")]
        )
        value = run("book_imbalance", context(kalshi_market, book=book))
        assert value.components["bid_depth_usd"] == Decimal("10.00")
        assert value.components["ask_depth_usd"] == Decimal("90.00")
        assert value.value < 0

    def test_no_book_means_unavailable(self, kalshi_market: NormalizedMarket) -> None:
        for name in ("book_spread", "book_imbalance", "book_slope"):
            assert run(name, context(kalshi_market)).reason is Unavailable.NO_ORDER_BOOK


class TestBuckets:
    @pytest.mark.parametrize(
        "price,bucket",
        [("0.05", "0-10c"), ("0.30", "30-40c"), ("0.95", "90-100c")],
    )
    def test_price_buckets_label_the_band(
        self, kalshi_market: NormalizedMarket, price: str, bucket: str
    ) -> None:
        value = run("price_bucket", context(kalshi_market, executable=price))
        assert value.labels["bucket"] == bucket

    def test_the_buckets_are_configurable(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            executable="0.30",
            extra={"price_buckets": (Decimal("0.5"),)},
        )
        assert run("price_bucket", ctx).labels["bucket"] == "0-50c"

    def test_the_default_ladder_is_ten_cent_steps(self) -> None:
        assert DEFAULT_PRICE_BUCKETS[0] == Decimal("0.1")
        assert len(DEFAULT_PRICE_BUCKETS) == 9

    def test_time_buckets_separate_twenty_minutes_from_thirty_days(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A 30c contract with 20 minutes left is not the same economic object
        as a 30c contract with 30 days left."""
        soon = kalshi_market.model_copy(
            update={"expected_resolution_time": at(80)}
        )
        later = kalshi_market.model_copy(
            update={"expected_resolution_time": at(60 + 60 * 24 * 30)}
        )
        assert run("hours_to_resolution", context(soon)).labels["bucket"] == "0-1h"
        assert run("hours_to_resolution", context(later)).labels["bucket"] == ">720h"


class TestCrossVenueAndSiblings:
    def test_cross_venue_divergence_refuses_an_unverified_pair(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Two contracts that read alike and settle differently produce a
        divergence that is a RULE difference. Trading it is taking the short
        side of a settlement definition nobody read.
        """
        ctx = context(
            kalshi_market,
            cross_venue_probabilities=[("polymarket", Decimal("0.40"))],
            cross_venue_verified=False,
        )
        value = run("cross_venue_divergence", ctx)
        assert value.reason is Unavailable.NOT_VERIFIED_EQUIVALENT

    def test_a_verified_pair_produces_a_signed_gap(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            executable="0.30",
            cross_venue_probabilities=[("polymarket", Decimal("0.40"))],
            cross_venue_verified=True,
        )
        value = run("cross_venue_divergence", ctx)
        assert value.value == Decimal("-0.1000")
        assert value.labels["counterparty_platform"] == "polymarket"

    def test_sibling_coherence_requires_a_declared_exhaustive_set(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """Assuming exhaustiveness from the shape of the data would turn a
        partial board into a fake incoherence."""
        ctx = context(
            kalshi_market,
            sibling_probabilities=[("a", Decimal("0.3")), ("b", Decimal("0.4"))],
        )
        assert run("sibling_coherence_error", ctx).reason is Unavailable.NOT_APPLICABLE

    def test_an_overpriced_exhaustive_board_is_detected(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            sibling_probabilities=[
                ("a", Decimal("0.40")),
                ("b", Decimal("0.40")),
                ("c", Decimal("0.35")),
            ],
            extra={"sibling_set_exhaustive": True},
        )
        value = run("sibling_coherence_error", ctx)
        assert value.value == Decimal("0.1500")
        assert value.labels["direction"] == "overpriced"


class TestCatalysts:
    def test_no_schedule_is_refused_rather_than_reported_as_a_long_time(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """"No catalyst known" and "no catalyst for a long time" are opposite
        research conditions."""
        assert (
            run("hours_to_next_catalyst", context(kalshi_market)).reason
            is Unavailable.NO_CATALYST_SCHEDULE
        )

    def test_the_next_future_catalyst_wins(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(
            kalshi_market,
            catalysts=[("late", at(600)), ("soon", at(120)), ("past", at(0))],
        )
        value = run("hours_to_next_catalyst", ctx)
        assert value.labels["catalyst"] == "soon"
        assert value.value == Decimal("1.0000")

    def test_every_catalyst_in_the_past_is_a_refusal(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        ctx = context(kalshi_market, catalysts=[("past", at(0))], decision_at=60)
        assert (
            run("hours_to_next_catalyst", ctx).reason
            is Unavailable.NO_CATALYST_SCHEDULE
        )


class TestCapacity:
    def test_capacity_is_capped_at_the_break_even_price(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        """A book with $40,000 behind it, all above break-even, has no capacity
        for this signal at all - and raw depth would say the opposite."""
        book = make_book(
            yes_asks=[("0.30", "100"), ("0.50", "100000")],
            yes_bids=[("0.28", "100")],
        )
        value = run("executable_capacity_usd", context(kalshi_market, book=book))
        assert value.labels["capped_at_breakeven"] == "true"
        assert value.value == Decimal("30.00")
        assert value.components["total_ask_depth_usd"] > Decimal("30.00")

    def test_without_a_model_estimate_it_reports_total_depth_and_says_so(
        self, kalshi_market: NormalizedMarket
    ) -> None:
        book = make_book(yes_asks=[("0.30", "100")], yes_bids=[("0.28", "100")])
        value = run(
            "executable_capacity_usd",
            context(kalshi_market, book=book, low=None, high=None),
        )
        assert value.labels["capped_at_breakeven"] == "false"
