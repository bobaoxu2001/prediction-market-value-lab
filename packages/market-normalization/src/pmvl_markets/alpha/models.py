"""The three questions, kept apart.

PMVL used to go straight from ``fair probability`` to ``recommendation``. That
collapses three different questions into one, and the collapse is the most
expensive modelling error available here, because each of the three can be right
while the trade is still wrong.

Model A - Outcome
    ``P(contract settles YES | information at time t)``

    Calibration is the standard. A good outcome model is one whose 30% events
    happen 30% of the time.

Model B - Alpha
    ``E[market_price(t+h) - market_price(t) | information at time t]``

    A *different* question. Model fair 42% against a market at 30% does not imply
    a trade: the market may hold information the model does not, and "the market
    is wrong" and "the market will move" are separate claims. One is about the
    world; the other is about other participants.

    The standard here is realised convergence, not calibration.

Model C - Execution
    ``P(fill at price p | book, state, time)`` and the edge conditional on that
    fill.

    Trading at the ask is not the same act as posting a maker order. Lifting the
    offer is certain and expensive; resting is cheap and uncertain, and the
    uncertainty is adversely selected - the orders that fill fastest are the ones
    you would least like filled.

Why the boundary is types and not discipline
--------------------------------------------
:class:`AlphaScore` cannot be constructed from a probability alone. It requires a
horizon and a component breakdown, so "the model disagrees with the price" cannot
be typed into the slot where "the price will move" belongs. That is the same
device the independence gate uses: make the wrong thing hard to express rather
than documenting that it is wrong.

Nothing here fabricates a learned model. :class:`RuleBasedAlphaModel` is
deterministic and says so; there is not enough point-in-time history to fit
anything, and a fitted-looking model on absent data would be the single most
misleading artefact this repository could produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from pmvl_shared.enums import Side
from pmvl_shared.money import ONE, ZERO, D, quantize_prob, quantize_usd
from pmvl_shared.schemas import NormalizedMarket, OrderBook

from .registry import FactorContext, FeatureVector


# ---------------------------------------------------------------------------
# Model A - outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeEstimate:
    """What Model A says, and how much of it is independent of the price.

    ``has_independent_prior`` is carried rather than re-derived. It is the single
    most important gate in the system - a model whose every component comes from
    the target market's own price carries no information the market lacks - and
    re-deriving it here would create a second definition that could disagree with
    the ensemble's.
    """

    probability: Decimal
    low: Decimal
    high: Decimal
    confidence: Decimal
    has_independent_prior: bool
    model_version: str
    #: Component name -> probability, for attribution.
    components: Mapping[str, Decimal] = field(default_factory=dict)
    detail: str = ""

    @property
    def interval_width(self) -> Decimal:
        return self.high - self.low

    def conservative(self, side: Side) -> Decimal:
        """The bound that may gate a trade on ``side``.

        ``1 - high`` for NO, never ``1 - low``. The latter is the optimistic
        bound and overstates NO-side edge systematically.
        """
        return self.low if side is Side.YES else ONE - self.high


@runtime_checkable
class OutcomeModel(Protocol):
    """Anything that can answer Model A's question at a point in time."""

    name: str

    def estimate(self, ctx: FactorContext) -> OutcomeEstimate | None:
        """Return an estimate, or ``None`` for no opinion.

        No opinion is a first-class, honest result and is strictly better than
        0.5 at low confidence, which drags an ensemble toward a coin flip while
        looking like an answer.
        """


# ---------------------------------------------------------------------------
# Model B - alpha
# ---------------------------------------------------------------------------


class AlphaDirection(str, Enum):
    """Which way the price is expected to move, or that no claim is made."""

    UP = "up"
    DOWN = "down"
    NONE = "none"


