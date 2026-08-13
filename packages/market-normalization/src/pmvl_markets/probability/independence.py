"""What each probability component is allowed to count as evidence.

The ensemble pooled every component into one number and published it as
``fair_probability_mean``. One of those components is ``target_market_reference``,
whose entire content is the price of the market being evaluated. So the published
"fair probability" was partly a restatement of the quote it was being compared
against, and the difference between them - the "edge" - was partly the model
agreeing with itself.

That component is not worthless. As a prior it is well calibrated, because a liquid
prediction market is hard to beat, and as a sanity anchor it catches a model that
has gone somewhere absurd. What it cannot do is *justify trading against the price
it came from*. The fix is not to delete it but to stop letting one number serve two
incompatible purposes.

Three quantities are published instead, and each says what it may be used for:

``independent estimate``
    Pools only components that never saw the target price. This is the only figure
    that can demonstrate an edge.
``market-informed estimate``
    Pools everything, including the target price and cross-venue quotes. Better
    calibrated, and useless for deciding whether the market is wrong.
``conservative decision probability``
    The independent estimate's lower bound after penalties for uncertainty, source
    reliability, staleness and model risk. This is what gates eligibility.

Correlation groups exist because "two independent components" is a claim about the
world, not about the code. Two models reading the same venue's price, or two
research providers summarising the same wire story, are one observation counted
twice, and averaging them shrinks the interval as though evidence had accumulated.
Components sharing a correlation group contribute at most one group's worth of
weight to the independence test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.money import ONE, ZERO, D, safe_div


class IndependenceClass(StrEnum):
    """Whether a component may be used to argue the target market is mispriced."""

    #: Never observes the target market's price, directly or through a transform.
    INDEPENDENT = "independent"
    #: Uses the target price, or a same-venue price that moves with it.
    MARKET_INFORMED = "market_informed"


class SourceType(StrEnum):
    """Where a component's information physically comes from."""

    TARGET_MARKET_PRICE = "target_market_price"
    SAME_VENUE_PRICE = "same_venue_price"
    CROSS_VENUE_PRICE = "cross_venue_price"
    EXTERNAL_REFERENCE_DATA = "external_reference_data"
    STATISTICAL_MODEL = "statistical_model"
    RESEARCH_EVIDENCE = "research_evidence"
    HISTORICAL_BASE_RATE = "historical_base_rate"


#: Source types that disqualify a component from the independent estimate.
#:
#: CROSS_VENUE_PRICE is the interesting case. Another venue's quote on the same
#: question is genuinely a separate observation - different traders, different
#: order flow - but the two venues are arbitraged against each other, so it is not
#: independent *evidence about the world*. It is included in the market-informed
#: estimate and excluded from the independent one.
_MARKET_DEPENDENT_SOURCES = frozenset(
    {
        SourceType.TARGET_MARKET_PRICE,
        SourceType.SAME_VENUE_PRICE,
        SourceType.CROSS_VENUE_PRICE,
    }
)


@dataclass(frozen=True)
class IndependenceMetadata:
    """A component's declaration of what it looked at.

    Every field here is something a reader would otherwise have to infer from the
    component's name, which is how ``target_market_reference`` ended up inside a
    number labelled "fair probability".
    """

    source_type: SourceType
    #: Components sharing a group are one observation. Two models reading the same
    #: venue, or two providers summarising the same wire story, do not accumulate.
    correlation_group: str
    reliability_weight: Decimal = ONE
    known_limitations: str = ""

    @property
    def uses_target_price(self) -> bool:
        return self.source_type is SourceType.TARGET_MARKET_PRICE

    @property
    def uses_same_venue_prices(self) -> bool:
        return self.source_type in (
            SourceType.TARGET_MARKET_PRICE,
            SourceType.SAME_VENUE_PRICE,
        )

    @property
    def uses_cross_venue_prices(self) -> bool:
        return self.source_type is SourceType.CROSS_VENUE_PRICE

    @property
    def independence_class(self) -> IndependenceClass:
        return (
            IndependenceClass.MARKET_INFORMED
            if self.source_type in _MARKET_DEPENDENT_SOURCES
            else IndependenceClass.INDEPENDENT
        )

    @property
    def is_independent(self) -> bool:
        return self.independence_class is IndependenceClass.INDEPENDENT

    def as_dict(self) -> dict[str, object]:
        return {
            "source_type": self.source_type.value,
            "independence_class": self.independence_class.value,
            "uses_target_price": self.uses_target_price,
            "uses_same_venue_prices": self.uses_same_venue_prices,
            "uses_cross_venue_prices": self.uses_cross_venue_prices,
            "correlation_group": self.correlation_group,
            "reliability_weight": str(self.reliability_weight),
            "known_limitations": self.known_limitations,
        }


