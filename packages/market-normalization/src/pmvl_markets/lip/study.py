"""Pooling market-days into one verdict, once (section 7).

The unit of independence is not the market: markets on the same day share that day's
conditions, and markets in the same family share a book of related ladders. So the
bootstrap resamples dates and families *both* (Owen's pigeonhole bootstrap): every draw
reweights each (date, family) cell by the product of its date's and its family's
multiplicities. Intervals are t-calibrated on G = min(counted days, families) because
twenty-odd clusters is too few for a percentile interval to mean what it says.

The estimand is dollars per admitted market-hour. Every admitted second counts in the
denominator - gated out, unobserved, closed, unquoted or unfilled - so nothing that
went badly can leave the average by being dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any

import numpy as np
from scipy import stats

from .sim_params import SIM, SimParams

__all__ = [
    "COMBOS",
    "Cell",
    "decide",
    "lofo",
    "pigeonhole_bootstrap",
    "point",
    "t_interval",
]

SCENARIOS = ("THROUGH", "FIFO", "HEAVY")
REGIMES = ("REG_LOW", "REG_HIGH")
VALUATIONS = ("MARK_1H", "DAY_EXIT")
#: the 12 reading combinations; a positive verdict needs every one of them
COMBOS = tuple(product(SCENARIOS, REGIMES, VALUATIONS))
ENVELOPES = ("LOW", "HIGH")


@dataclass
class Cell:
    """One (study day, family): admitted hours and, per series, dollars."""

    date: str
    family: str
    hours: float = 0.0
    #: (policy, combo, envelope) -> $
    value: dict[tuple[str, tuple[str, str, str], str], float] = field(default_factory=dict)


def _matrices(cells: list[Cell], series: list[tuple]) -> tuple[list[str], list[str], np.ndarray, np.ndarray]:
    dates = sorted({c.date for c in cells})
    fams = sorted({c.family for c in cells})
    di, fi = {d: i for i, d in enumerate(dates)}, {f: i for i, f in enumerate(fams)}
    h = np.zeros((len(dates), len(fams)))
    v = np.zeros((len(series), len(dates), len(fams)))
    for c in cells:
        h[di[c.date], fi[c.family]] += c.hours
        for s, key in enumerate(series):
            v[s, di[c.date], fi[c.family]] += c.value.get(key, 0.0)
    return dates, fams, h, v


def point(cells: list[Cell], key: tuple) -> float:
    hours = sum(c.hours for c in cells)
    return sum(c.value.get(key, 0.0) for c in cells) / hours if hours > 0 else 0.0


def pigeonhole_bootstrap(cells: list[Cell], series: list[tuple], *, draws: int = SIM.bootstrap_draws,
                         seed: int = SIM.seed) -> np.ndarray:
    """theta* for every series, all series sharing the same draws. Shape (series, draws)."""
    dates, fams, h, v = _matrices(cells, series)
    if not dates or not fams:  # nothing admitted all study: no draw has a denominator
        return np.full((len(series), draws), np.nan)
    rng = np.random.default_rng(seed)
    cd = rng.multinomial(len(dates), [1 / len(dates)] * len(dates), size=draws).astype(float)
    cf = rng.multinomial(len(fams), [1 / len(fams)] * len(fams), size=draws).astype(float)
    den = np.einsum("bd,df,bf->b", cd, h, cf)
    num = np.einsum("bd,sdf,bf->sb", cd, v, cf)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def t_interval(theta: float, se: float, g: int, alpha: float) -> tuple[float, float]:
    """theta +- t_{G-1, 1-alpha/2} * se (two-sided)."""
    if g < 2 or not np.isfinite(se):
        return (-np.inf, np.inf)
    t = stats.t.ppf(1 - alpha / 2, g - 1)
    return theta - t * se, theta + t * se


def lofo(cells: list[Cell], key: tuple) -> float:
    """theta without the family carrying the largest total value for this series."""
    by_family: dict[str, float] = {}
    for c in cells:
        by_family[c.family] = by_family.get(c.family, 0.0) + c.value.get(key, 0.0)
    if not by_family:
        return 0.0
    worst = max(by_family, key=lambda f: by_family[f])
    return point([c for c in cells if c.family != worst], key)


@dataclass
class LipVerdict:
    verdict: str
    why: str
    rows: list[dict[str, Any]]
    final: bool

    def as_dict(self) -> dict[str, Any]:
        if not self.final:
            # interims carry health only: no dollar figure leaves before the final look
            return {"verdict": "INTERIM_NOT_A_DECISION", "why": self.why}
        return {"verdict": self.verdict, "why": self.why, "rows": self.rows}


def decide(cells: list[Cell], *, policies: list[str], counted_days: int, final: bool,
           versions: set[str], gates_closed: bool, isolation_ok: bool,
           params: SimParams = SIM) -> LipVerdict:
    """The section 7 decision table, evaluated once."""
    if not final:
        return LipVerdict("INTERIM_NOT_A_DECISION", "an interim decides nothing", [], False)
    families = {c.family for c in cells if c.hours > 0}
    g = min(counted_days, len(families))
    pre = []
    if counted_days < params.min_counted_days:
        pre.append(f"{counted_days} counted days < {params.min_counted_days}")
    if len(families) < params.min_families:
        pre.append(f"{len(families)} families < {params.min_families}")
    if len(versions) != 1:
        pre.append(f"versions {sorted(versions)} are not one")
    if not gates_closed:
        pre.append("gates G1-G6 were not all closed before day 1")
    if not isolation_ok:
        pre.append("maker isolation breached")
    series = [(p, c, e) for p in policies for c in COMBOS for e in ENVELOPES]
    boot = pigeonhole_bootstrap(cells, series, draws=params.bootstrap_draws, seed=params.seed)
    se = {s: float(np.nanstd(boot[i], ddof=1)) if np.isfinite(boot[i]).sum() > 1 else float("nan")
          for i, s in enumerate(series)}
    rows = []
    positive_policies, all_negative = [], True
    for p in policies:
        p_positive = True
        for c in COMBOS:
            low, high = (p, c, "LOW"), (p, c, "HIGH")
            th_lo, th_hi = point(cells, low), point(cells, high)
            lo_ci = t_interval(th_lo, se[low], g, params.alpha_positive)
            hi_ci = t_interval(th_hi, se[high], g, params.alpha_negative)
            lofo_lo = lofo(cells, low)
            rows.append({"policy": p, "combo": list(c), "theta_low": th_lo, "theta_high": th_hi,
                         "low_ci_98_75": list(lo_ci), "high_ci_95": list(hi_ci), "lofo_low": lofo_lo})
            if not (lo_ci[0] > 0 and lofo_lo > 0):
                p_positive = False
            if not hi_ci[1] < 0:
                all_negative = False
        if p_positive:
            positive_policies.append(p)
    if pre:
        return LipVerdict("INSUFFICIENT_EVIDENCE", "; ".join(pre), rows, True)
    if positive_policies:
        return LipVerdict("MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY",
                          "LOW lower bound > 0 and leave-one-family-out > 0 under all 12 readings for "
                          + ", ".join(positive_policies), rows, True)
    if all_negative:
        return LipVerdict("CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION",
                          "every policy's HIGH upper bound < 0 under all 12 readings", rows, True)
    return LipVerdict("INSUFFICIENT_EVIDENCE", "intervals straddle zero", rows, True)