@dataclass(frozen=True)
class AlphaScore:
    """Model B's answer: an expected *price move*, over a stated horizon.

    ``expected_move`` is in probability points of the market price, positive
    meaning up. It is **not** a probability, it is **not** an edge, and it is not
    interchangeable with either. The unit is stated in the field name because the
    single likeliest misuse of this object is reading it as ``fair - market``.

    ``components`` is mandatory in spirit even though the dataclass would accept
    an empty mapping: a score without attribution cannot answer "which part of
    this signal was doing the work", which is the only question that turns a
    backtest into knowledge.
    """

    expected_move: Decimal
    horizon: timedelta
    confidence: Decimal
    direction: AlphaDirection
    model_version: str
    #: Named contributions that produced the score. Never collapsed away.
    components: Mapping[str, Decimal] = field(default_factory=dict)
    #: Why this score is what it is, in words a reader can check.
    reasons: tuple[str, ...] = ()
    #: Set when the model declines. A declining score has expected_move ZERO and
    #: direction NONE, which is not the same as a genuine zero forecast.
    declined_reason: str | None = None

    @property
    def declined(self) -> bool:
        return self.declined_reason is not None

    @classmethod
    def decline(cls, reason: str, *, model_version: str, horizon: timedelta) -> "AlphaScore":
        return cls(
            expected_move=ZERO,
            horizon=horizon,
            confidence=ZERO,
            direction=AlphaDirection.NONE,
            model_version=model_version,
            declined_reason=reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_move": str(self.expected_move),
            "horizon_seconds": int(self.horizon.total_seconds()),
            "confidence": str(self.confidence),
            "direction": self.direction.value,
            "model_version": self.model_version,
            "components": {k: str(v) for k, v in self.components.items()},
            "reasons": list(self.reasons),
            "declined_reason": self.declined_reason,
        }


@runtime_checkable
class AlphaModel(Protocol):
    """Anything that can answer Model B's question."""

    name: str
    horizon: timedelta

    def score(
        self, ctx: FactorContext, features: FeatureVector, outcome: OutcomeEstimate
    ) -> AlphaScore:
        """Estimate the expected price move over ``horizon``."""