#: Declared per component name. A component with no entry is treated as
#: MARKET_INFORMED: an undeclared source cannot be asserted to be independent, and
#: defaulting the other way would let a new model silently join the edge-bearing
#: estimate without anyone stating where its information comes from.
COMPONENT_INDEPENDENCE: dict[str, IndependenceMetadata] = {
    # --- market-informed: these all read a price ------------------------------
    "target_market_reference": IndependenceMetadata(
        source_type=SourceType.TARGET_MARKET_PRICE,
        correlation_group="target_venue_price",
        reliability_weight=D("0.9"),
        known_limitations=(
            "This IS the price being evaluated. Well calibrated as a prior, and "
            "structurally incapable of demonstrating that the price is wrong."
        ),
    ),
    "extreme_price_sanity": IndependenceMetadata(
        source_type=SourceType.TARGET_MARKET_PRICE,
        correlation_group="target_venue_price",
        reliability_weight=D("0.5"),
        known_limitations=(
            "A bound derived from the target price itself; it constrains the "
            "estimate but cannot be evidence against the quote it came from."
        ),
    ),
    "sibling_coherence": IndependenceMetadata(
        source_type=SourceType.SAME_VENUE_PRICE,
        correlation_group="target_venue_price",
        reliability_weight=D("0.6"),
        known_limitations=(
            "Other outcomes of the same event on the same venue. Coherence is a "
            "real constraint, but the prices move together with the target."
        ),
    ),
    "related_markets": IndependenceMetadata(
        source_type=SourceType.SAME_VENUE_PRICE,
        correlation_group="target_venue_price",
        reliability_weight=D("0.5"),
        known_limitations=(
            "Related contracts on the same venue share order flow with the target."
        ),
    ),
    "cross_platform_consensus": IndependenceMetadata(
        source_type=SourceType.CROSS_VENUE_PRICE,
        correlation_group="venue_price_complex",
        reliability_weight=D("0.7"),
        known_limitations=(
            "A separate order flow, but arbitraged against the target venue, so it "
            "is not independent evidence about the world."
        ),
    ),
    # --- independent: none of these observe a prediction-market price ----------
    "research_agent": IndependenceMetadata(
        source_type=SourceType.RESEARCH_EVIDENCE,
        correlation_group="research_provider",
        reliability_weight=D("0.6"),
        known_limitations=(
            "Multiple outlets reporting one wire story are one observation; the "
            "correlation group prevents them accumulating. Disabled by default."
        ),
    ),
    "equity_index_gbm_threshold": IndependenceMetadata(
        source_type=SourceType.EXTERNAL_REFERENCE_DATA,
        correlation_group="equity_market_data",
        reliability_weight=D("0.9"),
        known_limitations=(
            "Uses the underlying index, not the contract. Assumes the fitted "
            "volatility regime persists to settlement."
        ),
    ),
    "crypto_gbm_threshold": IndependenceMetadata(
        source_type=SourceType.EXTERNAL_REFERENCE_DATA,
        correlation_group="crypto_spot_feed",
        reliability_weight=D("0.85"),
        known_limitations=(
            "Uses spot and realised volatility from an exchange feed, not the "
            "prediction market. GBM understates jump risk."
        ),
    ),
    "cpi_nowcast_bucket": IndependenceMetadata(
        source_type=SourceType.EXTERNAL_REFERENCE_DATA,
        correlation_group="inflation_nowcast",
        reliability_weight=D("0.8"),
        known_limitations=(
            "The Cleveland Fed's published nowcast, with dispersion measured from "
            "its own past errors. It cannot see the surprise component of a "
            "single print, and the 0.1pp rounding bucket is narrow relative to "
            "that error."
        ),
    ),
    "sports_base_rate": IndependenceMetadata(
        source_type=SourceType.HISTORICAL_BASE_RATE,
        correlation_group="sports_results_feed",
        # The lowest reliability weight of any independent component, and it should
        # be. A win-loss record is a real observation about the world and it is a
        # thin one: it cannot see the starting pitcher, the injury report or the
        # rest day, all of which the market has already priced.
        reliability_weight=D("0.35"),
        known_limitations=(
            "Win-loss record only, via Log5 plus a fixed home-field rate. Ignores "
            "lineups, injuries, rest and travel. Counted from completed games "
            "strictly before the evaluation instant."
        ),
    ),
    "weather_nws_threshold": IndependenceMetadata(
        source_type=SourceType.EXTERNAL_REFERENCE_DATA,
        correlation_group="nws_forecast",
        reliability_weight=D("0.9"),
        known_limitations=(
            "Uses the NWS forecast directly. Settlement granularity is whole "
            "degrees, so threshold comparators matter."
        ),
    ),
    "time_to_resolution": IndependenceMetadata(
        source_type=SourceType.STATISTICAL_MODEL,
        correlation_group="structural_prior",
        reliability_weight=D("0.3"),
        known_limitations=(
            "A weak structural prior from time remaining alone. Independent of "
            "price, but carries almost no information about the outcome."
        ),
    ),
    # The *_unsourced models always return no opinion - they exist to name the
    # feed a category would need - so they never reach the pool. Declared anyway
    # so that a future implementation cannot join the independent set silently.
    "sports_unsourced": IndependenceMetadata(
        source_type=SourceType.STATISTICAL_MODEL,
        correlation_group="sports_ratings_feed",
        reliability_weight=ZERO,
        known_limitations="Not implemented; emits no opinion.",
    ),
    "economics_unsourced": IndependenceMetadata(
        source_type=SourceType.STATISTICAL_MODEL,
        correlation_group="macro_release_feed",
        reliability_weight=ZERO,
        known_limitations="Not implemented; emits no opinion.",
    ),
    "politics_unsourced": IndependenceMetadata(
        source_type=SourceType.STATISTICAL_MODEL,
        correlation_group="polling_aggregate",
        reliability_weight=ZERO,
        known_limitations="Not implemented; emits no opinion.",
    ),
    "generic_unsourced": IndependenceMetadata(
        source_type=SourceType.STATISTICAL_MODEL,
        correlation_group="per_question_research",
        reliability_weight=ZERO,
        known_limitations="Not implemented; emits no opinion.",
    ),
}

