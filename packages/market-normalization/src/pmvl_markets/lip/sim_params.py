"""Every simulation and statistics constant of the LIP study (pre-registration 3-7).

Pinned by ``LIP_SIM_VERSION``. Recording constants live in ``params``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SIM", "SimParams"]


@dataclass(frozen=True)
class SimParams:
    # --- policy mechanics (3) ----------------------------------------------------
    #: books at the start of a segment that must all pass the gate
    gate_books: int = 10
    #: improve only when others' spread is at least this many steps (at 2, two
    #: improved sides would lock)
    improve_min_spread: int = 3
    min_spread: int = 1
    close_buffer_s: float = 1_800.0
    dead_man_s: float = 10.0
    replenish_every_s: float = 5.0
    #: latency floors; LOW also takes the triggering book's own round trip if larger
    latency_low_floor_s: float = 0.410
    latency_high_s: float = 0.300
    #: documented Basic-tier write budget (Amendment 1, G2); HIGH is unlimited
    writes_per_s_low: float = 10.0

    # --- credit (5) ---------------------------------------------------------------
    #: bracketing books may span at most 2 * cadence + 1.5 s
    cadence_s: float = 1.0
    span_slack_s: float = 1.5

    # --- valuation (6) ------------------------------------------------------------
    mark_horizon_s: float = 3_600.0
    mark_search_s: float = 300.0
    maker_rate_unknown_fee_low: float = 0.0175

    # --- statistics (7) -----------------------------------------------------------
    bootstrap_draws: int = 10_000
    seed: int = 20260924
    policies: int = 4
    alpha_positive: float = 0.0125
    alpha_negative: float = 0.05
    min_counted_days: int = 20
    min_families: int = 10
    max_calendar_days: int = 30
    verdict_lag_days: int = 14

    @property
    def span_limit_s(self) -> float:
        return 2 * self.cadence_s + self.span_slack_s


SIM = SimParams()