@dataclass(frozen=True)
class RuleBasedAlphaModel:
    """A deterministic convergence score. Explicitly not learned.

    There is not enough point-in-time history in this repository to fit anything,
    and a model that *looked* fitted would be the most misleading artefact it
    could ship. So the mapping from features to an expected move is written down,
    reviewable, and stated as a hypothesis.

    The hypothesis, in one line:

        a *fresh* source move that the market has *not* absorbed is followed by
        the market absorbing some of it.

    ``absorption_assumption`` is the fraction of the unabsorbed gap expected to
    close over the horizon. It is a **stated prior, not a measurement**, and this
    is the number that must be replaced by a fitted one before any claim about
    profitability is defensible. It is deliberately conservative: assuming full
    convergence would make every gap look tradeable, and the whole point of the
    exercise is to find out whether any of it converges at all.

    The model declines - rather than scoring zero - whenever the thesis does not
    apply. A declined score and a genuine zero are different research outcomes
    and must not be pooled.
    """

    name: str = "source_lag_convergence_v0"
    horizon: timedelta = timedelta(hours=6)
    model_version: str = "alpha-rule-v0"
    #: Fraction of the unabsorbed gap assumed to close over the horizon.
    absorption_assumption: Decimal = Decimal("0.35")
    #: Minimum unabsorbed gap, in probability points, worth scoring at all.
    min_unabsorbed_gap: Decimal = Decimal("0.03")
    #: Confidence ceiling. A rule with no fitted history may not look certain.
    max_confidence: Decimal = Decimal("0.35")

    def score(
        self, ctx: FactorContext, features: FeatureVector, outcome: OutcomeEstimate
    ) -> AlphaScore:
        def declined(reason: str) -> AlphaScore:
            return AlphaScore.decline(
                reason, model_version=self.model_version, horizon=self.horizon
            )

        if not outcome.has_independent_prior:
            # The independence gate, restated at the alpha layer. Without it the
            # "gap" is the model agreeing with the price and calling the residual
            # a forecast.
            return declined("no independent prior: the estimate is not independent of the price")

        divergence = features.get("source_divergence")
        if divergence is None or not divergence.available or divergence.value is None:
            return declined("no source divergence available")

        freshness = features.get("source_freshness_ok")
        if freshness is None or not freshness.available:
            return declined("source freshness unknown")
        if freshness.value == ZERO:
            # Stale-but-divergent is the anti-signal: the market has had time to
            # absorb the source and still disagrees, which is evidence the market
            # knows something the model does not.
            return declined("source is stale; a persistent gap is not a lag")

        gap = divergence.value
        reaction = features.get("market_reaction_to_source")
        absorbed = ZERO
        reasons: list[str] = []
        if reaction is not None and reaction.available:
            fraction = reaction.components.get("absorbed_fraction")
            if fraction is not None:
                absorbed = max(ZERO, min(ONE, fraction))
                reasons.append(
                    f"market has absorbed {quantize_prob(absorbed)} of the source move"
                )
        else:
            reasons.append(
                "no measured market reaction; treating the gap as fully unabsorbed, "
                "which is the optimistic reading and is flagged as such"
            )

        unabsorbed = gap * (ONE - absorbed)
        if abs(unabsorbed) < self.min_unabsorbed_gap:
            return declined(
                f"unabsorbed gap {quantize_prob(abs(unabsorbed))} below the "
                f"{self.min_unabsorbed_gap} floor"
            )

        expected_move = quantize_prob(unabsorbed * self.absorption_assumption)
        confidence = self._confidence(features, outcome)
        reasons.append(
            f"assumes {self.absorption_assumption} of the unabsorbed gap closes "
            f"within {int(self.horizon.total_seconds() // 3600)}h - a stated "
            "prior, not a measurement"
        )
        return AlphaScore(
            expected_move=expected_move,
            horizon=self.horizon,
            confidence=confidence,
            direction=AlphaDirection.UP if expected_move > ZERO else AlphaDirection.DOWN,
            model_version=self.model_version,
            components={
                "raw_gap": gap,
                "absorbed_fraction": quantize_prob(absorbed),
                "unabsorbed_gap": quantize_prob(unabsorbed),
                "absorption_assumption": self.absorption_assumption,
                "outcome_confidence": outcome.confidence,
                "interval_width": quantize_prob(outcome.interval_width),
            },
            reasons=tuple(reasons),
        )

    def _confidence(
        self, features: FeatureVector, outcome: OutcomeEstimate
    ) -> Decimal:
        """Scaled down by everything that should make a reader less sure.

        A wide outcome interval, a dispersed set of sources and a distant
        resolution all reduce it. There is no term that increases confidence,
        because nothing available here has earned one.
        """
        confidence = min(self.max_confidence, outcome.confidence)
        width = outcome.interval_width
        if width > ZERO:
            confidence *= max(Decimal("0.2"), ONE - width)
        dispersion = features.get("forecast_dispersion")
        if dispersion is not None and dispersion.available and dispersion.value:
            confidence *= max(Decimal("0.2"), ONE - dispersion.value * D("2"))
        return quantize_prob(max(ZERO, min(self.max_confidence, confidence)))


# ---------------------------------------------------------------------------
# Model C - execution (the interface; the mechanics live in execution.py)
# ---------------------------------------------------------------------------


class Liquidity(str, Enum):
    """Whether an order takes liquidity or provides it.

    Two different economic acts, and conflating them is how a backtest reports a
    maker rebate on a position it lifted from the offer.
    """

    TAKER = "taker"
    MAKER = "maker"


@dataclass(frozen=True)
class ProposedOrder:
    """What the strategy wants to do, before execution has had its say.

    Deliberately separate from anything resembling a fill. A proposed order is a
    request; whether it becomes a position is Model C's question, and keeping
    them as different types stops a research surface reporting proposals as
    trades.
    """

    market_key: str
    side: Side
    liquidity: Liquidity
    limit_price: Decimal
    size: Decimal
    #: What the strategy believed when it proposed this.
    conservative_probability: Decimal
    expected_edge_if_filled: Decimal
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_key": self.market_key,
            "side": self.side.value,
            "liquidity": self.liquidity.value,
            "limit_price": str(self.limit_price),
            "size": str(self.size),
            "conservative_probability": str(self.conservative_probability),
            "expected_edge_if_filled": str(self.expected_edge_if_filled),
            "reasons": list(self.reasons),
        }


@runtime_checkable
class ExecutionModel(Protocol):
    """Anything that can answer Model C's question."""

    name: str

    def price_order(
        self, market: NormalizedMarket, book: OrderBook, order: ProposedOrder
    ) -> "ExecutionAssessment":
        """Estimate fill probability and the edge conditional on that fill."""


