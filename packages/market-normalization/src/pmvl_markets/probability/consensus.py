"""Market-consensus priors.

Two components live here and the distinction between them is the difference between
a real signal and a circular one:

:class:`CrossPlatformConsensus`
    Uses *only other venues'* prices for the same question. Genuinely independent of
    the target market, so it can support an edge. If Polymarket prices a question at
    62c and Kalshi is offering YES at 55c, that is a real cross-venue disagreement.

:class:`ReferencePrior`
    Uses the target market's own price. Marked ``independent=False``. It exists so
    that markets with no other information source still get a *displayable* fair
    probability - but the ranking layer refuses to derive an edge from it, because
    "the market says 55c so fair value is 55c so 55c is cheap" is not an argument.
"""

from __future__ import annotations

import statistics
from decimal import Decimal

from pmvl_shared.enums import Category
from pmvl_shared.money import D, ONE, ZERO, clamp_prob, quantize_prob, safe_div
from pmvl_shared.timeutil import age_seconds, utcnow

from .base import ModelContext, ModelEstimate, ProbabilityModel, no_opinion


class CrossPlatformConsensus(ProbabilityModel):
    """Independent prior built from other venues' quotes for the same question."""

    name = "cross_platform_consensus"
    independent = True
    max_confidence = Decimal("0.72")

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        quotes = {
            platform: price
            for platform, price in ctx.cross_platform_quotes.items()
            if platform != ctx.market.platform.value and price is not None
        }
        if not quotes:
            return no_opinion("no matched market on another venue")

        prices = [float(p) for p in quotes.values()]
        mean = D(str(statistics.fmean(prices)))
        # Disagreement between venues is itself information about uncertainty.
        spread = D(str(statistics.pstdev(prices))) if len(prices) > 1 else Decimal("0.02")

        # A single corroborating venue is worth less than several agreeing ones.
        confidence = min(self.max_confidence, Decimal("0.45") + Decimal("0.12") * (len(prices) - 1))
        # Wide cross-venue disagreement means at least one venue is wrong; trust less.
        if spread > Decimal("0.08"):
            confidence *= Decimal("0.6")

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(mean)),
            confidence=quantize_prob(confidence),
            stdev=max(spread, Decimal("0.015")),
            independent=True,
            detail=(
                f"consensus of {len(prices)} other venue(s): "
                + ", ".join(f"{k}={v}" for k, v in quotes.items())
            ),
            data={"quotes": {k: str(v) for k, v in quotes.items()}},
        )


class SiblingCoherencePrior(ProbabilityModel):
    """Prior from sibling outcomes of the same multi-outcome event.

    In a mutually exclusive, exhaustive event the outcome probabilities must sum to
    1. When the siblings sum to more or less than 1, the residual is information
    about this outcome that does not come from its own quote.
    """

    name = "sibling_coherence"
    independent = True
    max_confidence = Decimal("0.45")

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        siblings = [p for _, p in ctx.sibling_outcome_prices if p is not None]
        if len(siblings) < 2:
            return no_opinion("fewer than two sibling outcomes")
        if not ctx.extra.get("mutually_exclusive_exhaustive"):
            return no_opinion("event is not known to be mutually exclusive and exhaustive")

        sibling_sum = sum(siblings, ZERO)
        residual = ONE - sibling_sum
        if residual <= 0 or residual >= ONE:
            return no_opinion(
                f"sibling outcomes sum to {sibling_sum}, leaving no coherent residual"
            )

        # Confidence falls as the sibling set drifts from summing to 1 overall,
        # since that indicates the venue's own book is internally inconsistent.
        overshoot = abs(ONE - (sibling_sum + residual))
        confidence = self.max_confidence * (ONE - min(ONE, overshoot * D(5)))

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(residual)),
            confidence=quantize_prob(max(ZERO, confidence)),
            stdev=Decimal("0.05"),
            independent=True,
            detail=f"residual after {len(siblings)} sibling outcomes summing to {sibling_sum}",
            data={"sibling_sum": str(sibling_sum)},
        )


