"""Walk-forward backtest over immutable recommendation snapshots.

Look-ahead prevention
---------------------
The backtest reads **only** ``recommendation_snapshots``. It never queries the live
market table, never re-runs the model, and never re-prices an entry. Every field it
consumes - entry price, all-in cost, fair probability, interval, confidence, order
book - was frozen at publication time, before the outcome was known.

Concretely, the following are structurally impossible here rather than merely avoided
by convention:

* **Re-pricing entries.** Fills use ``total_cost_at_recommendation``. There is no code
  path that reads a current quote.
* **Re-fitting probabilities.** ``fair_probability`` comes from the snapshot. The
  model is not invoked.
* **Survivorship.** Every snapshot is evaluated, including ones that lost. Snapshots
  are never deleted.
* **Reordering.** Rank came from the snapshot's publication-time score.

Data quality
------------
Every trade records how its fill price was derived. A backtest run whose fills came
from top-of-book quotes is not the same claim as one backed by full depth, and a run
built on candlesticks is not an executable-price backtest at all. The run's overall
``data_quality`` is the *worst* quality of any trade in it, so a single degraded fill
cannot be hidden behind an aggregate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.enums import DataProvenance, DataQuality, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.timeutil import utcnow

from ..db_models import BacktestRun, BacktestTrade, Market, RecommendationSnapshot
from ..pricing.execution import fractional_kelly
from .metrics import Observation, summarize

log = get_logger(__name__)

#: Bumped when the simulation rules change, so old runs stay interpretable.
BACKTEST_VERSION = "backtest-v1.0.0"


@dataclass
class StakePlan:
    """How much to put on one selected snapshot."""

    contracts: Decimal
    stake: Decimal


@dataclass
class Strategy:
    """A named selection + sizing rule."""

    name: str
    description: str
    #: Chooses which of a day's snapshots to act on, in rank order.
    select: Callable[[list[RecommendationSnapshot]], list[RecommendationSnapshot]]
    #: Decides position size for one snapshot.
    size: Callable[[RecommendationSnapshot], StakePlan]
    horizons: tuple[str, ...] = ("24h", "7d", "30d")
    platforms: tuple[str, ...] = ()
    min_confidence: Decimal = ZERO


def _fixed_dollar(amount: Decimal) -> Callable[[RecommendationSnapshot], StakePlan]:
    def sizer(snapshot: RecommendationSnapshot) -> StakePlan:
        cost = snapshot.total_cost_at_recommendation
        if cost <= 0:
            return StakePlan(ZERO, ZERO)
        contracts = safe_div(amount, cost)
        # Never simulate buying more than the book actually showed.
        if snapshot.executable_size > 0:
            contracts = min(contracts, snapshot.executable_size)
        return StakePlan(contracts, quantize_usd(contracts * cost))

    return sizer


def _kelly_sizer(bankroll: Decimal) -> Callable[[RecommendationSnapshot], StakePlan]:
    def sizer(snapshot: RecommendationSnapshot) -> StakePlan:
        cost = snapshot.total_cost_at_recommendation
        if cost <= 0:
            return StakePlan(ZERO, ZERO)
        # Sized on the conservative lower bound, matching how it was recommended.
        fraction = fractional_kelly(snapshot.fair_probability_low, cost)
        stake = bankroll * fraction
        if stake <= 0:
            return StakePlan(ZERO, ZERO)
        contracts = safe_div(stake, cost)
        if snapshot.executable_size > 0:
            contracts = min(contracts, snapshot.executable_size)
        return StakePlan(contracts, quantize_usd(contracts * cost))

    return sizer


def _top_n(n: int) -> Callable[[list[RecommendationSnapshot]], list[RecommendationSnapshot]]:
    def selector(day: list[RecommendationSnapshot]) -> list[RecommendationSnapshot]:
        return sorted(day, key=lambda s: s.rank)[:n]

    return selector


def default_strategies(*, bankroll: Decimal = D(1000)) -> list[Strategy]:
    """The strategy set the spec requires, plus the slices used for attribution."""
    return [
        Strategy("top1_10usd", "Buy the single highest-ranked recommendation each day, $10",
                 _top_n(1), _fixed_dollar(D(10))),
        Strategy("top3_equal_10usd", "Equal-weight the top 3 each day, $10 each",
                 _top_n(3), _fixed_dollar(D(10))),
        Strategy("top10_equal_10usd", "Equal-weight the top 10 each day, $10 each",
                 _top_n(10), _fixed_dollar(D(10))),
        Strategy("top10_equal_25usd", "Equal-weight the top 10 each day, $25 each",
                 _top_n(10), _fixed_dollar(D(25))),
        Strategy("top10_fractional_kelly", "Top 10, sized by fractional Kelly on the "
                 "conservative probability bound", _top_n(10), _kelly_sizer(bankroll)),
        Strategy("high_confidence_only", "Top 10 restricted to model confidence >= 0.5",
                 _top_n(10), _fixed_dollar(D(10)), min_confidence=D("0.5")),
        Strategy("resolves_within_24h", "Only markets resolving within 24 hours",
                 _top_n(10), _fixed_dollar(D(10)), horizons=("24h",)),
        Strategy("kalshi_only", "Top 10, Kalshi only",
                 _top_n(10), _fixed_dollar(D(10)), platforms=("kalshi",)),
        Strategy("polymarket_only", "Top 10, Polymarket only",
                 _top_n(10), _fixed_dollar(D(10)), platforms=("polymarket",)),
        Strategy("cross_platform_combined", "Top 10 across both venues",
                 _top_n(10), _fixed_dollar(D(10)), platforms=("kalshi", "polymarket")),
    ]


@dataclass
class BacktestResult:
    run_id: str
    strategy: str
    n_recommendations: int = 0
    n_settled: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    data_quality: DataQuality = DataQuality.UNKNOWN
    window_start: datetime | None = None
    window_end: datetime | None = None
    walk_forward: bool = True
    notes: str = ""
    by_slice: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "n_recommendations": self.n_recommendations,
            "n_settled": self.n_settled,
            "data_quality": self.data_quality.value,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "walk_forward": self.walk_forward,
            "metrics": self.metrics,
            "by_slice": self.by_slice,
            "notes": self.notes,
        }


def _snapshot_quality(snapshot: RecommendationSnapshot) -> DataQuality:
    """How trustworthy this snapshot's simulated fill is.

    ``ORDERBOOK`` requires actual ask levels to have been frozen. A snapshot with only
    a best-ask figure is ``QUOTE``: the price is real but the available depth is not
    known, so a large simulated fill would be fiction.
    """
    book = snapshot.orderbook_snapshot or {}
    side_key = "yes_asks" if snapshot.side == Side.YES.value else "no_asks"
    if book.get(side_key):
        return DataQuality.ORDERBOOK
    if book.get("best_yes_ask") or book.get("best_no_ask"):
        return DataQuality.QUOTE
    return DataQuality.UNKNOWN


def _worst_quality(qualities: Sequence[DataQuality]) -> DataQuality:
    order = [
        DataQuality.UNKNOWN,
        DataQuality.CANDLE,
        DataQuality.QUOTE,
        DataQuality.ORDERBOOK,
    ]
    if not qualities:
        return DataQuality.UNKNOWN
    return min(qualities, key=order.index)


def load_snapshots(
    session: Session,
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    settled_only: bool = True,
) -> list[RecommendationSnapshot]:
    stmt = select(RecommendationSnapshot).order_by(
        RecommendationSnapshot.snapshot_date, RecommendationSnapshot.rank
    )
    if settled_only:
        stmt = stmt.where(RecommendationSnapshot.final_result.is_not(None))
    if window_start:
        stmt = stmt.where(RecommendationSnapshot.recommendation_created_at >= window_start)
    if window_end:
        stmt = stmt.where(RecommendationSnapshot.recommendation_created_at <= window_end)
    return list(session.scalars(stmt))


def run_strategy(
    session: Session,
    strategy: Strategy,
    snapshots: Sequence[RecommendationSnapshot],
    *,
    persist: bool = True,
    now: datetime | None = None,
) -> BacktestResult:
    """Simulate one strategy over the frozen snapshot history."""
    now = now or utcnow()
    run_id = f"{strategy.name}-{uuid4().hex[:10]}"
    result = BacktestResult(run_id=run_id, strategy=strategy.name)

    eligible = [
        s for s in snapshots
        if s.horizon in strategy.horizons
        and (not strategy.platforms or s.platform in strategy.platforms)
        and s.model_confidence >= strategy.min_confidence
    ]
    if not eligible:
        result.notes = (
            "no settled snapshots matched this strategy's filters; "
            "metrics are undefined rather than zero"
        )
        if persist:
            _persist_run(session, result, strategy, trades=[], now=now)
        return result

    # Group by publication day, then apply the selector *within* each day. Selecting
    # across the whole history at once would let a later day's rankings influence an
    # earlier day's picks.
    by_day: dict[date, list[RecommendationSnapshot]] = defaultdict(list)
    for snapshot in eligible:
        by_day[snapshot.snapshot_date].append(snapshot)

    observations: list[Observation] = []
    trades: list[BacktestTrade] = []
    qualities: list[DataQuality] = []
    attributes: dict[int, dict[str, Any]] = {}

    for day in sorted(by_day):
        for snapshot in strategy.select(by_day[day]):
            result.n_recommendations += 1
            plan = strategy.size(snapshot)
            if plan.contracts <= 0 or plan.stake <= 0:
                continue

            payout_per_contract = _payout_per_contract(snapshot)
            if payout_per_contract is None:
                continue

            cost = snapshot.total_cost_at_recommendation
            pnl = quantize_usd((payout_per_contract - cost) * plan.contracts)
            quality = _snapshot_quality(snapshot)
            qualities.append(quality)

            outcome_value = _outcome_value(snapshot)
            observations.append(
                Observation(
                    predicted_probability=snapshot.fair_probability,
                    market_probability=_market_probability(snapshot),
                    cost=cost,
                    payout=quantize_usd(payout_per_contract * plan.contracts),
                    stake=plan.stake,
                    pnl=pnl,
                    won=pnl > 0,
                    outcome_value=outcome_value,
                )
            )
            attributes[len(observations) - 1] = {
                "platform": snapshot.platform,
                "horizon": snapshot.horizon,
                "confidence_band": _confidence_band(snapshot.model_confidence),
            }

            trades.append(
                BacktestTrade(
                    run_id=run_id,
                    snapshot_id=snapshot.id,
                    market_id=snapshot.market_id,
                    platform=snapshot.platform,
                    market_title=snapshot.market_title,
                    side=snapshot.side,
                    entered_at=snapshot.recommendation_created_at,
                    fill_price=snapshot.entry_price_at_recommendation,
                    fees=quantize_usd(cost - snapshot.entry_price_at_recommendation),
                    contracts=plan.contracts,
                    stake=plan.stake,
                    predicted_probability=snapshot.fair_probability,
                    market_probability=_market_probability(snapshot),
                    outcome=snapshot.final_result,
                    payout=quantize_usd(payout_per_contract * plan.contracts),
                    pnl=pnl,
                    settled_at=snapshot.settled_at,
                    data_quality=quality.value,
                )
            )

    result.n_settled = len(observations)
    result.metrics = summarize(observations)
    result.data_quality = _worst_quality(qualities)
    result.window_start = min(
        (s.recommendation_created_at for s in eligible), default=None
    )
    result.window_end = max((s.recommendation_created_at for s in eligible), default=None)

    from .metrics import split_by

    result.by_slice = split_by(
        observations, ("platform", "horizon", "confidence_band"), attributes
    )

    if result.data_quality != DataQuality.ORDERBOOK:
        result.notes = (
            f"data quality is '{result.data_quality.value}': at least one simulated "
            "fill was not backed by a frozen depth ladder, so achievable size is not "
            "established for every trade"
        )

    if persist:
        _persist_run(session, result, strategy, trades=trades, now=now)
    return result


def _payout_per_contract(snapshot: RecommendationSnapshot) -> Decimal | None:
    """Terminal value of the recommended side, from the recorded settlement."""
    result = (snapshot.final_result or "").lower()
    if result in ("yes", "no"):
        yes_payout = ONE if result == "yes" else ZERO
    elif result == "fifty_fifty":
        yes_payout = D("0.5")
    elif result == "void":
        # A void market returns the stake; treat the payout as the cost paid.
        return snapshot.total_cost_at_recommendation
    else:
        return None
    return yes_payout if snapshot.side == Side.YES.value else ONE - yes_payout


def _outcome_value(snapshot: RecommendationSnapshot) -> Decimal:
    """Realised outcome in the *recommended side's* frame, for scoring.

    The model's probability is stated for the side it recommended, so calibration has
    to be measured in that same frame: a NO recommendation that wins is an outcome of
    1 against a predicted probability of winning.
    """
    result = (snapshot.final_result or "").lower()
    if result == "fifty_fifty":
        return D("0.5")
    if result == "yes":
        return ONE if snapshot.side == Side.YES.value else ZERO
    if result == "no":
        return ONE if snapshot.side == Side.NO.value else ZERO
    return D("0.5")


def _market_probability(snapshot: RecommendationSnapshot) -> Decimal | None:
    """The market's own implied probability for the recommended side at entry.

    This is the benchmark the model has to beat. Falls back to the entry price, which
    for a binary contract *is* the market's implied probability of that side winning.
    """
    evidence = snapshot.evidence_snapshot or {}
    implied = evidence.get("market_implied_probability")
    if implied:
        value = D(implied)
        return value if snapshot.side == Side.YES.value else ONE - value
    return snapshot.entry_price_at_recommendation


def _confidence_band(confidence: Decimal) -> str:
    if confidence >= D("0.7"):
        return "high"
    if confidence >= D("0.4"):
        return "medium"
    return "low"


def _persist_run(
    session: Session,
    result: BacktestResult,
    strategy: Strategy,
    *,
    trades: Sequence[BacktestTrade],
    now: datetime,
) -> None:
    session.add(
        BacktestRun(
            run_id=result.run_id,
            strategy=strategy.name,
            model_version=BACKTEST_VERSION,
            window_start=result.window_start,
            window_end=result.window_end,
            walk_forward=True,
            data_quality=result.data_quality.value,
            n_recommendations=result.n_recommendations,
            n_settled=result.n_settled,
            metrics=result.metrics,
            calibration=result.metrics.get("calibration_curve"),
            config={
                "description": strategy.description,
                "horizons": list(strategy.horizons),
                "platforms": list(strategy.platforms),
                "min_confidence": str(strategy.min_confidence),
                "by_slice": result.by_slice,
            },
            notes=result.notes,
            provenance=DataProvenance.LIVE.value,
            created_at=now,
        )
    )
    for trade in trades:
        session.add(trade)
    session.flush()


def run_backtest(
    session: Session,
    *,
    strategies: Sequence[Strategy] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    now: datetime | None = None,
) -> list[BacktestResult]:
    """Run every strategy over the settled snapshot history."""
    now = now or utcnow()
    strategies = strategies or default_strategies()
    snapshots = load_snapshots(
        session, window_start=window_start, window_end=window_end, settled_only=True
    )

    if not snapshots:
        log.info(
            "backtest has no settled snapshots yet; this is expected until published "
            "recommendations reach their resolution date"
        )

    results = [run_strategy(session, s, snapshots, now=now) for s in strategies]
    log.info(
        "backtest complete: %d strategies, %d settled snapshots available",
        len(results), len(snapshots),
    )
    return results
