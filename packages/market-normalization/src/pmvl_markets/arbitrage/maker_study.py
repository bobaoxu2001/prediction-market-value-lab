"""Pooling many sessions without pretending they are many independent observations.

One session answered the question once, and the interval around that answer straddled
zero. The fix is more sessions, not more quotes: quotes inside a session share a book,
a spread regime and whatever moved the market that afternoon, so a thousand more of them
narrow the interval around a number that has not become better known.

So the unit resampled here is the **session**, and a second interval resamples the
**calendar date**, because sessions on one date share that date's conditions and the
session-level interval does not price that. Both are reported. When the study lands on
fewer dates than sessions - which it will, because twenty half-hours do not span twenty
days - the date-level interval is the binding one and the session-level one is labelled
as the optimistic bound.

The multiplicity correction is here for the same reason. Four policies is a family; the
best of four looks good at four too, and a family-wise claim needs the wider interval.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from pmvl_shared.money import ZERO, quantize_usd

from .maker_experiment import (
    MARKOUT_HORIZONS,
    FillEstimator,
    MakerExperimentReport,
    QuotePolicy,
)

__all__ = [
    "BOOTSTRAP_ITERATIONS",
    "BOOTSTRAP_SEED",
    "FAMILY_SIZE",
    "SessionResult",
    "PooledPolicyResult",
    "StudyReport",
    "pool_sessions",
]

#: Fixed in the pre-registration, before the first session.
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20260919
#: Four declared policies are one family; the family-wise interval is Bonferroni'd.
FAMILY_SIZE = 4
PRIMARY_HORIZON = 60


@dataclass(frozen=True)
class SessionResult:
    """One session's experiment, tagged with when it happened."""

    session: int
    date: str
    started_at: str
    report: MakerExperimentReport

    def quote_pnls(self, estimator: FillEstimator, horizon: int) -> list[Decimal]:
        return [o.pnl(estimator, horizon=horizon) for o in self.report.outcomes]


def _interval(
    samples: Sequence[Decimal], alpha: float
) -> tuple[Decimal, Decimal] | None:
    if not samples:
        return None
    ordered = sorted(samples)
    lo = ordered[int((alpha / 2) * (len(ordered) - 1))]
    hi = ordered[int((1 - alpha / 2) * (len(ordered) - 1))]
    return quantize_usd(lo), quantize_usd(hi)


def _cluster_bootstrap(
    clusters: Sequence[Sequence[Decimal]], *, iterations: int, seed: int
) -> list[Decimal]:
    """Means from resampling whole clusters with replacement.

    Empty clusters are kept in the draw. A session that placed no quotes is still a
    session, and dropping it would quietly raise the effective cluster count.
    """
    if len(clusters) < 2:
        return []
    rng = random.Random(seed)
    means: list[Decimal] = []
    for _ in range(iterations):
        drawn: list[Decimal] = []
        for _ in range(len(clusters)):
            drawn.extend(rng.choice(clusters))
        if drawn:
            means.append(sum(drawn, ZERO) / Decimal(len(drawn)))
    return means


@dataclass
class PooledPolicyResult:
    policy: QuotePolicy
    sessions: int
    dates: int
    quotes: int
    fills: dict[str, int]
    mean_pnl_per_quote: Decimal
    session_ci95: tuple[Decimal, Decimal] | None
    session_ci_family: tuple[Decimal, Decimal] | None
    date_ci95: tuple[Decimal, Decimal] | None
    date_ci_family: tuple[Decimal, Decimal] | None
    per_session_mean: list[tuple[int, str, Decimal, int]]
    hedged_pair_pnl: Decimal | None
    mean_markout: Decimal | None

    @property
    def verdict_positive(self) -> bool:
        """Family-wise lower bound above zero, on the binding (date-level) interval."""
        binding = self.date_ci_family or self.session_ci_family
        return binding is not None and binding[0] > ZERO

    @property
    def verdict_negative(self) -> bool:
        """Per-policy 95% upper bound below zero, on the binding interval."""
        binding = self.date_ci95 or self.session_ci95
        return binding is not None and binding[1] < ZERO

    def as_dict(self) -> dict[str, Any]:
        def ci(v):
            return None if v is None else [str(v[0]), str(v[1])]

        return {
            "policy": self.policy.as_dict(),
            "sessions": self.sessions,
            "dates": self.dates,
            "quotes_placed": self.quotes,
            "fills": self.fills,
            "mean_pnl_per_quote_usd": str(self.mean_pnl_per_quote),
            "session_cluster_ci95": ci(self.session_ci95),
            "session_cluster_ci_family_wise": ci(self.session_ci_family),
            "date_cluster_ci95": ci(self.date_ci95),
            "date_cluster_ci_family_wise": ci(self.date_ci_family),
            "binding_interval": "date" if self.date_ci95 else "session",
            "hedged_pair_pnl_per_contract": (
                None if self.hedged_pair_pnl is None else str(self.hedged_pair_pnl)
            ),
            "mean_markout_given_fill_60s": (
                None if self.mean_markout is None else str(self.mean_markout)
            ),
            "per_session": [
                {"session": s, "date": d, "mean_pnl_per_quote": str(m), "quotes": q}
                for s, d, m, q in self.per_session_mean
            ],
        }


