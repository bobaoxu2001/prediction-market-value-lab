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
    1. When the complete set sums to something else, the correction belongs to every
    outcome proportionally, so each is renormalised by the total. The information is
    real but weak, and it is not independent of this market's own quote in the way a
    cross-venue price is - it is the venue's own book being internally inconsistent.
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

        # The residual only belongs to THIS outcome when every other outcome is
        # priced. On a partially-quoted set the leftover mass is shared among all the
        # unpriced ones, and attributing it here is simply wrong: a Seoul temperature
        # event with two quoted buckets out of many produced a residual of 0.895 for a
        # bucket the market priced at 0.001, and it took 26% of the ensemble weight.
        expected = int(ctx.extra.get("event_outcome_count") or 0)
        if expected <= 0:
            return no_opinion("event outcome count unknown; cannot verify completeness")
        if len(siblings) + 1 < expected:
            return no_opinion(
                f"only {len(siblings) + 1} of {expected} outcomes priced; the residual "
                "is shared among the unpriced ones and cannot be attributed here"
            )

        own_price = ctx.extra.get("own_outcome_price")
        if own_price is None:
            return no_opinion("this outcome's own price is unavailable for normalisation")

        sibling_sum = sum(siblings, ZERO)
        total = sibling_sum + D(str(own_price))
        if total <= ZERO:
            return no_opinion("outcome set sums to zero")

        # NORMALISE, do not residualise.
        #
        # On a complete exhaustive set the prices should sum to 1. When they sum to
        # S != 1 the discrepancy belongs to the whole set, so each outcome is scaled
        # by 1/S. The previous residual form (1 - sum(others)) handed the entire
        # shortfall to whichever outcome was being scored: a Seoul temperature board
        # summing to 0.63 turned a bucket the market priced at 0.07 into 0.44, and
        # would have done the same to every other bucket in turn.
        coherent = safe_div(D(str(own_price)), total)

        deviation = abs(ONE - total)
        if deviation < Decimal("0.02"):
            return no_opinion(
                f"outcome set sums to {total}, already coherent; nothing to add"
            )

        # A set that is wildly off does not reflect a tradeable view - it reflects
        # missing quotes - so trust falls as the deviation grows.
        confidence = self.max_confidence * (ONE - min(ONE, deviation * D(2)))
        if confidence <= ZERO:
            return no_opinion(f"outcome set sums to {total}; too incoherent to use")

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(coherent)),
            confidence=quantize_prob(confidence),
            stdev=max(deviation / D(2), Decimal("0.02")),
            independent=True,
            detail=(
                f"normalised across {len(siblings) + 1} outcomes summing to {total}"
            ),
            data={"outcome_sum": str(total), "own_price": str(own_price)},
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
