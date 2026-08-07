"""Category models for domains with no keyless authoritative data source.

Sports, macro, politics and general news markets have no free, reliable, machine-
readable source that this project can call without credentials. Rather than emit a
confident-sounding number from an LLM or a hand-tuned heuristic, each of these models
returns **no opinion** and states exactly which data source would make it real.

That is not a placeholder for its own sake - it is the correct behaviour. A model
that guesses would still be weighted in the ensemble, would still move the fair
probability away from the market price, and would still generate "edge" that is
pure noise. The markets these cover are not silently dropped: they still receive a
cross-platform consensus estimate where a matched market exists on the other venue,
and that estimate *is* independent and tradeable.

:class:`ExtremePriceSanityModel` is the one component here that does contribute, and
it contributes a bound rather than an estimate.
"""

from __future__ import annotations

from decimal import Decimal

from pmvl_shared.enums import Category
from pmvl_shared.money import D, ZERO, clamp_prob, quantize_prob
from pmvl_shared.timeutil import hours_until, utcnow

from ..base import ModelContext, ModelEstimate, ProbabilityModel, no_opinion


class _UnsourcedCategoryModel(ProbabilityModel):
    """Declares a category as unmodelled and names the data source it would need."""

    required_source: str = ""

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        return no_opinion(
            f"no independent model for {ctx.market.category.value}; "
            f"would require {self.required_source}"
        )


class SportsModel(_UnsourcedCategoryModel):
    """Sports contracts that are not head-to-head games.

    :class:`~.sports.SportsBaseRateModel` now prices single-game winner contracts
    from win-loss records, which are keyless. It prices nothing else, and "nothing
    else" is most of the board: on the live universe the sports category is
    dominated by golf finishing positions, NASCAR top-N, tournament futures years
    out, and player props. None of those has a record-based analogue, and each
    would need the licensed ratings and form data below.

    This model therefore still exists and still declines - it now covers a smaller
    but still large remainder.
    """

    name = "sports_unsourced"
    categories = (Category.SPORTS,)
    required_source = (
        "a licensed ratings/odds feed (e.g. Sportradar, Opta) for anything that is "
        "not a head-to-head game with a win-loss record - not keyless"
    )


class EconomicsModel(_UnsourcedCategoryModel):
    """Macro release markets (CPI, NFP, GDP, Fed decisions).

    A real model needs the release calendar plus consensus forecasts and nowcasts
    (FRED, BLS bulk, Cleveland Fed nowcast). FRED requires an API key and the BLS
    public tier is limited to a handful of queries per day, so neither supports a
    scheduled scan.
    """

    name = "economics_unsourced"
    categories = (Category.ECONOMICS,)
    required_source = "FRED API key or BLS registered access for release + nowcast data"


class PoliticsModel(_UnsourcedCategoryModel):
    """Elections and political events.

    A real model needs polling aggregates with house effects and a fundamentals
    prior. No keyless, licensed, machine-readable aggregate exists.
    """

    name = "politics_unsourced"
    categories = (Category.POLITICS, Category.GEOPOLITICS)
    required_source = "a licensed polling aggregate with house-effect adjustments"


class GenericEventModel(_UnsourcedCategoryModel):
    """Culture, tech, mentions and everything else.

    These are one-off questions with no repeatable structure to model. Their fair
    value comes from cross-venue consensus and, when enabled, the research agent.
    """

    name = "generic_unsourced"
    categories = (Category.CULTURE, Category.TECH, Category.MENTIONS, Category.OTHER)
    required_source = "per-question research (see the research provider)"


class ExtremePriceSanityModel(ProbabilityModel):
    """A bound, not an estimate, for contracts priced in the extreme tails.

    Deep-tail contracts (under 2c or over 98c) are where model error is most likely
    to be mistaken for edge: a model claiming 6% on a 1c contract implies a 500%
    return, which will dominate any ROI-based ranking. Empirically these contracts
    resolve against the buyer the overwhelming majority of the time, and the residual
    probability is dominated by settlement-rule and dispute risk rather than by the
    underlying event.

    This component supplies a *ceiling* on how far a tail estimate may stray from the
    tail, expressed as a low-confidence estimate near the quoted price. It cannot
    create an edge (its confidence is low and it sits near the market price); it
    exists to damp one.
    """

    name = "extreme_price_sanity"
    independent = False
    max_confidence = Decimal("0.4")

    #: Prices inside this band are unremarkable and the model abstains.
    lower = Decimal("0.02")
    upper = Decimal("0.98")

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        from ..consensus import reference_price

        # Must use the same usable-price logic as the reference prior. Reading
        # `best_yes_ask or last_trade_price` treated a never-traded market's 0.0000
        # last price as a real tail price and anchored the estimate at zero.
        price = reference_price(ctx)
        if price is None:
            return no_opinion("no usable reference price")
        if self.lower <= price <= self.upper:
            return no_opinion("price is not in the extreme tails")

        # Anchor slightly *toward* the tail: the residual mass in a 1c contract is
        # mostly settlement risk, which does not pay out.
        anchor = price * Decimal("0.9") if price < self.lower else (
            Decimal("1") - (Decimal("1") - price) * Decimal("0.9")
        )
        return ModelEstimate(
            probability=quantize_prob(clamp_prob(anchor)),
            confidence=self.max_confidence,
            stdev=Decimal("0.01"),
            independent=False,
            detail=f"tail contract at {price}; anchored toward the tail to damp ROI artefacts",
            data={"quoted_price": str(price)},
        )


class TimeToResolutionModel(ProbabilityModel):
    """Flags markets whose expected resolution is imminent or already passed.

    Contributes no probability. It exists to attach a data-quality signal: a market
    inside its settlement window has a book that may no longer be executable, and the
    ranking layer uses this to add a risk flag rather than to move the estimate.
    """

    name = "time_to_resolution"
    independent = True
    max_confidence = ZERO

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        hours = hours_until(
            ctx.market.expected_resolution_time, now=ctx.now or utcnow()
        )
        if hours is None:
            return no_opinion("no expected resolution time")
        return ModelEstimate(
            probability=None,
            confidence=ZERO,
            detail=f"{hours:.1f}h to expected resolution",
            data={"hours_to_resolution": f"{hours:.2f}", "imminent": hours < 1},
        )


def default_category_models() -> list[ProbabilityModel]:
    return [
        SportsModel(),
        EconomicsModel(),
        PoliticsModel(),
        GenericEventModel(),
    ]
