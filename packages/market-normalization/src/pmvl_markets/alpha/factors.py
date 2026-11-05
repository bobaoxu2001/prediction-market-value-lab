"""The initial alpha factor set.

What is here is what the repository can currently compute from data it actually
has. Factors whose inputs do not exist yet are **not** stubbed with a plausible
number - they are absent, or they register and refuse. A factor that returns a
default when it has no data is worse than a missing factor, because a strategy
fitted on it has learned the shape of the missing data and nothing says so.

Sections map onto the factor families in :mod:`.registry`:

1. source divergence and source-update lag
2. threshold distance
3. forecast dispersion
4. order-book microstructure
5. maker/taker economics
6. time to resolution
7. price bucket
8. cross-venue divergence
9. sibling coherence
10. catalyst timing
11. liquidity / capacity

Everything is ``Decimal``. Binary contracts live on a cent lattice where float
error changes verdicts, and a factor that crosses a threshold because of binary
representation is a research result nobody can reproduce.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from pmvl_shared.enums import Side
from pmvl_shared.money import ONE, ZERO, D, quantize_prob, quantize_usd, safe_div
from pmvl_shared.timeutil import ensure_utc

from ..pricing.fees import fee_per_contract, kalshi_maker_fee, taker_fee
from ..pricing.orderbook import depth_usd, executable_quote
from .registry import (
    REGISTRY,
    FactorContext,
    FactorFamily,
    FeatureValue,
    Unavailable,
)

# ---------------------------------------------------------------------------
# Shared vocabulary
# ---------------------------------------------------------------------------

#: Observation kinds the factors below read. Named constants rather than string
#: literals so a typo is an ImportError instead of a factor that never fires.
KIND_SOURCE_ESTIMATE = "source_estimate"
KIND_SOURCE_POINT_VALUE = "source_point_value"
KIND_MARKET_PROBABILITY = "market_probability"

#: Price buckets, in dollars. Configurable by passing an override through
#: ``FactorContext.extra["price_buckets"]``; the default is the 10-cent ladder
#: the research plan asks for.
DEFAULT_PRICE_BUCKETS: tuple[Decimal, ...] = tuple(
    Decimal(str(edge)) / Decimal("100") for edge in range(10, 100, 10)
)

#: Time-to-resolution buckets, in hours.
#:
#: A 30-cent contract with 20 minutes left and a 30-cent contract with 30 days
#: left are not the same economic object, and pooling them is how a strategy
#: reports an average over two different businesses.
DEFAULT_TIME_BUCKET_HOURS: tuple[int, ...] = (1, 6, 24, 72, 168, 720)

#: How stale a source observation may be before a divergence factor refuses.
#:
#: Not a tuning knob discovered by search. The thesis under test is that the
#: market has not yet absorbed a *recent* source change; a day-old forecast that
#: still disagrees with the price is evidence the market knows something the
#: model does not, which is the opposite trade.
DEFAULT_MAX_SOURCE_AGE = timedelta(hours=6)


def _side_for(ctx: FactorContext) -> Side:
    """Which side of the book a divergence implies buying.

    Model above market means buy YES; model below means buy NO. Stated once here
    so every economics factor agrees about which ladder it is pricing, rather
    than each deciding for itself and one of them being wrong.
    """
    fair = ctx.model_fair_probability
    market = ctx.executable_probability
    if fair is None or market is None:
        return Side.YES
    return Side.YES if fair >= market else Side.NO


def _oldest_input(*times: datetime | None) -> datetime | None:
    present = [ensure_utc(t) for t in times if t is not None]
    return min(present) if present else None


# ---------------------------------------------------------------------------
# 1. Source divergence
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "source_divergence",
    family=FactorFamily.SOURCE_DIVERGENCE,
    version="v0",
    description=(
        "Independent estimate minus executable market probability. Signed: "
        "positive means the model is above the market and the implied trade is "
        "YES."
    ),
    requires=("model_fair_probability", "executable_probability"),
    needs_outcome_model=True,
)
def source_divergence(ctx: FactorContext) -> FeatureValue:
    """``fair - executable``, with sign and magnitude both preserved.

    Against the **executable** probability, never the midpoint. A midpoint gap is
    an arithmetic fact about a screen; an executable gap is the only one a trade
    could realise, and on a wide book the two differ by more than any edge this
    system has ever measured.
    """
    fair, market = ctx.model_fair_probability, ctx.executable_probability
    if fair is None:
        return FeatureValue.missing("source_divergence", Unavailable.NO_SOURCE_OBSERVATION)
    if market is None:
        return FeatureValue.missing("source_divergence", Unavailable.NO_QUOTE)
    gap = quantize_prob(fair - market)
    return FeatureValue.of(
        "source_divergence",
        gap,
        components={
            "fair_probability": quantize_prob(fair),
            "executable_probability": quantize_prob(market),
            "magnitude": abs(gap),
        },
        labels={"direction": "yes" if gap >= 0 else "no"},
    )


@REGISTRY.factor(
    "source_divergence_conservative",
    family=FactorFamily.SOURCE_DIVERGENCE,
    version="v0",
    description=(
        "The same gap measured from the conservative bound of the model's "
        "interval rather than its mean."
    ),
    requires=("model_fair_probability_low", "model_fair_probability_high"),
    needs_outcome_model=True,
)
def source_divergence_conservative(ctx: FactorContext) -> FeatureValue:
    """The gap a wide interval cannot manufacture.

    For a YES trade the conservative bound is ``low``. For NO it is
    ``1 - high``: using ``1 - low`` would be the *optimistic* bound and would
    systematically overstate NO-side edge, which is the same mistake the ranking
    layer already refuses to make.
    """
    market = ctx.executable_probability
    if market is None:
        return FeatureValue.missing(
            "source_divergence_conservative", Unavailable.NO_QUOTE
        )
    low, high = ctx.model_fair_probability_low, ctx.model_fair_probability_high
    if low is None or high is None:
        return FeatureValue.missing(
            "source_divergence_conservative", Unavailable.NO_SOURCE_OBSERVATION
        )
    side = _side_for(ctx)
    if side is Side.YES:
        bound, gap = low, low - market
    else:
        bound, gap = ONE - high, (ONE - high) - (ONE - market)
    return FeatureValue.of(
        "source_divergence_conservative",
        quantize_prob(gap),
        components={
            "conservative_bound": quantize_prob(bound),
            "interval_width": quantize_prob(high - low),
        },
        labels={"side": side.value},
    )


@REGISTRY.factor(
    "source_update_lag_seconds",
    family=FactorFamily.SOURCE_LAG,
    version="v0",
    description=(
        "Seconds since the authoritative source last changed its value, "
        "measured from when this system could first have used the new one."
    ),
    requires=(KIND_SOURCE_ESTIMATE,),
)
def source_update_lag_seconds(ctx: FactorContext) -> FeatureValue:
    """How long ago the source moved - the clock the whole thesis runs on.

    Measured from ``available_at``, not ``observed_at``. The strategy's claim is
    about a window in which the market has not yet absorbed information *we*
    could have acted on; a forecast published an hour before we could read it
    was not tradeable for that hour.
    """
    latest = ctx.view.latest(KIND_SOURCE_ESTIMATE)
    if latest is None:
        return FeatureValue.missing(
            "source_update_lag_seconds", Unavailable.NO_SOURCE_OBSERVATION
        )
    age = latest.age_at(ctx.decision_time)
    if age is None:
        return FeatureValue.missing(
            "source_update_lag_seconds", Unavailable.NO_SOURCE_OBSERVATION
        )
    ingestion = latest.ingestion_lag()
    components = {"age_seconds": D(str(int(age.total_seconds())))}
    if ingestion is not None:
        components["ingestion_lag_seconds"] = D(str(int(ingestion.total_seconds())))
    return FeatureValue.of(
        "source_update_lag_seconds",
        D(str(int(age.total_seconds()))),
        components=components,
        labels={"source_id": latest.source_id},
        inputs_available_at=ensure_utc(latest.available_at),
    )


@REGISTRY.factor(
    "source_change_magnitude",
    family=FactorFamily.SOURCE_LAG,
    version="v0",
    description=(
        "How far the source's own estimate moved on its most recent update, in "
        "probability points."
    ),
    requires=(KIND_SOURCE_ESTIMATE,),
)
def source_change_magnitude(ctx: FactorContext) -> FeatureValue:
    """The size of the news, from the source's side.

    Needs two admissible observations. Reaching for "the value before the
    update" by any other route is how a factor ends up comparing today's
    forecast against today's forecast and reporting that nothing happened.
    """
    latest = ctx.view.latest(KIND_SOURCE_ESTIMATE)
    previous = ctx.view.previous(KIND_SOURCE_ESTIMATE)
    if latest is None or previous is None:
        return FeatureValue.missing(
            "source_change_magnitude", Unavailable.INSUFFICIENT_SOURCES
        )
    new, old = _as_probability(latest.value), _as_probability(previous.value)
    if new is None or old is None:
        return FeatureValue.missing(
            "source_change_magnitude", Unavailable.MALFORMED_INPUT
        )
    change = quantize_prob(new - old)
    return FeatureValue.of(
        "source_change_magnitude",
        change,
        components={
            "before": quantize_prob(old),
            "after": quantize_prob(new),
            "magnitude": abs(change),
        },
        labels={"source_id": latest.source_id},
        inputs_available_at=_oldest_input(previous.available_at, latest.available_at),
    )


@REGISTRY.factor(
    "market_reaction_to_source",
    family=FactorFamily.SOURCE_LAG,
    version="v0",
    description=(
        "How far the market moved since the source's last update, and what "
        "fraction of the source's move that represents."
    ),
    requires=(KIND_SOURCE_ESTIMATE, KIND_MARKET_PROBABILITY),
)
def market_reaction_to_source(ctx: FactorContext) -> FeatureValue:
    """The quantity the source-lag thesis is actually about.

    A source that moved 12 points while the market moved 11 has been absorbed;
    the residual is noise plus fees. A source that moved 12 while the market
    moved 1 is the case worth studying. ``absorbed_fraction`` is that ratio, and
    the strategy layer keeps it as an attributable component rather than folding
    it into a score.
    """
    latest_source = ctx.view.latest(KIND_SOURCE_ESTIMATE)
    if latest_source is None:
        return FeatureValue.missing(
            "market_reaction_to_source", Unavailable.NO_SOURCE_OBSERVATION
        )
    update_time = ensure_utc(latest_source.available_at)
    prints = ctx.view.of_kind(KIND_MARKET_PROBABILITY)
    before = [p for p in prints if ensure_utc(p.available_at) <= update_time]
    after = [p for p in prints if ensure_utc(p.available_at) > update_time]
    if not before or not after:
        # No print on one side of the update means there is nothing to compare.
        # Assuming the pre-update price equals the current one would fabricate
        # exactly the "market has not reacted" reading the strategy is looking
        # for, which is the most expensive possible place to guess.
        return FeatureValue.missing(
            "market_reaction_to_source", Unavailable.INSUFFICIENT_SOURCES
        )
    price_before = _as_probability(before[-1].value)
    price_after = _as_probability(after[-1].value)
    if price_before is None or price_after is None:
        return FeatureValue.missing(
            "market_reaction_to_source", Unavailable.MALFORMED_INPUT
        )
    move = quantize_prob(price_after - price_before)

    components = {
        "price_before": quantize_prob(price_before),
        "price_after": quantize_prob(price_after),
        "market_move": move,
    }
    source_change = source_change_magnitude(ctx)
    if source_change.available and source_change.value != ZERO:
        components["source_change"] = source_change.value
        components["absorbed_fraction"] = quantize_prob(
            safe_div(move, source_change.value)
        )
    return FeatureValue.of(
        "market_reaction_to_source",
        move,
        components=components,
        labels={"source_id": latest_source.source_id},
        inputs_available_at=_oldest_input(before[-1].available_at, update_time),
    )


@REGISTRY.factor(
    "source_freshness_ok",
    family=FactorFamily.SOURCE_LAG,
    version="v0",
    description=(
        "1 when the newest source observation is inside the freshness window, "
        "0 when it is stale."
    ),
    requires=(KIND_SOURCE_ESTIMATE,),
)
def source_freshness_ok(ctx: FactorContext) -> FeatureValue:
    """A gate, expressed as a factor so a research run can segment on it.

    Stale-but-still-divergent is the case the strategy must *not* treat as a
    signal: if the market has had a day to absorb a forecast and still disagrees,
    the reasonable reading is that the market knows something the model does not.
    """
    window = ctx.extra.get("max_source_age", DEFAULT_MAX_SOURCE_AGE)
    latest = ctx.view.latest(KIND_SOURCE_ESTIMATE)
    if latest is None:
        return FeatureValue.missing(
            "source_freshness_ok", Unavailable.NO_SOURCE_OBSERVATION
        )
    age = latest.age_at(ctx.decision_time)
    if age is None:
        return FeatureValue.missing(
            "source_freshness_ok", Unavailable.NO_SOURCE_OBSERVATION
        )
    fresh = age <= window
    return FeatureValue.of(
        "source_freshness_ok",
        ONE if fresh else ZERO,
        components={
            "age_seconds": D(str(int(age.total_seconds()))),
            "window_seconds": D(str(int(window.total_seconds()))),
        },
        labels={"verdict": "fresh" if fresh else Unavailable.STALE_SOURCE.value},
        inputs_available_at=ensure_utc(latest.available_at),
    )


# ---------------------------------------------------------------------------
# 2. Threshold distance
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "threshold_distance",
    family=FactorFamily.THRESHOLD_DISTANCE,
    version="v0",
    description=(
        "Estimated settlement value minus the contract threshold, in the "
        "contract's own units."
    ),
    requires=(KIND_SOURCE_POINT_VALUE, "threshold_value"),
)
def threshold_distance(ctx: FactorContext) -> FeatureValue:
    """Raw distance, unnormalised, in the units the contract settles in.

    Deliberately not divided by anything. The normalised version is a separate
    factor that refuses without a *measured* error scale, so a caller that wants
    comparability across markets has to obtain the thing that makes it valid
    rather than getting an approximation for free.
    """
    if ctx.threshold_value is None:
        return FeatureValue.missing("threshold_distance", Unavailable.NO_THRESHOLD)
    latest = ctx.view.latest(KIND_SOURCE_POINT_VALUE)
    if latest is None:
        return FeatureValue.missing(
            "threshold_distance", Unavailable.NO_SOURCE_OBSERVATION
        )
    estimate = _as_decimal(latest.value)
    if estimate is None:
        return FeatureValue.missing("threshold_distance", Unavailable.MALFORMED_INPUT)
    distance = estimate - ctx.threshold_value
    return FeatureValue.of(
        "threshold_distance",
        distance,
        components={"estimate": estimate, "threshold": ctx.threshold_value},
        labels={
            "comparator": ctx.threshold_comparator or "unknown",
            "source_id": latest.source_id,
        },
        inputs_available_at=ensure_utc(latest.available_at),
    )


@REGISTRY.factor(
    "threshold_distance_z",
    family=FactorFamily.THRESHOLD_DISTANCE,
    version="v0",
    description=(
        "Threshold distance divided by a MEASURED forecast-error scale for this "
        "source at this lead time. Refuses when no measured scale exists."
    ),
    requires=(KIND_SOURCE_POINT_VALUE, "threshold_value", "error_scale"),
)
def threshold_distance_z(ctx: FactorContext) -> FeatureValue:
    """Normalised distance, or nothing.

    The refusal is the point. Dividing by an assumed scale produces a number
    that looks comparable across markets and is not, and a z of 2.0 carries an
    implied tail probability that a reader will use even when the docstring says
    not to. The scale must come from the source's own measured error history -
    the same standard the CPI model already holds itself to, where sigma is the
    root-mean-square of the Cleveland Fed nowcast's own 158 months of misses
    rather than a number somebody picked.

    ``normality_validated`` is carried through as a label rather than assumed.
    Even a measured sigma does not license reading a z as a Gaussian tail
    probability unless the residual distribution was actually checked.
    """
    raw = threshold_distance(ctx)
    if not raw.available or raw.value is None:
        return FeatureValue.missing(
            "threshold_distance_z", raw.reason or Unavailable.NO_THRESHOLD
        )
    scale = ctx.extra.get("error_scale")
    scale_value = _as_decimal(scale) if scale is not None else None
    if scale_value is None or scale_value <= ZERO:
        return FeatureValue.missing(
            "threshold_distance_z",
            Unavailable.NO_ERROR_SCALE,
            labels={
                "note": (
                    "no measured error scale for this source and lead time; a "
                    "z computed from an assumed scale is not comparable"
                )
            },
        )
    z = safe_div(raw.value, scale_value)
    validated = bool(ctx.extra.get("error_scale_normality_validated", False))
    return FeatureValue.of(
        "threshold_distance_z",
        z,
        components={**dict(raw.components), "error_scale": scale_value},
        labels={
            **dict(raw.labels),
            "normality_validated": "true" if validated else "false",
        },
        inputs_available_at=raw.inputs_available_at,
    )


# ---------------------------------------------------------------------------
# 3. Forecast dispersion
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "forecast_dispersion",
    family=FactorFamily.FORECAST_DISPERSION,
    version="v0",
    description=(
        "Spread across genuinely independent forecasts: range, robust "
        "dispersion, agreement count and directional consensus."
    ),
    requires=(KIND_SOURCE_ESTIMATE,),
)
def forecast_dispersion(ctx: FactorContext) -> FeatureValue:
    """Dispersion across sources, counting mirrors once.

    Needs at least two *independent* sources. A vendor that republishes NWS is
    not a second opinion about the weather; counting it as one would report
    agreement that does not exist, and agreement is precisely what this factor
    would be used as evidence of.

    Dispersion is the mean absolute deviation from the median, not the standard
    deviation. With two or three sources a standard deviation is dominated by
    whichever one is furthest out, and the median-based measure says something
    about the group rather than about its worst member.
    """
    mirrors = ctx.extra.get("source_mirrors") or {}
    observations = ctx.view.of_kind(KIND_SOURCE_ESTIMATE)
    newest: dict[str, Decimal] = {}
    for obs in observations:
        upstream = mirrors.get(obs.source_id, obs.source_id)
        value = _as_probability(obs.value)
        if value is not None:
            newest[upstream] = value  # ordered oldest-first, so this keeps newest
    if len(newest) < 2:
        return FeatureValue.missing(
            "forecast_dispersion",
            Unavailable.INSUFFICIENT_SOURCES,
            labels={"independent_sources": str(len(newest))},
        )
    values = sorted(newest.values())
    median = _median(values)
    spread = values[-1] - values[0]
    deviation = safe_div(
        sum((abs(v - median) for v in values), ZERO), D(str(len(values)))
    )
    market = ctx.executable_probability
    above = sum(1 for v in values if market is not None and v > market)
    below = sum(1 for v in values if market is not None and v < market)
    consensus = (
        ZERO
        if market is None or len(values) == 0
        else safe_div(D(str(above - below)), D(str(len(values))))
    )
    return FeatureValue.of(
        "forecast_dispersion",
        quantize_prob(deviation),
        components={
            "range": quantize_prob(spread),
            "median": quantize_prob(median),
            "independent_sources": D(str(len(values))),
            "directional_consensus": quantize_prob(consensus),
        },
        labels={"sources": ",".join(sorted(newest))},
    )


# ---------------------------------------------------------------------------
# 4. Order-book microstructure
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "book_spread",
    family=FactorFamily.MICROSTRUCTURE,
    version="v0",
    description="Ask minus bid on the traded side, in dollars.",
    requires=("order_book",),
    needs_book=True,
)
def book_spread(ctx: FactorContext) -> FeatureValue:
    if ctx.book is None:
        return FeatureValue.missing("book_spread", Unavailable.NO_ORDER_BOOK)
    side = _side_for(ctx)
    ask, bid = ctx.book.best_ask(side), ctx.book.best_bid(side)
    if ask is None or bid is None:
        return FeatureValue.missing("book_spread", Unavailable.NO_QUOTE)
    return FeatureValue.of(
        "book_spread",
        quantize_usd(ask - bid),
        components={"best_bid": bid, "best_ask": ask},
        labels={"side": side.value},
        inputs_available_at=ensure_utc(ctx.book.observed_at),
    )


@REGISTRY.factor(
    "book_imbalance",
    family=FactorFamily.MICROSTRUCTURE,
    version="v0",
    description=(
        "(bid depth - ask depth) / (bid depth + ask depth) over the top levels, "
        "in dollars."
    ),
    requires=("order_book",),
    needs_book=True,
)
def book_imbalance(ctx: FactorContext) -> FeatureValue:
    """Resting size on one side against the other.

    In dollars rather than contracts, because a contract at 5 cents and one at
    95 cents commit twenty times different capital and an imbalance measured in
    counts would call those the same book.
    """
    if ctx.book is None:
        return FeatureValue.missing("book_imbalance", Unavailable.NO_ORDER_BOOK)
    side = _side_for(ctx)
    levels = int(ctx.extra.get("imbalance_levels", 3))
    bids = ctx.book.bids(side)[:levels]
    asks = ctx.book.asks(side)[:levels]
    bid_usd = sum((level.price * level.size for level in bids), ZERO)
    ask_usd = sum((level.price * level.size for level in asks), ZERO)
    total = bid_usd + ask_usd
    if total <= ZERO:
        return FeatureValue.missing("book_imbalance", Unavailable.NO_ORDER_BOOK)
    return FeatureValue.of(
        "book_imbalance",
        quantize_prob(safe_div(bid_usd - ask_usd, total)),
        components={"bid_depth_usd": quantize_usd(bid_usd), "ask_depth_usd": quantize_usd(ask_usd)},
        labels={"side": side.value, "levels": str(levels)},
        inputs_available_at=ensure_utc(ctx.book.observed_at),
    )


@REGISTRY.factor(
    "book_slope",
    family=FactorFamily.MICROSTRUCTURE,
    version="v0",
    description=(
        "Cost of size: VWAP at the research size minus top-of-book ask, per "
        "contract."
    ),
    requires=("order_book",),
    needs_book=True,
)
def book_slope(ctx: FactorContext) -> FeatureValue:
    """How much worse the price gets when you actually want size.

    This is the number that distinguishes a book with a one-lot at the top from
    a book that can absorb a position, and it is invisible in any top-of-book
    quote. A partial fill is reported as such rather than being extrapolated -
    the shortfall is itself the finding.
    """
    if ctx.book is None:
        return FeatureValue.missing("book_slope", Unavailable.NO_ORDER_BOOK)
    side = _side_for(ctx)
    size = _as_decimal(ctx.extra.get("research_size", Decimal("100"))) or Decimal("100")
    top = ctx.book.best_ask(side)
    quote = executable_quote(ctx.book, side, size)
    if top is None or quote is None:
        return FeatureValue.missing("book_slope", Unavailable.NO_QUOTE)
    return FeatureValue.of(
        "book_slope",
        quantize_usd(quote.average_price - top),
        components={
            "top_of_book": top,
            "vwap_at_size": quote.average_price,
            "requested_size": size,
            "filled_size": quote.filled_size,
        },
        labels={
            "side": side.value,
            "fully_filled": "true" if quote.fully_filled else "false",
        },
        inputs_available_at=ensure_utc(ctx.book.observed_at),
    )


# ---------------------------------------------------------------------------
# 5. Maker/taker economics
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "taker_edge",
    family=FactorFamily.MAKER_TAKER_ECONOMICS,
    version="v0",
    description=(
        "Conservative fair probability minus all-in taker cost, per contract."
    ),
    requires=("order_book", "model_fair_probability_low"),
    needs_book=True,
    needs_outcome_model=True,
)
def taker_edge(ctx: FactorContext) -> FeatureValue:
    """What lifting the offer is worth, after the venue's real fee at that size.

    Kalshi ceils its fee to the whole cent on the whole order, so the
    per-contract fee is size-dependent and a one-lot pays several times the rate
    a hundred-lot pays. Quoting the large-order rate on a small position would
    understate the fee burden on exactly the trades a research run is most likely
    to simulate.
    """
    if ctx.book is None:
        return FeatureValue.missing("taker_edge", Unavailable.NO_ORDER_BOOK)
    side = _side_for(ctx)
    conservative = _conservative_probability(ctx, side)
    if conservative is None:
        return FeatureValue.missing("taker_edge", Unavailable.NO_SOURCE_OBSERVATION)
    size = _as_decimal(ctx.extra.get("research_size", Decimal("100"))) or Decimal("100")
    quote = executable_quote(ctx.book, side, size)
    if quote is None or quote.filled_size <= ZERO:
        return FeatureValue.missing("taker_edge", Unavailable.NO_QUOTE)
    fee = fee_per_contract(
        ctx.market.platform,
        quote.filled_size,
        quote.average_price,
        rate=ctx.market.fee_rate,
        fee_type=ctx.market.fee_type,
    )
    edge = conservative - quote.average_price - fee
    return FeatureValue.of(
        "taker_edge",
        quantize_usd(edge),
        components={
            "conservative_probability": quantize_prob(conservative),
            "entry_vwap": quote.average_price,
            "fee_per_contract": quantize_usd(fee),
            "filled_size": quote.filled_size,
        },
        labels={"side": side.value, "liquidity": "taker"},
        inputs_available_at=ensure_utc(ctx.book.observed_at),
    )


@REGISTRY.factor(
    "maker_proposed_edge",
    family=FactorFamily.MAKER_TAKER_ECONOMICS,
    version="v0",
    description=(
        "Edge on a maker order posted one tick inside the touch, BEFORE any "
        "fill probability is applied."
    ),
    requires=("order_book", "model_fair_probability_low"),
    needs_book=True,
    needs_outcome_model=True,
)
def maker_proposed_edge(ctx: FactorContext) -> FeatureValue:
    """What resting an order *would* be worth if it filled.

    "Proposed", and the word is load-bearing. This is an edge conditional on a
    fill that has not been shown to happen. Multiplying it by a fill probability
    is the execution model's job, and separating the two is what stops a research
    surface reporting a maker edge that assumes certainty of execution.

    Kalshi charges maker fees only on series whose ``fee_type`` says so; on
    Polymarket makers are never charged. Both come from the venues' published
    schedules and neither is assumed here.
    """
    if ctx.book is None:
        return FeatureValue.missing("maker_proposed_edge", Unavailable.NO_ORDER_BOOK)
    side = _side_for(ctx)
    conservative = _conservative_probability(ctx, side)
    if conservative is None:
        return FeatureValue.missing(
            "maker_proposed_edge", Unavailable.NO_SOURCE_OBSERVATION
        )
    bid, ask = ctx.book.best_bid(side), ctx.book.best_ask(side)
    if bid is None or ask is None:
        return FeatureValue.missing("maker_proposed_edge", Unavailable.NO_QUOTE)
    tick = ctx.market.tick_size if ctx.market.tick_size > ZERO else Decimal("0.01")
    # One tick better than the current best bid, but never through the offer:
    # a "maker" order priced at or above the ask is a taker order wearing the
    # wrong label, and it would be filled instantly at a worse price than this
    # factor claims.
    proposed = min(bid + tick, ask - tick) if ask - tick > bid else bid
    size = _as_decimal(ctx.extra.get("research_size", Decimal("100"))) or Decimal("100")
    fee = _maker_fee_per_contract(ctx, size, proposed)
    edge = conservative - proposed - fee
    taker = taker_edge(ctx)
    components = {
        "conservative_probability": quantize_prob(conservative),
        "proposed_price": proposed,
        "best_bid": bid,
        "best_ask": ask,
        "maker_fee_per_contract": quantize_usd(fee),
        "spread_captured": quantize_usd(ask - proposed),
    }
    if taker.available and taker.value is not None:
        components["edge_over_taker"] = quantize_usd(edge - taker.value)
    return FeatureValue.of(
        "maker_proposed_edge",
        quantize_usd(edge),
        components=components,
        labels={"side": side.value, "liquidity": "maker", "fill_applied": "false"},
        inputs_available_at=ensure_utc(ctx.book.observed_at),
    )


@REGISTRY.factor(
    "maker_taker_fee_difference",
    family=FactorFamily.MAKER_TAKER_ECONOMICS,
    version="v0",
    description="Per-contract taker fee minus per-contract maker fee at the research size.",
    requires=("order_book",),
    needs_book=True,
)
def maker_taker_fee_difference(ctx: FactorContext) -> FeatureValue:
    """What the venue charges for immediacy, in isolation.

    Separated from the price improvement so an attribution report can tell a
    strategy that earns its edge from the spread apart from one that earns it
    from the fee schedule. Those have different capacities and different
    futures.
    """
    if ctx.book is None:
        return FeatureValue.missing(
            "maker_taker_fee_difference", Unavailable.NO_ORDER_BOOK
        )
    side = _side_for(ctx)
    price = ctx.book.best_ask(side)
    if price is None:
        return FeatureValue.missing("maker_taker_fee_difference", Unavailable.NO_QUOTE)
    size = _as_decimal(ctx.extra.get("research_size", Decimal("100"))) or Decimal("100")
    taker_total = taker_fee(
        ctx.market.platform,
        size,
        price,
        rate=ctx.market.fee_rate,
        fee_type=ctx.market.fee_type,
    )
    taker_each = safe_div(taker_total, size)
    maker_each = _maker_fee_per_contract(ctx, size, price)
    return FeatureValue.of(
        "maker_taker_fee_difference",
        quantize_usd(taker_each - maker_each),
        components={
            "taker_fee_per_contract": quantize_usd(taker_each),
            "maker_fee_per_contract": quantize_usd(maker_each),
        },
        labels={"side": side.value, "fee_type": ctx.market.fee_type or "default"},
        inputs_available_at=ensure_utc(ctx.book.observed_at),
    )


# ---------------------------------------------------------------------------
# 6. Time to resolution
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "hours_to_resolution",
    family=FactorFamily.TIME_TO_RESOLUTION,
    version="v0",
    description="Hours from the decision to expected resolution, continuous.",
    requires=("expected_resolution_time",),
)
def hours_to_resolution(ctx: FactorContext) -> FeatureValue:
    resolution = ensure_utc(ctx.market.expected_resolution_time)
    if resolution is None:
        return FeatureValue.missing("hours_to_resolution", Unavailable.NOT_APPLICABLE)
    hours = (resolution - ctx.decision_time).total_seconds() / 3600.0
    return FeatureValue.of(
        "hours_to_resolution",
        D(f"{hours:.4f}"),
        labels={"bucket": _time_bucket(hours, ctx)},
    )


# ---------------------------------------------------------------------------
# 7. Price bucket
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "price_bucket",
    family=FactorFamily.PRICE_BUCKET,
    version="v0",
    description=(
        "Which price band the executable market probability falls in. Bucket "
        "edges are configurable."
    ),
    requires=("executable_probability",),
)
def price_bucket(ctx: FactorContext) -> FeatureValue:
    """The band, as the lower edge, with a readable label.

    Calibration is not uniform across the price range - cheap contracts are
    systematically different objects from even-money ones - so almost every
    research question about this system wants to be asked per band.
    """
    market = ctx.executable_probability
    if market is None:
        return FeatureValue.missing("price_bucket", Unavailable.NO_QUOTE)
    edges: Sequence[Decimal] = ctx.extra.get("price_buckets", DEFAULT_PRICE_BUCKETS)
    lower = ZERO
    for edge in edges:
        if market < edge:
            break
        lower = edge
    upper = next((e for e in edges if e > lower), ONE)
    return FeatureValue.of(
        "price_bucket",
        lower,
        components={"lower": lower, "upper": upper},
        labels={"bucket": f"{int(lower * 100)}-{int(upper * 100)}c"},
    )


# ---------------------------------------------------------------------------
# 8. Cross-venue divergence
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "cross_venue_divergence",
    family=FactorFamily.CROSS_VENUE,
    version="v0",
    description=(
        "Executable probability here minus the executable probability of a "
        "VERIFIED-equivalent contract elsewhere."
    ),
    requires=("cross_venue_probabilities", "cross_venue_verified"),
)
def cross_venue_divergence(ctx: FactorContext) -> FeatureValue:
    """Only for pairs that passed strict equivalence.

    The gate is not caution for its own sake. Two contracts that read alike and
    settle differently produce a divergence that is a *rule* difference, and a
    strategy that trades it is not arbitraging a mispricing - it is taking the
    short side of a settlement definition it has not read. The matching layer
    already distinguishes verified equivalence from superficial similarity;
    this factor refuses everything below the strict verdict.
    """
    if not ctx.cross_venue_verified:
        return FeatureValue.missing(
            "cross_venue_divergence", Unavailable.NOT_VERIFIED_EQUIVALENT
        )
    if not ctx.cross_venue_probabilities:
        return FeatureValue.missing(
            "cross_venue_divergence", Unavailable.NO_SOURCE_OBSERVATION
        )
    here = ctx.executable_probability
    if here is None:
        return FeatureValue.missing("cross_venue_divergence", Unavailable.NO_QUOTE)
    platform, there = ctx.cross_venue_probabilities[0]
    gap = quantize_prob(here - there)
    return FeatureValue.of(
        "cross_venue_divergence",
        gap,
        components={"here": quantize_prob(here), "there": quantize_prob(there)},
        labels={"counterparty_platform": platform, "magnitude_direction": "here_high" if gap > 0 else "here_low"},
    )


# ---------------------------------------------------------------------------
# 9. Sibling coherence
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "sibling_coherence_error",
    family=FactorFamily.SIBLING_COHERENCE,
    version="v0",
    description=(
        "How far a mutually exclusive, exhaustive sibling set departs from "
        "summing to one."
    ),
    requires=("sibling_probabilities",),
)
def sibling_coherence_error(ctx: FactorContext) -> FeatureValue:
    """The arithmetic the venue's own board has to satisfy.

    On an exhaustive set of mutually exclusive outcomes the probabilities sum to
    one, and a sum away from one is an internal inconsistency in prices that
    needs no forecast to detect. Signed: positive means the board is
    over-priced in aggregate (the set costs more than a dollar), which is the
    direction a seller cares about.

    Only meaningful when the event really is exhaustive and mutually exclusive.
    ``extra["sibling_set_exhaustive"]`` must say so; assuming it from the shape
    of the data would turn a partial board into a fake incoherence.
    """
    if not ctx.extra.get("sibling_set_exhaustive"):
        return FeatureValue.missing(
            "sibling_coherence_error",
            Unavailable.NOT_APPLICABLE,
            labels={"note": "event not declared mutually exclusive and exhaustive"},
        )
    if len(ctx.sibling_probabilities) < 2:
        return FeatureValue.missing(
            "sibling_coherence_error", Unavailable.INSUFFICIENT_SOURCES
        )
    total = sum((p for _, p in ctx.sibling_probabilities), ZERO)
    return FeatureValue.of(
        "sibling_coherence_error",
        quantize_prob(total - ONE),
        components={"sum": quantize_prob(total), "legs": D(str(len(ctx.sibling_probabilities)))},
        labels={"direction": "overpriced" if total > ONE else "underpriced"},
    )


# ---------------------------------------------------------------------------
# 10. Catalyst timing
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "hours_to_next_catalyst",
    family=FactorFamily.CATALYST,
    version="v0",
    description=(
        "Hours until the next scheduled information event - a data release, an "
        "official update, a settlement snapshot."
    ),
    requires=("catalysts",),
)
def hours_to_next_catalyst(ctx: FactorContext) -> FeatureValue:
    """When the next piece of news is due.

    A gap that persists *through* a scheduled release is different evidence from
    one measured hours before it. Without a schedule this refuses rather than
    reporting a large number, because "no catalyst known" and "no catalyst for a
    long time" are opposite research conditions.
    """
    if not ctx.catalysts:
        return FeatureValue.missing(
            "hours_to_next_catalyst", Unavailable.NO_CATALYST_SCHEDULE
        )
    upcoming = sorted(
        (
            (label, ensure_utc(when))
            for label, when in ctx.catalysts
            if ensure_utc(when) > ctx.decision_time
        ),
        key=lambda item: item[1],
    )
    if not upcoming:
        return FeatureValue.missing(
            "hours_to_next_catalyst",
            Unavailable.NO_CATALYST_SCHEDULE,
            labels={"note": "every known catalyst is in the past"},
        )
    label, when = upcoming[0]
    hours = (when - ctx.decision_time).total_seconds() / 3600.0
    return FeatureValue.of(
        "hours_to_next_catalyst",
        D(f"{hours:.4f}"),
        components={"pending_catalysts": D(str(len(upcoming)))},
        labels={"catalyst": label},
    )


# ---------------------------------------------------------------------------
# 11. Liquidity and capacity
# ---------------------------------------------------------------------------


@REGISTRY.factor(
    "executable_capacity_usd",
    family=FactorFamily.LIQUIDITY_CAPACITY,
    version="v0",
    description=(
        "Dollars of ask-side depth available at or better than a worst "
        "acceptable price."
    ),
    requires=("order_book",),
    needs_book=True,
)
def executable_capacity_usd(ctx: FactorContext) -> FeatureValue:
    """Whether a theoretical edge is a trade or a screenshot.

    Capped at the price where the conservative edge goes to zero, so this is
    capacity *at a profit* rather than total resting size. A book with $40,000
    behind it, all of it above the break-even price, has no capacity for this
    signal at all, and reporting the raw depth would say the opposite.
    """
    if ctx.book is None:
        return FeatureValue.missing("executable_capacity_usd", Unavailable.NO_ORDER_BOOK)
    side = _side_for(ctx)
    conservative = _conservative_probability(ctx, side)
    total = depth_usd(ctx.book, side)
    if conservative is None:
        return FeatureValue.of(
            "executable_capacity_usd",
            quantize_usd(total),
            components={"total_ask_depth_usd": quantize_usd(total)},
            labels={"side": side.value, "capped_at_breakeven": "false"},
            inputs_available_at=ensure_utc(ctx.book.observed_at),
        )
    profitable = depth_usd(ctx.book, side, max_price=conservative)
    return FeatureValue.of(
        "executable_capacity_usd",
        quantize_usd(profitable),
        components={
            "total_ask_depth_usd": quantize_usd(total),
            "breakeven_price": quantize_prob(conservative),
        },
        labels={"side": side.value, "capped_at_breakeven": "true"},
        inputs_available_at=ensure_utc(ctx.book.observed_at),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ArithmeticError, ValueError):
            return None
    return None


def _as_probability(value: object) -> Decimal | None:
    """A Decimal that is actually in [0, 1].

    A source estimate outside the unit interval is a parsing failure, not a
    confident forecast, and letting it through would produce a divergence of
    several hundred points that dominates every downstream sort.
    """
    number = _as_decimal(value)
    if number is None or number < ZERO or number > ONE:
        return None
    return number


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _conservative_probability(ctx: FactorContext, side: Side) -> Decimal | None:
    """The bound that gates a trade on this side.

    ``low`` for YES; ``1 - high`` for NO. Never ``1 - low`` on the NO side - that
    is the optimistic bound and it overstates NO-side edge systematically, which
    is the exact error the value pipeline documents refusing.
    """
    low, high = ctx.model_fair_probability_low, ctx.model_fair_probability_high
    if side is Side.YES:
        return low
    return None if high is None else ONE - high


def _maker_fee_per_contract(
    ctx: FactorContext, size: Decimal, price: Decimal
) -> Decimal:
    """Per-contract maker fee from the venues' published schedules.

    Kalshi charges makers only on series whose ``fee_type`` is the maker-fee
    variant; Polymarket never charges makers. Both are read from the market row
    rather than assumed, and a series this code has not seen pays nothing rather
    than a guessed rate - which is the direction that does not manufacture edge.
    """
    from pmvl_shared.enums import Platform  # noqa: PLC0415

    if ctx.market.platform != Platform.KALSHI:
        return ZERO
    if "maker" not in (ctx.market.fee_type or ""):
        return ZERO
    rate = ctx.market.maker_fee_rate
    if rate <= ZERO:
        return ZERO
    return safe_div(kalshi_maker_fee(size, price, rate=rate), size)


def _time_bucket(hours: float, ctx: FactorContext) -> str:
    edges: Sequence[int] = ctx.extra.get(
        "time_bucket_hours", DEFAULT_TIME_BUCKET_HOURS
    )
    if hours < 0:
        return "expired"
    previous = 0
    for edge in edges:
        if hours < edge:
            return f"{previous}-{edge}h"
        previous = edge
    return f">{previous}h"