#: Used when a component declares nothing. Deliberately market-informed.
UNDECLARED = IndependenceMetadata(
    source_type=SourceType.SAME_VENUE_PRICE,
    correlation_group="undeclared",
    reliability_weight=D("0.3"),
    known_limitations=(
        "This component did not declare its source. It is treated as market-informed "
        "because an undeclared source cannot be asserted to be independent."
    ),
)


def metadata_for(component_name: str) -> IndependenceMetadata:
    return COMPONENT_INDEPENDENCE.get(component_name, UNDECLARED)


@dataclass
class IndependenceReport:
    """Which components backed the independent estimate, and how much they add."""

    independent_names: list[str] = field(default_factory=list)
    market_informed_names: list[str] = field(default_factory=list)
    #: Distinct correlation groups among the independent components. This, not the
    #: component count, is how many observations there really are.
    independent_groups: set[str] = field(default_factory=set)

    @property
    def has_independent_prior(self) -> bool:
        return bool(self.independent_names)

    @property
    def effective_independent_sources(self) -> int:
        return len(self.independent_groups)

    def as_dict(self) -> dict[str, object]:
        return {
            "has_independent_prior": self.has_independent_prior,
            "independent_components": sorted(self.independent_names),
            "market_informed_components": sorted(self.market_informed_names),
            "independent_correlation_groups": sorted(self.independent_groups),
            "effective_independent_sources": self.effective_independent_sources,
        }


def classify(component_names: list[str]) -> IndependenceReport:
    """Split component names into the two classes, collapsing correlation groups."""
    report = IndependenceReport()
    for name in component_names:
        meta = metadata_for(name)
        if meta.is_independent:
            report.independent_names.append(name)
            report.independent_groups.add(meta.correlation_group)
        else:
            report.market_informed_names.append(name)
    return report


#: Widening applied to the independent estimate's interval when it rests on very few
#: genuinely distinct sources. One source is a single point of failure regardless of
#: how confident it claims to be.
_SPARSE_SOURCE_PENALTY: dict[int, Decimal] = {0: D("3.0"), 1: D("1.4"), 2: D("1.15")}


def model_risk_multiplier(report: IndependenceReport) -> Decimal:
    """How much to widen the band for having few independent observations."""
    return _SPARSE_SOURCE_PENALTY.get(report.effective_independent_sources, ONE)


def conservative_decision_probability(
    *,
    independent_low: Decimal | None,
    report: IndependenceReport,
    reliability: Decimal,
    freshness_penalty: Decimal = ZERO,
) -> Decimal | None:
    """The figure eligibility is decided on.

    Deliberately the *lower bound* of the independent estimate, not its mean: a
    recommendation should survive the pessimistic end of our own uncertainty, and
    an edge that only exists at the mean is an edge that exists because we rounded
    in our own favour.

    Returns None when there is no independent estimate at all, which is not a
    probability of zero - it means the question "is this market mispriced" has no
    answer here, and eligibility must fail closed rather than substitute a number.
    """
    if independent_low is None or not report.has_independent_prior:
        return None
    shrink = safe_div(reliability, ONE) if reliability > ZERO else ZERO
    adjusted = independent_low * shrink - freshness_penalty
    return max(ZERO, min(ONE, adjusted))