class ReferencePrior(ProbabilityModel):
    """The target market's own mid-price. **Not independent.**

    Included so every market has a displayable reference and so the ensemble can be
    anchored when independent components are weak. Because ``independent=False``, the
    ensemble records that fact and the ranking layer will not publish a value
    recommendation whose edge rests on this component.
    """

    name = "target_market_reference"
    independent = False
    max_confidence = Decimal("0.9")

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        market = ctx.market
        mid = reference_price(ctx)
        if mid is None:
            return no_opinion("no usable quote on the target market")
        book = ctx.target_book

        now = ctx.now or utcnow()
        age = age_seconds(market.quote_observed_at, now=now)

        # A tight, deep, recently-updated book is a more reliable statement of
        # consensus than a wide one on a market nobody trades.
        confidence = Decimal("0.55")
        # A one-sided quote is real information but a weaker consensus statement than
        # a two-sided market, so it must not carry full weight.
        two_sided = (
            book is not None
            and book.best_bid("yes") is not None
            and book.best_ask("yes") is not None
        ) or (market.best_yes_bid is not None and market.best_yes_ask is not None)
        if not two_sided:
            confidence = Decimal("0.4")
        spread = market.spread
        if spread is not None:
            if spread <= Decimal("0.01"):
                confidence = Decimal("0.85")
            elif spread <= Decimal("0.03"):
                confidence = Decimal("0.7")
            elif spread > Decimal("0.10"):
                confidence = Decimal("0.35")
        if (market.volume_24h or ZERO) < D(1000):
            confidence *= Decimal("0.7")
        if age is not None and age > 900:
            confidence *= Decimal("0.6")

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(mid)),
            confidence=quantize_prob(min(self.max_confidence, confidence)),
            stdev=max(spread or Decimal("0.02"), Decimal("0.01")),
            independent=False,
            detail=f"target market mid={mid} spread={spread}",
            data_freshness_seconds=int(age) if age is not None else None,
            data={"mid": str(mid), "spread": str(spread) if spread else None},
        )


class RelatedMarketPrior(ProbabilityModel):
    """Weak prior from logically related markets on other venues.

    Related markets constrain but do not determine this one (a "wins the tournament"
    price bounds a "wins the final" price). Confidence is capped low because the
    logical relationship is not verified here - only the correlation is used.
    """

    name = "related_markets"
    independent = True
    max_confidence = Decimal("0.3")

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        related = [p for _, p in ctx.related_market_prices if p is not None]
        if not related:
            return no_opinion("no related markets identified")
        mean = D(str(statistics.fmean([float(p) for p in related])))
        spread = (
            D(str(statistics.pstdev([float(p) for p in related])))
            if len(related) > 1
            else Decimal("0.08")
        )
        confidence = min(self.max_confidence, Decimal("0.12") * len(related))
        return ModelEstimate(
            probability=quantize_prob(clamp_prob(mean)),
            confidence=quantize_prob(confidence),
            stdev=max(spread, Decimal("0.06")),
            independent=True,
            detail=f"mean of {len(related)} related market(s)",
        )


def _usable(price: Decimal | None) -> Decimal | None:
    """A price of exactly 0 or 1 from a summary field is 'no data', not a price.

    Kalshi reports ``last_price_dollars = 0.0000`` on a market that has never traded.
    Reading that as "the market thinks this is worthless" is how a deep in-the-money
    contract with a 98c resting bid came to be scored as P(YES) = 0.
    """
    if price is None or price <= 0 or price >= ONE:
        return None
    return price


def reference_price(ctx: ModelContext) -> Decimal | None:
    """The market's own implied probability of YES, from the best evidence available.

    Preference order, most to least informative:

    1. Book mid, when both sides are quoted.
    2. A **one-sided** quote. A lone 98c bid with no offer is still a strong
       statement about value; discarding it and falling through to a stale or absent
       last trade throws away the market's actual opinion.
    3. The venue's summary bid/ask, then last trade - each only if it is a real price.
    """
    book = ctx.target_book
    if book is not None:
        bid, ask = _usable(book.best_bid("yes")), _usable(book.best_ask("yes"))
        if bid is not None and ask is not None:
            return quantize_prob((bid + ask) / D(2))
        if ask is not None:
            return quantize_prob(ask)
        if bid is not None:
            return quantize_prob(bid)

    m = ctx.market
    bid, ask = _usable(m.best_yes_bid), _usable(m.best_yes_ask)
    if bid is not None and ask is not None:
        return quantize_prob((bid + ask) / D(2))
    if ask is not None:
        return quantize_prob(ask)
    if bid is not None:
        return quantize_prob(bid)
    return _usable(m.last_trade_price)


def implied_from_orderbook(ctx: ModelContext) -> Decimal | None:
    """Market-implied probability of YES, for display next to the model's estimate."""
    return reference_price(ctx)
