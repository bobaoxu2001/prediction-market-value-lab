"""Strategies: when to enter, and - equally - when to leave.

The existing backtest holds every position to settlement. That is correct for
grading a published recommendation, and it cannot express the trade this whole
exercise is about:

    buy at 29c, sell at 43c, before the binary event has resolved at all.

So a strategy here has two policies, not one. :class:`SettlementMode` keeps the
old behaviour and is the honest default for a signal whose thesis is about the
outcome. :class:`ConvergenceMode` is for a signal whose thesis is about the
*price*, and it needs exit conditions because "the market converged" is a thing
that happens and then stops being true.

Component attribution is a requirement, not a nicety
----------------------------------------------------
:class:`Signal` carries every component that produced it. Collapsing a gap, a
fee, a spread and a freshness check into one number makes a strategy that works
indistinguishable from one that got lucky on the one component doing all the
work - and the segmentation report in :mod:`.metrics` has nothing to segment on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from pmvl_shared.enums import Side
from pmvl_shared.money import ONE, ZERO, quantize_prob, quantize_usd
from pmvl_shared.schemas import NormalizedMarket, OrderBook

from .execution import maker_limit_price
from .models import (
    AlphaScore,
    Liquidity,
    OutcomeEstimate,
    ProposedOrder,
)
from .registry import FactorContext, FeatureVector
from .sizing import (
    NO_TRADE,
    DeclineReason,
    ExposureLedger,
    ExposureLimits,
    SizeDecision,
    day_key,
    size_position,
)


class ExitTrigger(str, Enum):
    """Why a position was closed. A closed vocabulary, so exits can be counted.

    The distribution over these is one of the more informative outputs of a
    convergence run: a strategy whose exits are mostly ``MAX_HOLD`` never
    converged, and one whose exits are mostly ``SIGNAL_INVALIDATED`` was reacting
    to noise.
    """

    CONVERGED = "converged"
    ALPHA_DECAYED = "alpha_decayed"
    SIGNAL_INVALIDATED = "signal_invalidated"
    TIME_STOP = "time_stop"
    RISK_STOP = "risk_stop"
    NEAR_SETTLEMENT = "near_settlement"
    MAX_HOLD = "max_hold"
    SETTLED = "settled"


@dataclass(frozen=True)
class Signal:
    """A strategy's decision at one instant, with everything that produced it.

    ``components`` and ``feature_names`` are what make an attribution report
    possible later. They are stored on the signal rather than recomputed at
    analysis time because recomputing would use whatever the factors do *now*,
    and the point of a frozen decision is that it cannot be revised.
    """

    market_key: str
    decision_time: datetime
    side: Side
    liquidity: Liquidity
    #: The strategy's own conviction, after every conservative adjustment.
    score: Decimal
    conservative_probability: Decimal
    executable_probability: Decimal
    expected_edge_per_contract: Decimal
    alpha: AlphaScore
    components: Mapping[str, Decimal] = field(default_factory=dict)
    feature_names: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    #: Set when the strategy looked and decided not to act.
    declined_reason: str | None = None

    @property
    def actionable(self) -> bool:
        return self.declined_reason is None and self.expected_edge_per_contract > ZERO

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_key": self.market_key,
            "decision_time": self.decision_time.isoformat(),
            "side": self.side.value,
            "liquidity": self.liquidity.value,
            "score": str(self.score),
            "conservative_probability": str(self.conservative_probability),
            "executable_probability": str(self.executable_probability),
            "expected_edge_per_contract": str(self.expected_edge_per_contract),
            "alpha": self.alpha.as_dict(),
            "components": {k: str(v) for k, v in self.components.items()},
            "feature_names": list(self.feature_names),
            "reasons": list(self.reasons),
            "declined_reason": self.declined_reason,
        }


@dataclass(frozen=True)
class ExitDecision:
    """Whether to close, and why."""

    should_exit: bool
    trigger: ExitTrigger | None = None
    detail: str = ""


@runtime_checkable
class Strategy(Protocol):
    """Entry and exit, as two separate questions."""

    name: str
    version: str

    def evaluate(
        self,
        ctx: FactorContext,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        alpha: AlphaScore,
    ) -> Signal:
        """Decide whether there is a trade here, and describe it."""

    def size(
        self,
        signal: Signal,
        market: NormalizedMarket,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        ledger: ExposureLedger,
    ) -> SizeDecision:
        """Decide how much, or decline."""

    def should_exit(
        self,
        position: "OpenPosition",
        ctx: FactorContext,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        alpha: AlphaScore,
    ) -> ExitDecision:
        """Decide whether to close an open position."""


@dataclass
class OpenPosition:
    """A filled position, with the decision that created it frozen onto it.

    ``entry_signal`` is kept whole rather than reduced to a price. An exit policy
    needs to know what the entry believed in order to notice that it stopped
    being true, and a position that remembers only its cost basis cannot
    distinguish "the thesis played out" from "the thesis was wrong and the price
    moved anyway".
    """

    market_key: str
    side: Side
    liquidity: Liquidity
    entry_price: Decimal
    contracts: Decimal
    opened_at: datetime
    entry_signal: Signal
    fill_confidence: str
    #: Appended, never replaced: a position may fill in pieces.
    fills: list[tuple[datetime, Decimal, Decimal]] = field(default_factory=list)

    @property
    def notional(self) -> Decimal:
        return quantize_usd(self.entry_price * self.contracts)

    def age_at(self, when: datetime) -> timedelta:
        return when - self.opened_at


@dataclass(frozen=True)
class ExitPolicy:
    """When a convergence position should be closed.

    Every threshold is a stated assumption, not a fitted value. Fitting them
    against the same period the strategy is evaluated on is the most common way
    a convergence backtest becomes a curve-fit, and the split machinery in
    :mod:`.evidence` exists so that fitting them later happens on a period that
    is not the one used to report the result.
    """

    #: Close when the remaining gap has shrunk to this fraction of the original.
    converged_fraction: Decimal = Decimal("0.35")
    #: Close when the alpha model's expected move drops below this.
    min_expected_move: Decimal = Decimal("0.01")
    #: Close when the source that produced the signal reverses by this much.
    invalidation_move: Decimal = Decimal("0.05")
    #: Hard cap on holding time.
    max_hold: timedelta = timedelta(hours=48)
    #: Close this long before expected resolution rather than riding into it.
    settlement_buffer: timedelta = timedelta(hours=2)
    #: Close if the position is down this much per contract.
    risk_stop_per_contract: Decimal = Decimal("0.08")


@dataclass(frozen=True)
class SettlementMode:
    """Enter on the signal, hold to settlement.

    The honest default when the thesis is about the *outcome* rather than the
    price path. It never exits early, so its result is a statement about the
    outcome model and nothing else - which makes it the right control to compare
    a convergence run against.
    """

    name: str = "settlement"

    def should_exit(
        self,
        position: OpenPosition,
        ctx: FactorContext,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        alpha: AlphaScore,
    ) -> ExitDecision:
        return ExitDecision(False, detail="settlement mode holds to resolution")


@dataclass(frozen=True)
class ConvergenceMode:
    """Enter on mispricing, exit when the reason to hold is gone.

    Five ways out, checked in the order below. The order matters: an invalidated
    signal should close as an invalidation even if it is also near its time stop,
    because those are different findings and only one of them says the strategy
    was wrong.
    """

    policy: ExitPolicy = ExitPolicy()
    name: str = "convergence"

    def should_exit(
        self,
        position: OpenPosition,
        ctx: FactorContext,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        alpha: AlphaScore,
    ) -> ExitDecision:
        entry_gap = position.entry_signal.components.get("raw_gap")
        current = features.number("source_divergence")

        # 1. The source that produced the signal has reversed. Not a decayed
        #    signal - a wrong one - and it must not be counted as convergence.
        if entry_gap is not None and current is not None:
            if entry_gap > ZERO and current < -self.policy.invalidation_move:
                return ExitDecision(
                    True,
                    ExitTrigger.SIGNAL_INVALIDATED,
                    f"gap reversed from {entry_gap} to {current}",
                )
            if entry_gap < ZERO and current > self.policy.invalidation_move:
                return ExitDecision(
                    True,
                    ExitTrigger.SIGNAL_INVALIDATED,
                    f"gap reversed from {entry_gap} to {current}",
                )

        # 2. Converged: most of the original gap has closed.
        if entry_gap is not None and current is not None and entry_gap != ZERO:
            remaining = abs(current) / abs(entry_gap)
            if remaining <= self.policy.converged_fraction:
                return ExitDecision(
                    True,
                    ExitTrigger.CONVERGED,
                    f"{quantize_prob(ONE - remaining)} of the gap has closed",
                )

        # 3. The alpha model no longer expects a move worth holding for.
        if not alpha.declined and abs(alpha.expected_move) < self.policy.min_expected_move:
            return ExitDecision(
                True,
                ExitTrigger.ALPHA_DECAYED,
                f"expected move {alpha.expected_move} below "
                f"{self.policy.min_expected_move}",
            )

        # 4. Settlement is close. Riding a convergence position into resolution
        #    silently converts it into an outcome bet, which is a different
        #    strategy with a different risk profile and would pollute the result.
        resolution = ctx.market.expected_resolution_time
        if resolution is not None:
            if resolution - ctx.decision_time <= self.policy.settlement_buffer:
                return ExitDecision(
                    True,
                    ExitTrigger.NEAR_SETTLEMENT,
                    "within the settlement buffer",
                )

        # 5. Time stop.
        if position.age_at(ctx.decision_time) >= self.policy.max_hold:
            return ExitDecision(
                True, ExitTrigger.MAX_HOLD, f"held for {self.policy.max_hold}"
            )

        # 6. Risk stop, measured against the executable exit, not the entry.
        market_now = ctx.executable_probability
        if market_now is not None:
            drawdown = (
                position.entry_price - market_now
                if position.side is Side.YES
                else market_now - position.entry_price
            )
            if drawdown >= self.policy.risk_stop_per_contract:
                return ExitDecision(
                    True,
                    ExitTrigger.RISK_STOP,
                    f"down {quantize_usd(drawdown)} per contract",
                )

        return ExitDecision(False)


@dataclass(frozen=True)
class SourceLagConvergenceStrategy:
    """Strategy v0: trade the gap between a trusted source and a lagging price.

    **This strategy does not assume its thesis is true.** It exists so the thesis
    can be tested. Everything it does is arranged so that a negative result is
    readable: components are retained, refusals are counted, and the exit
    distribution says which way it failed.

    Scope, deliberately narrow:

    * **weather threshold contracts**, where the settlement source is the NWS
      observation the model already reads - the mapping from source to
      settlement is exact rather than inferred;
    * **daily numeric official-source contracts** where the same is true.

    Not football, and not "every Kalshi category". A strategy that spans
    categories with different settlement mappings cannot attribute a failure to
    the thesis rather than to one category's rules.

    The scoring chain, in order, each step conservative:

    1. start from the *conservative* bound of the outcome interval, never the
       mean, so a wide estimate cannot manufacture edge;
    2. subtract the executable entry price, never the midpoint;
    3. subtract the venue's real fee at that size, including Kalshi's cent
       ceiling;
    4. require the source to be fresh, because a persistent gap is evidence
       against the thesis rather than for it;
    5. require the alpha model to expect a move, because disagreeing with the
       price is not the same as the price being about to change;
    6. require capacity, because an edge with no depth behind it is a
       screenshot.
    """

    name: str = "source_lag_convergence"
    version: str = "v0"
    mode: ConvergenceMode | SettlementMode = field(default_factory=ConvergenceMode)
    limits: ExposureLimits = field(default_factory=ExposureLimits)
    #: Minimum conservative edge per contract after fees.
    min_edge: Decimal = Decimal("0.02")
    #: Prefer resting when the spread is at least this wide.
    maker_spread_threshold: Decimal = Decimal("0.02")

    def evaluate(
        self,
        ctx: FactorContext,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        alpha: AlphaScore,
    ) -> Signal:
        side = (
            Side.YES
            if outcome.probability >= (ctx.executable_probability or outcome.probability)
            else Side.NO
        )
        market = ctx.executable_probability or ZERO
        conservative = outcome.conservative(side)

        def declined(reason: str) -> Signal:
            return Signal(
                market_key=f"{ctx.market.platform.value}:{ctx.market.platform_market_id}",
                decision_time=ctx.decision_time,
                side=side,
                liquidity=Liquidity.TAKER,
                score=ZERO,
                conservative_probability=conservative,
                executable_probability=market,
                expected_edge_per_contract=ZERO,
                alpha=alpha,
                feature_names=features.available_names,
                declined_reason=reason,
            )

        if not outcome.has_independent_prior:
            return declined("no independent prior")
        if alpha.declined:
            return declined(f"alpha model declined: {alpha.declined_reason}")

        liquidity = self._choose_liquidity(features)
        edge_feature = (
            "maker_proposed_edge" if liquidity is Liquidity.MAKER else "taker_edge"
        )
        edge = features.number(edge_feature)
        if edge is None:
            return declined(f"{edge_feature} unavailable")
        if edge < self.min_edge:
            return declined(
                f"{edge_feature} {quantize_usd(edge)} below the "
                f"{self.min_edge} floor after fees"
            )

        capacity = features.number("executable_capacity_usd")
        if capacity is None or capacity <= ZERO:
            return declined("no executable capacity at a profitable price")

        components: dict[str, Decimal] = {
            "conservative_edge": quantize_usd(edge),
            "alpha_expected_move": alpha.expected_move,
            "alpha_confidence": alpha.confidence,
            "capacity_usd": capacity,
        }
        for name in (
            "source_divergence",
            "source_update_lag_seconds",
            "source_change_magnitude",
            "book_spread",
            "hours_to_resolution",
            "price_bucket",
            "threshold_distance",
            "forecast_dispersion",
        ):
            value = features.number(name)
            if value is not None:
                components[name] = value
        alpha_components = dict(alpha.components)
        if "raw_gap" in alpha_components:
            components["raw_gap"] = alpha_components["raw_gap"]
        if "unabsorbed_gap" in alpha_components:
            components["unabsorbed_gap"] = alpha_components["unabsorbed_gap"]

        # The score is the conservative edge scaled by the alpha model's
        # confidence. Deliberately NOT a product of everything: multiplying six
        # discounts together produces a number nobody can reason about, and the
        # components above are what a segmentation report reads anyway.
        score = quantize_usd(edge * alpha.confidence)

        return Signal(
            market_key=f"{ctx.market.platform.value}:{ctx.market.platform_market_id}",
            decision_time=ctx.decision_time,
            side=side,
            liquidity=liquidity,
            score=score,
            conservative_probability=conservative,
            executable_probability=market,
            expected_edge_per_contract=quantize_usd(edge),
            alpha=alpha,
            components=components,
            feature_names=features.available_names,
            reasons=alpha.reasons
            + (f"{liquidity.value} entry chosen from the observed spread",),
        )

    def _choose_liquidity(self, features: FeatureVector) -> Liquidity:
        """Rest when the spread pays for the fill risk; otherwise lift.

        A one-tick book offers nothing to capture and posting into it is a taker
        order under a maker label - which the execution model refuses anyway, so
        deciding here keeps the refusal from looking like a strategy failure.
        """
        spread = features.number("book_spread")
        if spread is None:
            return Liquidity.TAKER
        return (
            Liquidity.MAKER if spread >= self.maker_spread_threshold else Liquidity.TAKER
        )

    def propose(
        self,
        signal: Signal,
        market: NormalizedMarket,
        book: OrderBook,
        contracts: Decimal,
    ) -> ProposedOrder | None:
        """Turn a signal and a size into a concrete order.

        Returns ``None`` when the book leaves no room to rest. That is a real
        outcome for a maker strategy and must not be quietly converted into a
        taker order at a worse price - which would be a different strategy
        reporting under this one's name.
        """
        if signal.liquidity is Liquidity.MAKER:
            limit = maker_limit_price(market, book, signal.side)
            if limit is None:
                return None
        else:
            limit = book.best_ask(signal.side)
            if limit is None:
                return None
        return ProposedOrder(
            market_key=signal.market_key,
            side=signal.side,
            liquidity=signal.liquidity,
            limit_price=limit,
            size=contracts,
            conservative_probability=signal.conservative_probability,
            expected_edge_if_filled=signal.expected_edge_per_contract,
            reasons=signal.reasons,
        )

    def size(
        self,
        signal: Signal,
        market: NormalizedMarket,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        ledger: ExposureLedger,
    ) -> SizeDecision:
        if not signal.actionable:
            return NO_TRADE(
                DeclineReason.NO_EDGE, signal.declined_reason or "not actionable"
            )
        capacity = features.number("executable_capacity_usd") or ZERO
        return size_position(
            edge_per_contract=signal.expected_edge_per_contract,
            entry_price=signal.executable_probability,
            interval_width=outcome.interval_width,
            capacity_usd=capacity,
            min_order_size=market.min_order_size,
            market_key=signal.market_key,
            category=market.category.value,
            strategy=self.name,
            day=day_key(signal.decision_time),
            # Siblings of one event move together; capping them as a group is
            # what stops "ten positions" being one position ten times.
            correlation_group=market.platform_event_id or signal.market_key,
            limits=self.limits,
            ledger=ledger,
        )

    def should_exit(
        self,
        position: OpenPosition,
        ctx: FactorContext,
        features: FeatureVector,
        outcome: OutcomeEstimate,
        alpha: AlphaScore,
    ) -> ExitDecision:
        return self.mode.should_exit(position, ctx, features, outcome, alpha)
