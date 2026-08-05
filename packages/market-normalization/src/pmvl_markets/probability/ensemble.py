"""The fair-probability ensemble.

Combination rule
----------------
Components are pooled in **log-odds space**, weighted by ``weight = confidence``.
Log-odds pooling is used rather than a linear average because probabilities near the
boundaries are not linearly comparable: averaging 0.02 and 0.20 linearly gives 0.11,
which implies a five-fold change in odds is "halfway", whereas log-odds pooling gives
0.063 and respects the multiplicative structure of odds.

The independence rule
---------------------
``has_independent_prior`` is True only when at least one component with
``independent=True`` produced an opinion. When it is False, the ensemble still
publishes a number - the market's own price - but flags that the estimate carries no
information the market does not already have. The ranking layer refuses to build a
recommendation on such an estimate, which is what prevents the system from
manufacturing edge out of its own inputs.

Interval construction
---------------------
The interval is **not** a formal confidence interval - the components are not
independent samples from a common distribution and pretending otherwise would be
dishonest. It is an explicitly conservative uncertainty band combining:

1. each component's own stated standard deviation, weight-averaged;
2. disagreement *between* components (weighted dispersion);
3. a penalty for stale data;
4. a floor that widens as total confidence falls.

Ranking then uses the **lower** bound, so a wide band cannot produce a recommendation.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from pmvl_shared.enums import Category
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ONE, ZERO, clamp_prob, quantize_prob, safe_div
from .independence import (
    classify,
    conservative_decision_probability,
    metadata_for,
    model_risk_multiplier,
)
from pmvl_shared.schemas import FairProbability, ProbabilityComponent
from pmvl_shared.timeutil import utcnow

from ..research.provider import BaseResearchProvider, NullResearchProvider, ResearchResult
from .base import ModelContext, ModelEstimate, ProbabilityModel, no_opinion
from .categories.crypto import CryptoThresholdModel
from .categories.equity import EquityIndexThresholdModel
from .categories.structural import (
    ExtremePriceSanityModel,
    TimeToResolutionModel,
    default_category_models,
)
from .categories.weather import WeatherThresholdModel
from .consensus import (
    CrossPlatformConsensus,
    ReferencePrior,
    RelatedMarketPrior,
    SiblingCoherencePrior,
    implied_from_orderbook,
)

log = get_logger(__name__)

#: Bumped whenever the ensemble's composition or combination rule changes, so that
#: historical predictions remain attributable to the exact model that made them.
MODEL_VERSION = "ensemble-v1.0.0"

_EPS = Decimal("1e-9")

#: Ceiling on a single component's log-odds sigma. 3.0 is already a factor-of-20
#: uncertainty on the odds; beyond that the component is saying nothing at all.
MAX_COMPONENT_LOG_ODDS_SIGMA = 3.0


def to_log_odds(p: Decimal) -> Decimal:
    p = clamp_prob(p)
    return D(str(math.log(float(p) / (1.0 - float(p)))))


def from_log_odds(x: Decimal) -> Decimal:
    try:
        value = 1.0 / (1.0 + math.exp(-float(x)))
    except OverflowError:
        value = 0.0 if x < 0 else 1.0
    return clamp_prob(D(str(value)))


class ResearchModel(ProbabilityModel):
    """Wraps a research provider as a deliberately weak ensemble member.

    The cap is the point: an LLM's stated confidence is a property of its prose, not
    of the world. Weight is earned by the *evidence* returned - dated, novel, quality
    sources - and is bounded well below the data-driven models regardless of how
    certain the response sounds.
    """

    name = "research_agent"
    independent = True
    #: Hard ceiling. Even perfect-looking research cannot outvote a real data model.
    max_confidence = Decimal("0.35")

    def __init__(self, provider: BaseResearchProvider | None = None) -> None:
        self._provider = provider or NullResearchProvider()
        self.last_result: ResearchResult | None = None

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        result = await self._provider.research(ctx.market)
        self.last_result = result
        if result is None:
            return no_opinion("no research provider configured or no result returned")
        if result.probability is None:
            return no_opinion("research returned evidence but no probability")

        quality = result.evidence_quality()
        if quality <= 0:
            return no_opinion("research returned a probability with no supporting sources")

        # Confidence is the *minimum* of what the model claims and what its evidence
        # justifies, then capped. Fluent prose with thin sourcing scores low.
        confidence = min(
            self.max_confidence,
            result.self_reported_confidence * quality,
        )
        if confidence <= 0:
            return no_opinion("research confidence collapsed to zero after quality weighting")

        return ModelEstimate(
            probability=result.probability,
            confidence=quantize_prob(confidence),
            stdev=Decimal("0.12"),
            independent=True,
            detail=f"research ({len(result.sources)} sources, quality={quality:.2f})",
            data={
                "decisive_question": result.decisive_question[:300],
                "n_sources": len(result.sources),
                "evidence_quality": str(quality),
                "self_reported_confidence": str(result.self_reported_confidence),
            },
        )


@dataclass
class EnsembleOutput:
    fair: FairProbability
    research: ResearchResult | None = None


class ProbabilityEnsemble:
    """Runs every applicable model and pools their opinions."""

    def __init__(
        self,
        models: Sequence[ProbabilityModel] | None = None,
        *,
        research_provider: BaseResearchProvider | None = None,
    ) -> None:
        self._research_model = ResearchModel(research_provider)
        self._models: list[ProbabilityModel] = list(models) if models is not None else [
            CrossPlatformConsensus(),
            SiblingCoherencePrior(),
            RelatedMarketPrior(),
            CryptoThresholdModel(),
            EquityIndexThresholdModel(),
            WeatherThresholdModel(),
            *default_category_models(),
            ExtremePriceSanityModel(),
            TimeToResolutionModel(),
            ReferencePrior(),
            self._research_model,
        ]

    async def aclose(self) -> None:
        await asyncio.gather(*(m.aclose() for m in self._models), return_exceptions=True)

    def applicable(self, category: Category) -> list[ProbabilityModel]:
        return [m for m in self._models if not m.categories or category in m.categories]

    async def estimate(self, ctx: ModelContext) -> EnsembleOutput:
        models = self.applicable(ctx.market.category)
        results = await asyncio.gather(
            *(m.estimate(ctx) for m in models), return_exceptions=True
        )

        components: list[ProbabilityComponent] = []
        opinions: list[tuple[ProbabilityModel, ModelEstimate]] = []
        freshness: list[int] = []

        for model, result in zip(models, results):
            if isinstance(result, Exception):
                # Warning, not debug: a crashing model degrades silently into "no
                # opinion", which looks identical to a model that correctly declined.
                # That hid a naive/aware datetime TypeError in the equity model for a
                # full pipeline run - every index market fell back to its own price.
                log.warning(
                    "model %s raised %s: %s",
                    model.name, type(result).__name__, result,
                )
                components.append(
                    ProbabilityComponent(
                        name=model.name, probability=None, weight=ZERO,
                        confidence=ZERO, detail=f"error: {result}",
                    )
                )
                continue
            if result.data_freshness_seconds is not None:
                freshness.append(result.data_freshness_seconds)
            if result.has_opinion:
                opinions.append((model, result))
            else:
                components.append(result.to_component(model.name, ZERO))

        market_implied = implied_from_orderbook(ctx)

        if not opinions:
            # Nothing at all had a view, not even the market's own price.
            fair = FairProbability(
                fair_probability_mean=market_implied or Decimal("0.5"),
                fair_probability_low=ZERO,
                fair_probability_high=ONE,
                model_confidence=ZERO,
                evidence_quality=ZERO,
                model_version=MODEL_VERSION,
                probability_explanation=(
                    "No model produced an estimate for this market; no fair value is "
                    "claimed."
                ),
                category=ctx.market.category,
                has_independent_prior=False,
                market_implied_probability=market_implied,
                components=components,
            )
            return EnsembleOutput(fair=fair, research=self._research_model.last_result)

        total_weight = sum((r.confidence for _, r in opinions), ZERO)
        pooled_log_odds = ZERO
        for _model, result in opinions:
            weight = safe_div(result.confidence, total_weight)
            pooled_log_odds += weight * to_log_odds(result.probability)  # type: ignore[arg-type]
        mean = from_log_odds(pooled_log_odds)

        # Independence is decided by the declared source in
        # `probability.independence`, not by a per-model boolean. The boolean said a
        # cross-platform quote counted as independent evidence; it is a separate
        # order flow, but the two venues are arbitraged against each other, so it is
        # not independent evidence *about the world*. The declaration is stricter
        # and eligibility fails closed, which is the right direction for a gate.

        # ---- the independent subset ----------------------------------------
        # `mean` above pools EVERY component, including target_market_reference,
        # whose entire content is the price being evaluated. Published as "fair
        # probability" it made the model partly agree with itself and called the
        # residual an edge. The same components are pooled again here with the
        # market-dependent ones removed, so the two questions - "what is this worth"
        # and "is this price wrong" - stop sharing one number.
        independence = classify([model.name for model, _ in opinions])
        has_independent = independence.has_independent_prior
        independent_opinions = [
            (model, result)
            for model, result in opinions
            if metadata_for(model.name).is_independent
        ]
        independent_mean: Decimal | None = None
        if independent_opinions:
            ind_weight = sum((r.confidence for _, r in independent_opinions), ZERO)
            if ind_weight > ZERO:
                ind_log_odds = ZERO
                for _model, result in independent_opinions:
                    w = safe_div(result.confidence, ind_weight)
                    ind_log_odds += w * to_log_odds(result.probability)  # type: ignore[arg-type]
                independent_mean = from_log_odds(ind_log_odds)

        # ---- interval -------------------------------------------------------
        # Built in LOG-ODDS space, the same space the mean is pooled in.
        #
        # An additive band in probability space is wrong near the boundaries and was
        # silently destroying every low-probability estimate: a contract with a fair
        # value of 1.5% and a 0.02 sigma got `low = 0.015 - 0.25 -> 0`, so its
        # conservative EV was always negative and it could never be recommended no
        # matter how good the model was. Working in log-odds gives an asymmetric band
        # that respects [0, 1] and stays informative in the tails.
        #
        # Component sigmas are stated in probability units, so they are converted by
        # the delta method: d(logit)/dp = 1 / (p(1-p)).
        component_sigma = ZERO
        for _model, result in opinions:
            weight = safe_div(result.confidence, total_weight)
            probability = result.probability or mean
            p_float = float(probability)
            slope = max(p_float * (1.0 - p_float), 1e-4)
            # A component's stated probability-space sigma is only meaningful up to
            # the Bernoulli scale at its own mean: claiming sigma = 0.08 around a mean
            # of 0.0004 is not a coherent statement, and the delta method turns it
            # into a log-odds sigma of 200, which flattens the interval to [0, 1] and
            # makes every tail estimate unusable.
            stated = float(result.stdev or Decimal("0.08"))
            bounded = min(stated, math.sqrt(slope))
            sigma_logit = min(bounded / slope, MAX_COMPONENT_LOG_ODDS_SIGMA)
            component_sigma += weight * D(str(sigma_logit))

        # Dispersion between components, also in log-odds.
        mean_log_odds = to_log_odds(mean)
        dispersion_sq = ZERO
        for _model, result in opinions:
            weight = safe_div(result.confidence, total_weight)
            diff = to_log_odds(result.probability or mean) - mean_log_odds
            dispersion_sq += weight * (diff * diff)
        dispersion_logit = D(str(math.sqrt(max(0.0, float(dispersion_sq)))))

        sigma = component_sigma + dispersion_logit

        # Staleness penalty.
        max_age = max(freshness) if freshness else 0
        if max_age > 3600:
            sigma *= D("1.3")
        elif max_age > 900:
            sigma *= D("1.15")

        # Dispersion for the confidence calculation is reported in probability units,
        # where the 0.15 half-life used by aggregate_confidence is calibrated.
        dispersion_prob = ZERO
        for _model, result in opinions:
            weight = safe_div(result.confidence, total_weight)
            diff = (result.probability or mean) - mean
            dispersion_prob += weight * (diff * diff)
        dispersion_prob = D(str(math.sqrt(max(0.0, float(dispersion_prob)))))

        confidence = aggregate_confidence(
            [r.confidence for _, r in opinions], dispersion=dispersion_prob
        )
        # Low aggregate confidence must widen the band, never narrow it. One log-odds
        # unit at zero confidence is roughly a factor-of-e uncertainty on the odds.
        sigma = max(sigma, (ONE - confidence) * D("1.0"))
        # An estimate resting only on the market's own price gets a floor wide enough
        # that no edge can survive the conservative lower bound.
        if not has_independent:
            sigma = max(sigma, D("2.0"))

        # 1.28 sigma ~ an 80% band; deliberately not 1.96, because the components are
        # not independent draws and a 95% claim would overstate rigour.
        half_width = D("1.2816") * sigma
        low = quantize_prob(from_log_odds(mean_log_odds - half_width))
        high = quantize_prob(from_log_odds(mean_log_odds + half_width))

        # The independent estimate's own band. It is widened by model risk, because
        # an estimate resting on one correlation group is a single point of failure
        # however confident that one source claims to be.
        independent_low = independent_high = None
        conservative = conservative_no = None
        if independent_mean is not None:
            ind_sigma = sigma * model_risk_multiplier(independence)
            ind_half = D("1.2816") * ind_sigma
            ind_log_odds = to_log_odds(independent_mean)
            independent_low = quantize_prob(from_log_odds(ind_log_odds - ind_half))
            independent_high = quantize_prob(from_log_odds(ind_log_odds + ind_half))
            reliability = max(
                (metadata_for(m.name).reliability_weight for m, _ in independent_opinions),
                default=ZERO,
            )
            freshness_penalty = D("0.02") if max_age > 3600 else ZERO
            conservative = conservative_decision_probability(
                independent_low=independent_low,
                report=independence,
                reliability=reliability,
                freshness_penalty=freshness_penalty,
            )
            # The same figure for a NO buyer, whose pessimistic case is the one
            # where YES is MORE likely than estimated.
            #
            # It has to be computed here rather than derived downstream: it is
            # `1 - independent_high` put through the same reliability shrink and
            # freshness penalty, and neither of those inputs survives into the
            # stored prediction. Deriving it from the YES figure instead would
            # need `1 - low`, which is the optimistic bound and would overstate
            # every NO-side edge - the exact error `win_probability_for_side`
            # already warns about for the market-informed bounds.
            conservative_no = conservative_decision_probability(
                independent_low=(
                    ONE - independent_high if independent_high is not None else None
                ),
                report=independence,
                reliability=reliability,
                freshness_penalty=freshness_penalty,
            )

        research_result = self._research_model.last_result
        evidence_quality = (
            research_result.evidence_quality() if research_result else ZERO
        )

        for model, result in opinions:
            components.append(
                result.to_component(model.name, quantize_prob(safe_div(result.confidence, total_weight)))
            )

        explanation = _explain(
            mean, opinions, has_independent, market_implied, confidence
        )

        fair = FairProbability(
            fair_probability_mean=quantize_prob(mean),
            fair_probability_low=low,
            fair_probability_high=high,
            model_confidence=quantize_prob(confidence),
            data_freshness_seconds=max_age if freshness else None,
            evidence_quality=quantize_prob(evidence_quality),
            model_version=MODEL_VERSION,
            probability_explanation=explanation,
            category=ctx.market.category,
            has_independent_prior=has_independent,
            market_implied_probability=market_implied,
            components=components,
            # Three distinct quantities, each labelled with what it may be used for.
            # `fair_probability_mean` is retained for existing clients but is the
            # market-informed figure and is now named as such alongside it.
            market_informed_probability=quantize_prob(mean),
            independent_probability=(
                quantize_prob(independent_mean) if independent_mean is not None else None
            ),
            independent_probability_low=independent_low,
            independent_probability_high=independent_high,
            conservative_decision_probability=(
                quantize_prob(conservative) if conservative is not None else None
            ),
            # The NO-side decision figure rides in the independence report rather
            # than in its own column: it belongs to that report, and a JSON field
            # already exists for it. `conservative_decision_probability` remains
            # the YES-side column it has always been.
            independence={
                **independence.as_dict(),
                # A string, not a Decimal: this dict is persisted to a JSON
                # column and `json.dumps` cannot encode Decimal. Storing the
                # Decimal made the whole `score` job fail with "Object of type
                # Decimal is not JSON serializable", which took the pipeline down
                # rather than corrupting a snapshot - the DAG failing closed is
                # working as designed, but the value has to be encodable.
                #
                # String rather than float, for the same reason every other money
                # and probability figure crossing a boundary here is a string:
                # float() would reintroduce the representation error the Decimal
                # core exists to avoid.
                "conservative_decision_probability_no": (
                    str(quantize_prob(conservative_no))
                    if conservative_no is not None
                    else None
                ),
            },
            component_independence={
                model.name: metadata_for(model.name).as_dict() for model, _ in opinions
            },
        )
        return EnsembleOutput(fair=fair, research=research_result)


def aggregate_confidence(
    confidences: Sequence[Decimal], *, dispersion: Decimal
) -> Decimal:
    """Combine component confidences into one honest aggregate in [0, 1].

    Summing confidences is wrong in two ways, and both matter:

    1. It saturates. Three mediocre models summing past 1.0 would report *total*
       confidence, which no combination of mediocre models earns.
    2. It ignores disagreement. Two models at 0.5 that flatly contradict each other
       are evidence of *less* certainty, not more.

    Instead: start from the single best component, add a corroboration bonus that
    approaches - but never reaches - the remaining headroom, then scale the whole
    thing down by how much the components disagree. Dispersion of 0.15 in probability
    space (a genuinely large disagreement for a binary market) roughly halves it.
    """
    contributing = [c for c in confidences if c > 0]
    if not contributing:
        return ZERO

    best = max(contributing)
    others = sum(contributing, ZERO) - best
    # 1 - exp(-others): corroboration has diminishing returns and cannot exceed the
    # headroom left by the best single component.
    corroboration = D(str(1.0 - math.exp(-float(others))))
    combined = best + (ONE - best) * corroboration

    # Disagreement penalty. Halved at dispersion ~0.15.
    agreement = D(str(1.0 / (1.0 + float(dispersion) / 0.15)))
    return quantize_prob(clamp_prob(combined * agreement))


def _explain(
    mean: Decimal,
    opinions: list[tuple[ProbabilityModel, ModelEstimate]],
    has_independent: bool,
    market_implied: Decimal | None,
    confidence: Decimal,
) -> str:
    """Plain-language account of where the number came from."""
    ranked = sorted(opinions, key=lambda pair: pair[1].confidence, reverse=True)
    drivers = "; ".join(
        f"{model.name} p={result.probability} (conf {result.confidence})"
        for model, result in ranked[:3]
    )
    parts = [f"Fair probability {mean} pooled in log-odds from {len(opinions)} component(s)."]
    if drivers:
        parts.append(f"Main drivers: {drivers}.")
    if market_implied is not None:
        parts.append(f"Market-implied mid is {market_implied}.")
    if not has_independent:
        parts.append(
            "NO INDEPENDENT PRIOR: every contributing component is derived from this "
            "market's own price, so this estimate carries no information the market "
            "does not already have and cannot support a value recommendation."
        )
    elif confidence < Decimal("0.3"):
        parts.append("Aggregate confidence is low; the interval is correspondingly wide.")
    return " ".join(parts)
