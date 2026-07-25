"""Backtest and calibration metrics.

All computed with ``Decimal`` for money and ``float`` only inside statistical
formulas where precision is irrelevant to the conclusion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from pmvl_shared.money import D, ONE, ZERO, clamp_prob, quantize_usd, safe_div


@dataclass
class Observation:
    """One settled bet: what we predicted, what it cost, what happened."""

    predicted_probability: Decimal
    market_probability: Decimal | None
    cost: Decimal
    payout: Decimal
    stake: Decimal
    pnl: Decimal
    won: bool
    outcome_value: Decimal  # 1 for YES-side win, 0 for loss, 0.5 for a split


def brier_score(observations: Sequence[Observation], *, use_market: bool = False) -> float | None:
    """Mean squared error of the probability forecast. Lower is better.

    A forecast that always says 0.5 scores 0.25, which is the reference point for
    "no skill" on a balanced set.
    """
    values = []
    for obs in observations:
        p = obs.market_probability if use_market else obs.predicted_probability
        if p is None:
            continue
        values.append((float(p) - float(obs.outcome_value)) ** 2)
    return sum(values) / len(values) if values else None


def log_loss(observations: Sequence[Observation], *, use_market: bool = False) -> float | None:
    """Negative log likelihood. Punishes confident errors far harder than Brier."""
    values = []
    for obs in observations:
        p = obs.market_probability if use_market else obs.predicted_probability
        if p is None:
            continue
        p_float = float(clamp_prob(p))
        y = float(obs.outcome_value)
        values.append(-(y * math.log(p_float) + (1 - y) * math.log(1 - p_float)))
    return sum(values) / len(values) if values else None


def calibration_curve(
    observations: Sequence[Observation], *, bins: int = 10, use_market: bool = False
) -> list[dict[str, Any]]:
    """Reliability diagram data: predicted vs realised frequency per bin.

    A well-calibrated model has ``mean_predicted ~= observed_frequency`` in every
    populated bin. Empty bins are omitted rather than reported as zero, which would
    imply data that does not exist.
    """
    buckets: list[list[Observation]] = [[] for _ in range(bins)]
    for obs in observations:
        p = obs.market_probability if use_market else obs.predicted_probability
        if p is None:
            continue
        index = min(bins - 1, int(float(p) * bins))
        buckets[index].append(obs)

    out: list[dict[str, Any]] = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        probs = [
            float(o.market_probability if use_market else o.predicted_probability)
            for o in bucket
        ]
        outcomes = [float(o.outcome_value) for o in bucket]
        out.append(
            {
                "bin_lower": round(i / bins, 4),
                "bin_upper": round((i + 1) / bins, 4),
                "n": len(bucket),
                "mean_predicted": round(sum(probs) / len(probs), 4),
                "observed_frequency": round(sum(outcomes) / len(outcomes), 4),
            }
        )
    return out


def max_drawdown(pnl_series: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough decline of the cumulative P&L curve."""
    peak = ZERO
    cumulative = ZERO
    worst = ZERO
    for pnl in pnl_series:
        cumulative += pnl
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return quantize_usd(worst)


def profit_factor(observations: Sequence[Observation]) -> float | None:
    """Gross wins / gross losses. Above 1 means the winners outweigh the losers."""
    gains = sum(float(o.pnl) for o in observations if o.pnl > 0)
    losses = -sum(float(o.pnl) for o in observations if o.pnl < 0)
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return gains / losses


def sharpe_like(observations: Sequence[Observation]) -> float | None:
    """Mean per-bet return over its standard deviation.

    Not an annualised Sharpe ratio - the bets have heterogeneous holding periods and
    are not a time series of a single portfolio, so annualising would be misleading.
    It is a per-bet risk-adjusted return and is labelled that way in the UI.
    """
    returns = [
        float(safe_div(o.pnl, o.stake)) for o in observations if o.stake > 0
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(variance)
    return mean / stdev if stdev > 0 else None


def summarize(observations: Sequence[Observation]) -> dict[str, Any]:
    """Full metric set for one strategy run."""
    if not observations:
        return {
            "n_settled": 0,
            "note": "no settled observations; every metric below would be undefined",
        }

    total_stake = sum((o.stake for o in observations), ZERO)
    total_pnl = sum((o.pnl for o in observations), ZERO)
    wins = sum(1 for o in observations if o.won)

    model_brier = brier_score(observations)
    market_brier = brier_score(observations, use_market=True)

    return {
        "n_settled": len(observations),
        "wins": wins,
        "win_rate": round(wins / len(observations), 4),
        "avg_predicted_probability": round(
            float(sum((o.predicted_probability for o in observations), ZERO)) / len(observations), 4
        ),
        "avg_market_probability": (
            round(
                float(
                    sum(
                        (o.market_probability for o in observations if o.market_probability),
                        ZERO,
                    )
                )
                / max(1, sum(1 for o in observations if o.market_probability)),
                4,
            )
            if any(o.market_probability for o in observations)
            else None
        ),
        "total_stake": str(quantize_usd(total_stake)),
        "total_pnl": str(quantize_usd(total_pnl)),
        "roi": round(float(safe_div(total_pnl, total_stake)), 4) if total_stake > 0 else None,
        "max_drawdown": str(max_drawdown([o.pnl for o in observations])),
        "profit_factor": _round_or_none(profit_factor(observations)),
        "sharpe_like_per_bet": _round_or_none(sharpe_like(observations)),
        "brier_score": _round_or_none(model_brier),
        "log_loss": _round_or_none(log_loss(observations)),
        "market_brier_score": _round_or_none(market_brier),
        "market_log_loss": _round_or_none(log_loss(observations, use_market=True)),
        # The number that actually matters: did the model beat simply trusting the
        # market's own price? Positive means the model added information.
        "brier_improvement_vs_market": (
            round(market_brier - model_brier, 5)
            if model_brier is not None and market_brier is not None
            else None
        ),
        "calibration_curve": calibration_curve(observations),
        "market_calibration_curve": calibration_curve(observations, use_market=True),
    }


def _round_or_none(value: float | None, digits: int = 5) -> float | None:
    if value is None or math.isinf(value) or math.isnan(value):
        return None
    return round(value, digits)


def split_by(
    observations: Sequence[Observation], keys: Sequence[str], attribute_map: dict[int, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Metrics broken out by an attribute (platform, category, horizon, confidence)."""
    groups: dict[str, list[Observation]] = {}
    for index, obs in enumerate(observations):
        attrs = attribute_map.get(index, {})
        for key in keys:
            value = attrs.get(key)
            if value is None:
                continue
            groups.setdefault(f"{key}={value}", []).append(obs)
    return {name: summarize(group) for name, group in sorted(groups.items())}