@dataclass
class StudyReport:
    planned_sessions: int
    completed_sessions: int
    dates: list[str]
    estimator: str
    horizon: int
    results: list[PooledPolicyResult] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.completed_sessions >= self.planned_sessions

    def verdict(self) -> tuple[str, str]:
        """(verdict, why). Only decided at the planned session count."""
        if not self.complete:
            return (
                "INTERIM_NOT_A_DECISION",
                f"{self.completed_sessions} of {self.planned_sessions} sessions "
                "recorded; the pre-registration decides once, at the planned count",
            )
        positive = [r for r in self.results if r.verdict_positive]
        if positive:
            names = ", ".join(r.policy.name for r in positive)
            return (
                "MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY",
                f"family-wise lower bound above zero for {names}",
            )
        if self.results and all(r.verdict_negative for r in self.results):
            return (
                "CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION",
                "every declared policy's 95% upper bound is below zero on the binding "
                "interval",
            )
        return (
            "INSUFFICIENT_EVIDENCE",
            "intervals still straddle zero at the planned session count",
        )

    def as_dict(self) -> dict[str, Any]:
        verdict, why = self.verdict()
        return {
            "verdict": verdict,
            "why": why,
            "planned_sessions": self.planned_sessions,
            "completed_sessions": self.completed_sessions,
            "distinct_dates": len(self.dates),
            "dates": self.dates,
            "estimator": self.estimator,
            "markout_horizon_seconds": self.horizon,
            "bootstrap": {
                "iterations": BOOTSTRAP_ITERATIONS,
                "seed": BOOTSTRAP_SEED,
                "unit": "session, and separately calendar date",
                "family_size": FAMILY_SIZE,
                "family_wise_confidence": f"{100 * (1 - 0.05 / FAMILY_SIZE):.2f}%",
            },
            "independence_note": (
                f"{self.completed_sessions} sessions across {len(self.dates)} calendar "
                "date(s). Sessions sharing a date share that date's conditions, so the "
                "date-level interval binds and the session-level one is the optimistic "
                "bound"
            ),
            "policies": [r.as_dict() for r in self.results],
        }


def pool_sessions(
    sessions: Sequence[SessionResult],
    *,
    planned_sessions: int,
    estimator: FillEstimator = FillEstimator.QUEUE_MODEL,
    horizon: int = PRIMARY_HORIZON,
) -> StudyReport:
    """Pool every session's quotes, resampling sessions and dates rather than quotes."""
    dates = sorted({s.date for s in sessions})
    by_policy: dict[str, list[SessionResult]] = {}
    for session in sessions:
        by_policy.setdefault(session.report.policy.name, []).append(session)

    report = StudyReport(
        planned_sessions=planned_sessions,
        completed_sessions=len({s.session for s in sessions}),
        dates=dates,
        estimator=estimator.value,
        horizon=horizon,
    )

    family_alpha = 0.05 / FAMILY_SIZE
    for _policy_name, group in sorted(by_policy.items()):
        session_clusters = [s.quote_pnls(estimator, horizon) for s in group]
        date_map: dict[str, list[Decimal]] = {}
        for s in group:
            date_map.setdefault(s.date, []).extend(s.quote_pnls(estimator, horizon))
        date_clusters = [date_map[d] for d in sorted(date_map)]

        all_pnl = [v for c in session_clusters for v in c]
        mean = (
            quantize_usd(sum(all_pnl, ZERO) / Decimal(len(all_pnl))) if all_pnl else ZERO
        )

        session_means = _cluster_bootstrap(
            session_clusters, iterations=BOOTSTRAP_ITERATIONS, seed=BOOTSTRAP_SEED
        )
        date_means = _cluster_bootstrap(
            date_clusters, iterations=BOOTSTRAP_ITERATIONS, seed=BOOTSTRAP_SEED
        )

        fills = {
            est.value: sum(s.report.fills(est) for s in group) for est in FillEstimator
        }
        hedged = [v for s in group for v in _hedged_values(s.report, estimator)]
        markouts = [
            m
            for s in group
            for m in _markout_values(s.report, estimator, horizon)
        ]

        report.results.append(
            PooledPolicyResult(
                policy=group[0].report.policy,
                sessions=len(group),
                dates=len(date_clusters),
                quotes=sum(s.report.quotes_placed for s in group),
                fills=fills,
                mean_pnl_per_quote=mean,
                session_ci95=_interval(session_means, 0.05),
                session_ci_family=_interval(session_means, family_alpha),
                date_ci95=_interval(date_means, 0.05),
                date_ci_family=_interval(date_means, family_alpha),
                per_session_mean=[
                    (
                        s.session,
                        s.date,
                        quantize_usd(
                            sum(s.quote_pnls(estimator, horizon), ZERO)
                            / Decimal(len(s.quote_pnls(estimator, horizon)))
                        )
                        if s.report.quotes_placed
                        else ZERO,
                        s.report.quotes_placed,
                    )
                    for s in sorted(group, key=lambda x: x.session)
                ],
                hedged_pair_pnl=(
                    quantize_usd(sum(hedged, ZERO) / Decimal(len(hedged)))
                    if hedged
                    else None
                ),
                mean_markout=(
                    quantize_usd(sum(markouts, ZERO) / Decimal(len(markouts)))
                    if markouts
                    else None
                ),
            )
        )
    return report


def _hedged_values(
    report: MakerExperimentReport, estimator: FillEstimator
) -> list[Decimal]:
    """Per-contract PnL of buying the fill and closing it into the other side.

    An identity, not an estimate: the pair settles for exactly $1, so this is
    ``1 - price - hedge cost`` on every fill that had a hedge quoted.
    """
    from pmvl_shared.money import ONE

    return [
        ONE - o.quote.price - o.hedge_cost
        for o in report.outcomes
        if o.filled(estimator) and o.hedge_cost is not None
    ]


def _markout_values(
    report: MakerExperimentReport, estimator: FillEstimator, horizon: int
) -> list[Decimal]:
    return [
        o.markout[horizon]
        for o in report.outcomes
        if o.filled(estimator) and o.markout.get(horizon) is not None
    ]
