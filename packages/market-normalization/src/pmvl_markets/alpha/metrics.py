"""Strategy metrics, and the segmentation that makes them useful.

Probability metrics (Brier, log loss, calibration) answer "is the outcome model
any good?" and the existing backtest already computes them. They are necessary
and they are not sufficient: a perfectly calibrated model can lose money after
fees, and a poorly calibrated one can make money if the market is worse.

So the metrics here are about the *strategy*: what was deployed, what came back,
how long it took, how much of it filled, and how much capacity there was.

What is deliberately absent
---------------------------
**Annualised Sharpe.** It requires a return series with a meaningful periodicity
and enough observations for the second moment to mean anything, and this system
has neither: positions are irregular, overlapping, and few. Annualising a ratio
computed from thirty overlapping bets produces a number with a familiar name and
no content, and the familiar name is what makes it dangerous - a reader who sees
"Sharpe 2.1" will not read the footnote.

A per-bet mean-over-standard-deviation is reported instead, unannualised and
named so it cannot be mistaken for the other thing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from pmvl_shared.money import ZERO, D, quantize_prob, quantize_usd, safe_div

from .execution import FillConfidence, worst_fill_confidence
from .models import Liquidity
from .simulator import SimulatedTrade
from .strategy import ExitTrigger


@dataclass(frozen=True)
class StrategyMetrics:
    """What a set of trades did. Every field is a fact about those trades."""

    trades: int
    wins: int
    losses: int
    gross_pnl: Decimal
    net_pnl: Decimal
    fees_paid: Decimal
    deployed_capital: Decimal
    #: Net P&L over capital actually deployed. Not annualised.
    return_on_deployed: Decimal
    max_drawdown: Decimal
    turnover: Decimal
    average_holding: timedelta | None
    win_rate: Decimal
    profit_factor: Decimal | None
    #: Mean net P&L per contract, as the strategy expected it to be.
    expected_edge_per_contract: Decimal
    #: Mean net P&L per contract, as it turned out.
    realised_edge_per_contract: Decimal
    #: Unannualised. See the module docstring.
    per_bet_mean_over_stdev: Decimal | None
    worst_trade: Decimal
    loss_tail_p95: Decimal
    fill_confidence: FillConfidence
    maker_net_pnl: Decimal
    taker_net_pnl: Decimal
    maker_trades: int
    taker_trades: int
    capacity_usd: Decimal
    exit_triggers: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "gross_pnl": str(self.gross_pnl),
            "net_pnl": str(self.net_pnl),
            "fees_paid": str(self.fees_paid),
            "deployed_capital": str(self.deployed_capital),
            "return_on_deployed": str(self.return_on_deployed),
            "max_drawdown": str(self.max_drawdown),
            "turnover": str(self.turnover),
            "average_holding_seconds": (
                None
                if self.average_holding is None
                else int(self.average_holding.total_seconds())
            ),
            "win_rate": str(self.win_rate),
            "profit_factor": (
                None if self.profit_factor is None else str(self.profit_factor)
            ),
            "expected_edge_per_contract": str(self.expected_edge_per_contract),
            "realised_edge_per_contract": str(self.realised_edge_per_contract),
            "per_bet_mean_over_stdev": (
                None
                if self.per_bet_mean_over_stdev is None
                else str(self.per_bet_mean_over_stdev)
            ),
            "worst_trade": str(self.worst_trade),
            "loss_tail_p95": str(self.loss_tail_p95),
            "fill_confidence": self.fill_confidence.value,
            "maker_net_pnl": str(self.maker_net_pnl),
            "taker_net_pnl": str(self.taker_net_pnl),
            "maker_trades": self.maker_trades,
            "taker_trades": self.taker_trades,
            "capacity_usd": str(self.capacity_usd),
            "exit_triggers": dict(sorted(self.exit_triggers.items())),
            "note": (
                "no annualised Sharpe: irregular, overlapping positions and a "
                "small sample do not support one"
            ),
        }


def summarise(trades: Sequence[SimulatedTrade]) -> StrategyMetrics:
    """Compute every strategy metric over a set of trades.

    An empty set returns zeros rather than raising. "This strategy took no
    trades" is a genuine, common and informative result - especially for a
    system whose gates are supposed to refuse most opportunities - and a
    metrics function that could not express it would push callers into
    special-casing the interesting case.
    """
    if not trades:
        return StrategyMetrics(
            trades=0, wins=0, losses=0,
            gross_pnl=ZERO, net_pnl=ZERO, fees_paid=ZERO, deployed_capital=ZERO,
            return_on_deployed=ZERO, max_drawdown=ZERO, turnover=ZERO,
            average_holding=None, win_rate=ZERO, profit_factor=None,
            expected_edge_per_contract=ZERO, realised_edge_per_contract=ZERO,
            per_bet_mean_over_stdev=None, worst_trade=ZERO, loss_tail_p95=ZERO,
            fill_confidence=FillConfidence.TOUCH_ONLY,
            maker_net_pnl=ZERO, taker_net_pnl=ZERO,
            maker_trades=0, taker_trades=0, capacity_usd=ZERO,
        )

    nets = [t.net_pnl for t in trades]
    deployed = sum((t.deployed for t in trades), ZERO)
    gross = sum((t.gross_pnl for t in trades), ZERO)
    net = sum(nets, ZERO)
    fees = sum((t.entry_fee * t.contracts + t.exit_fee * t.contracts for t in trades), ZERO)
    wins = sum(1 for n in nets if n > ZERO)
    losses = sum(1 for n in nets if n < ZERO)

    gains = sum((n for n in nets if n > ZERO), ZERO)
    pains = -sum((n for n in nets if n < ZERO), ZERO)

    holds = [t.holding_time for t in trades if t.holding_time is not None]
    average_hold = (
        sum(holds, timedelta()) / len(holds) if holds else None
    )

    expected = [
        t.entry_signal.expected_edge_per_contract for t in trades
    ]
    realised = [t.realised_edge_per_contract for t in trades]

    exits: dict[str, int] = defaultdict(int)
    for trade in trades:
        if trade.exit_trigger is not None:
            exits[trade.exit_trigger.value] += 1

    maker = [t for t in trades if t.liquidity is Liquidity.MAKER]
    taker = [t for t in trades if t.liquidity is Liquidity.TAKER]

    return StrategyMetrics(
        trades=len(trades),
        wins=wins,
        losses=losses,
        gross_pnl=quantize_usd(gross),
        net_pnl=quantize_usd(net),
        fees_paid=quantize_usd(fees),
        deployed_capital=quantize_usd(deployed),
        return_on_deployed=quantize_prob(safe_div(net, deployed)),
        max_drawdown=_max_drawdown(trades),
        turnover=quantize_usd(deployed),
        average_holding=average_hold,
        win_rate=quantize_prob(safe_div(D(str(wins)), D(str(len(trades))))),
        profit_factor=(
            None if pains <= ZERO else quantize_usd(safe_div(gains, pains))
        ),
        expected_edge_per_contract=quantize_usd(_mean(expected)),
        realised_edge_per_contract=quantize_usd(_mean(realised)),
        per_bet_mean_over_stdev=_mean_over_stdev(nets),
        worst_trade=quantize_usd(min(nets)),
        loss_tail_p95=_loss_tail(nets),
        fill_confidence=worst_fill_confidence([t.fill_confidence for t in trades]),
        maker_net_pnl=quantize_usd(sum((t.net_pnl for t in maker), ZERO)),
        taker_net_pnl=quantize_usd(sum((t.net_pnl for t in taker), ZERO)),
        maker_trades=len(maker),
        taker_trades=len(taker),
        capacity_usd=quantize_usd(
            _mean([t.entry_signal.components.get("capacity_usd", ZERO) for t in trades])
        ),
        exit_triggers=dict(exits),
    )


def _mean(values: Sequence[Decimal]) -> Decimal:
    return safe_div(sum(values, ZERO), D(str(len(values)))) if values else ZERO


def _mean_over_stdev(values: Sequence[Decimal]) -> Decimal | None:
    """Per-bet mean over standard deviation. Unannualised, and named that way.

    Returns ``None`` below ten observations. A ratio computed from a handful of
    overlapping bets is dominated by whichever one was largest, and reporting it
    would invite exactly the comparison it cannot support.
    """
    if len(values) < 10:
        return None
    mean = _mean(values)
    variance = _mean([(v - mean) * (v - mean) for v in values])
    if variance <= ZERO:
        return None
    # Decimal has no sqrt on the context-free path; float is acceptable here
    # because this is a descriptive statistic and no verdict turns on its last
    # digit - unlike every price and fee, which stay Decimal throughout.
    stdev = D(str(float(variance) ** 0.5))
    return quantize_usd(safe_div(mean, stdev))


def _max_drawdown(trades: Sequence[SimulatedTrade]) -> Decimal:
    """Largest peak-to-trough fall in cumulative net P&L, in dollars.

    Ordered by exit time, because that is when the P&L was realised. Ordering by
    entry would report a drawdown sequence that never happened in that order.
    """
    ordered = sorted(
        trades, key=lambda t: (t.exit_at or t.entry_at, t.market_key)
    )
    peak, drawdown, cumulative = ZERO, ZERO, ZERO
    for trade in ordered:
        cumulative += trade.net_pnl
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return quantize_usd(drawdown)


def _loss_tail(values: Sequence[Decimal]) -> Decimal:
    """The 95th-percentile loss: how bad the bad ones get.

    Reported alongside the worst single trade because those answer different
    questions. One outlier can be bad luck; a fat 95th percentile is a property
    of the strategy.
    """
    losses = sorted(v for v in values if v < ZERO)
    if not losses:
        return ZERO
    index = max(0, int(len(losses) * Decimal("0.05")))
    return quantize_usd(losses[index])


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


#: The dimensions a research run must be able to slice on.
#:
#: The purpose is to find out whether a narrow sub-strategy holds the alpha even
#: when the aggregate does not - and equally, whether an aggregate that looks
#: positive is one bucket carrying eleven that are not.
SEGMENTATIONS: dict[str, Callable[[SimulatedTrade], str]] = {
    "category": lambda t: t.category or "unknown",
    "liquidity": lambda t: t.liquidity.value,
    "exit_trigger": lambda t: t.exit_trigger.value if t.exit_trigger else "open",
    "fill_confidence": lambda t: t.fill_confidence.value,
    # Not an `and`/`or` chain: a legitimate 0-10c bucket has value Decimal(0),
    # which is falsy, and the chain would report it as "unknown" - losing the
    # cheapest band, which is exactly the one whose calibration differs most.
    "price_bucket": lambda t: _bucket_price(
        t.entry_signal.components.get("price_bucket")
    ),
    "time_to_expiry": lambda t: _bucket_hours(
        t.entry_signal.components.get("hours_to_resolution")
    ),
    "spread_bucket": lambda t: _bucket_cents(
        t.entry_signal.components.get("book_spread")
    ),
    "liquidity_bucket": lambda t: _bucket_usd(
        t.entry_signal.components.get("capacity_usd")
    ),
    "source_divergence_bucket": lambda t: _bucket_points(
        t.entry_signal.components.get("source_divergence")
    ),
    "threshold_distance_bucket": lambda t: _bucket_points(
        t.entry_signal.components.get("threshold_distance")
    ),
    "signal_strength_bucket": lambda t: _bucket_points(
        t.entry_signal.score
    ),
}


def segment(
    trades: Sequence[SimulatedTrade],
    *,
    dimensions: Iterable[str] | None = None,
    min_trades: int = 5,
) -> dict[str, dict[str, Any]]:
    """Metrics per bucket, per dimension.

    ``min_trades`` marks thin buckets rather than dropping them. A bucket with
    two trades and a spectacular ROI is the single most misleading cell in an
    attribution table, and hiding it entirely would lose the fact that the
    strategy fired there at all. It is reported with ``"thin": true`` so a reader
    and a downstream filter can both see it.
    """
    wanted = list(dimensions) if dimensions is not None else list(SEGMENTATIONS)
    report: dict[str, dict[str, Any]] = {}
    for dimension in wanted:
        key_of = SEGMENTATIONS[dimension]
        buckets: dict[str, list[SimulatedTrade]] = defaultdict(list)
        for trade in trades:
            buckets[key_of(trade)].append(trade)
        report[dimension] = {
            bucket: {
                **summarise(rows).as_dict(),
                "thin": len(rows) < min_trades,
            }
            for bucket, rows in sorted(buckets.items())
        }
    return report


def _bucket_price(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    lower = int(value * 100)
    return f"{lower}-{lower + 10}c"


def _bucket_hours(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    hours = float(value)
    for edge in (1, 6, 24, 72, 168, 720):
        if hours < edge:
            return f"<{edge}h"
    return ">720h"


def _bucket_cents(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    cents = int(value * 100)
    for edge in (1, 2, 3, 5, 10):
        if cents < edge:
            return f"<{edge}c"
    return ">=10c"


def _bucket_usd(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    for edge in (100, 500, 2000, 10000):
        if value < edge:
            return f"<${edge}"
    return ">=$10000"


def _bucket_points(value: Decimal | None) -> str:
    """Signed probability-point buckets, in 4-point steps up to 12.

    Signed rather than absolute: a strategy that works when the model is above
    the market and fails when it is below is a real and common asymmetry, and an
    absolute-value bucket would average the two into nothing.
    """
    if value is None:
        return "unknown"
    points = value * 100
    for edge in (-12, -8, -4, 0, 4, 8, 12):
        if points < edge:
            return f"<{edge}pp"
    return ">=12pp"


def expected_versus_realised(trades: Sequence[SimulatedTrade]) -> dict[str, str]:
    """How much of the edge the strategy expected actually arrived.

    The most useful single diagnostic in the whole report. A ratio near one with
    a small sample says the machinery is consistent; a ratio near zero says the
    edge was an artefact of the cost model rather than of the market, and no
    amount of additional data will fix that.
    """
    if not trades:
        return {"expected": "0", "realised": "0", "ratio": "0", "note": "no trades"}
    expected = _mean([t.entry_signal.expected_edge_per_contract for t in trades])
    realised = _mean([t.realised_edge_per_contract for t in trades])
    return {
        "expected": str(quantize_usd(expected)),
        "realised": str(quantize_usd(realised)),
        "ratio": str(quantize_usd(safe_div(realised, expected))) if expected else "n/a",
        "note": (
            "a ratio far below 1 means the modelled edge did not survive "
            "execution, which more data will not repair"
        ),
    }