@dataclass(frozen=True)
class ExecutionAssessment:
    """What Model C says about a proposed order.

    ``expected_edge_conditional_on_fill`` and ``fill_probability`` are kept
    apart, and their product is offered as a separate field rather than as *the*
    answer. A surface that only showed the product could not distinguish a small
    certain edge from a large improbable one, and those have different risk,
    different capacity and different reasons to be wrong.
    """

    fill_probability: Decimal
    expected_edge_conditional_on_fill: Decimal
    expected_fill_size: Decimal
    #: How the fill probability was arrived at, for the audit trail.
    fill_model: str
    #: A qualitative confidence in the fill estimate itself.
    fill_confidence: str
    #: Charge for being filled precisely when you would rather not be.
    adverse_selection_penalty: Decimal = ZERO
    notes: tuple[str, ...] = ()

    @property
    def unconditional_expected_edge(self) -> Decimal:
        """Edge times fill probability, net of adverse selection.

        Offered, not substituted. See the class docstring.
        """
        return quantize_usd(
            (self.expected_edge_conditional_on_fill - self.adverse_selection_penalty)
            * self.fill_probability
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "fill_probability": str(self.fill_probability),
            "expected_edge_conditional_on_fill": str(
                self.expected_edge_conditional_on_fill
            ),
            "unconditional_expected_edge": str(self.unconditional_expected_edge),
            "expected_fill_size": str(self.expected_fill_size),
            "fill_model": self.fill_model,
            "fill_confidence": self.fill_confidence,
            "adverse_selection_penalty": str(self.adverse_selection_penalty),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Adapter: the existing ensemble as an OutcomeModel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenOutcomeModel:
    """Model A backed by an estimate that was computed elsewhere and frozen.

    The adapter the research path actually uses. Rather than calling the async
    ensemble in the middle of a simulation - which would reach live providers and
    destroy the point-in-time guarantee in one step - a caller computes the
    estimate under an ``as_of`` context, freezes it, and hands it in here.

    This is the same structural argument the walk-forward backtest makes about
    reading only frozen snapshots: look-ahead becomes impossible by construction
    rather than avoided by convention.
    """

    estimates: Mapping[str, OutcomeEstimate]
    name: str = "frozen_ensemble"

    def estimate(self, ctx: FactorContext) -> OutcomeEstimate | None:
        key = f"{ctx.market.platform.value}:{ctx.market.platform_market_id}"
        return self.estimates.get(key)


def outcome_from_prediction(
    *,
    probability: Decimal,
    low: Decimal,
    high: Decimal,
    confidence: Decimal,
    has_independent_prior: bool,
    model_version: str,
    components: Mapping[str, Decimal] | None = None,
    detail: str = "",
) -> OutcomeEstimate:
    """Build an :class:`OutcomeEstimate` from a stored ``model_predictions`` row.

    Bounds are ordered defensively. A stored row with ``low > high`` is corrupt,
    and silently swapping them here would hide the corruption; raising is the
    behaviour that gets it fixed.
    """
    if low > high:
        raise ValueError(
            f"outcome interval is inverted: low={low} high={high}. A stored "
            "prediction with reversed bounds is corrupt, not something to repair "
            "in the reader."
        )
    return OutcomeEstimate(
        probability=quantize_prob(probability),
        low=quantize_prob(low),
        high=quantize_prob(high),
        confidence=quantize_prob(confidence),
        has_independent_prior=has_independent_prior,
        model_version=model_version,
        components=dict(components or {}),
        detail=detail,
    )


def implied_probability_from_book(book: OrderBook, side: Side, size: Decimal) -> Decimal | None:
    """The executable probability: VWAP of the ask ladder at ``size``.

    Not the midpoint. A midpoint is what the screen shows; the ask ladder is what
    a trade would cost, and on a wide book the difference exceeds any edge this
    system has measured.
    """
    from ..pricing.orderbook import executable_quote  # noqa: PLC0415

    quote = executable_quote(book, side, size)
    if quote is None or quote.filled_size <= ZERO:
        return None
    return quantize_prob(quote.average_price)
