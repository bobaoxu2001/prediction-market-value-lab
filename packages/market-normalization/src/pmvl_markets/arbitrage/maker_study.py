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
    "ReplicationReport",
    "compare_days",
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
    #: How much the sessions actually differ. A frozen universe plus a top-by-volume
    #: rule can hand consecutive sessions the *same* books an hour apart, in which
    #: case twenty sessions are closer to one long recording cut into twenty pieces
    #: than to twenty draws - and the session-level interval would be describing
    #: autocorrelation rather than independent variation. Measured rather than
    #: assumed, because the design cannot rule it out.
    distinct_markets: int = 0
    mean_consecutive_overlap: Decimal | None = None
    #: The shape of the per-fill markout, not just its mean. A mean dragged down by a
    #: handful of resolution jumps and a mean produced by a small persistent tilt on
    #: ordinary fills are the same number and completely different findings: the first
    #: says "avoid the tail", the second says the quote is priced wrong. Trimmed means
    #: separate them.
    markout_distribution: dict[str, Any] = field(default_factory=dict)

    @property
    def binding_level(self) -> str:
        """Which interval the decision actually used - named, never assumed.

        The pre-registration says the date-level interval binds. When the study lands
        on one date that interval does not exist, and the decision falls back to the
        session level. That fallback is real and defensible - it is still twenty
        draws of something - but it answers a narrower question, so it is named in
        the output rather than applied quietly. A verdict that cannot say which
        interval produced it is a verdict that reads stronger than it is.
        """
        return "date" if self.date_ci95 is not None else "session"

    @property
    def verdict_positive(self) -> bool:
        binding = self.date_ci_family or self.session_ci_family
        return binding is not None and binding[0] > ZERO

    @property
    def verdict_negative(self) -> bool:
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
            "binding_interval": self.binding_level,
            "distinct_markets_across_sessions": self.distinct_markets,
            "mean_consecutive_session_overlap": (
                None
                if self.mean_consecutive_overlap is None
                else str(self.mean_consecutive_overlap)
            ),
            "markout_distribution": self.markout_distribution,
            "overlap_note": (
                "Jaccard overlap between the market sets of consecutive sessions. "
                "Near 1 means the sessions recorded the same books at different times, "
                "so the session-level interval is measuring autocorrelation rather "
                "than independent variation and should be read as the optimistic bound "
                "even before the date-level one is considered"
            ),
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

    @property
    def date_factor_sampled(self) -> bool:
        """Were there enough dates to resample the day at all?

        One date is one draw. Everything the study says is then conditional on that
        day, however many sessions it holds - twenty half-hours of one afternoon
        cannot tell you what a different afternoon does.
        """
        return len(self.dates) >= 2

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
            level = self.results[0].binding_level
            scope = (
                f"across {len(self.dates)} calendar dates"
                if self.date_factor_sampled
                else (
                    f"on the single date {self.dates[0] if self.dates else 'recorded'}, "
                    "so the day factor is unsampled and nothing here extends to a "
                    "different day"
                )
            )
            return (
                "CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION",
                f"every declared policy's 95% upper bound is below zero on the "
                f"{level}-level interval, {scope}",
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
            "date_factor_sampled": self.date_factor_sampled,
            "generalisation_limit": (
                "the day factor is resampled"
                if self.date_factor_sampled
                else "one calendar date: every interval here is conditional on that "
                     "day, and the study does not speak to a different one"
            ),
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

        distribution = _markout_shape(markouts)

        ordered_group = sorted(group, key=lambda x: x.session)
        market_sets = [
            {o.quote.market for o in s.report.outcomes} for s in ordered_group
        ]
        overlaps = [
            Decimal(len(a & b)) / Decimal(len(a | b))
            for a, b in zip(market_sets, market_sets[1:])
            if a or b
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
                markout_distribution=distribution,
                distinct_markets=len(set().union(*market_sets)) if market_sets else 0,
                mean_consecutive_overlap=(
                    quantize_usd(sum(overlaps, ZERO) / Decimal(len(overlaps)))
                    if overlaps
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


def _markout_shape(values: Sequence[Decimal]) -> dict[str, Any]:
    """Percentiles, win/loss counts and trimmed means for the per-fill markout.

    The trimmed means are the point. If the middle 90% loses at the same rate as the
    whole sample, the maker is being adversely selected on ordinary fills rather than
    occasionally run over, and no amount of tail management fixes it.
    """
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)

    def at(q: float) -> str:
        return str(ordered[min(n - 1, int(q * (n - 1)))])

    def mean(xs: Sequence[Decimal]) -> str | None:
        return str(quantize_usd(sum(xs, ZERO) / Decimal(len(xs)))) if xs else None

    losing = sum(1 for v in ordered if v < 0)
    winning = sum(1 for v in ordered if v > 0)
    return {
        "fills_with_a_markout": n,
        "p01": at(0.01), "p05": at(0.05), "p25": at(0.25), "p50": at(0.50),
        "p75": at(0.75), "p95": at(0.95), "p99": at(0.99),
        "min": str(ordered[0]), "max": str(ordered[-1]),
        "mean": mean(ordered),
        "mean_excluding_worst_one_percent": mean(ordered[int(0.01 * n):]),
        "mean_of_middle_ninety_percent": mean(ordered[int(0.05 * n): int(0.95 * n)]),
        "losing_fills": losing,
        "winning_fills": winning,
        "flat_fills": n - losing - winning,
    }


def _markout_values(
    report: MakerExperimentReport, estimator: FillEstimator, horizon: int
) -> list[Decimal]:
    return [
        o.markout[horizon]
        for o in report.outcomes
        if o.filled(estimator) and o.markout.get(horizon) is not None
    ]


# ---------------------------------------------------------------- replication
@dataclass
class ReplicationReport:
    """Does a second day's result agree with the first day's, by a rule fixed first.

    Comparison rather than pooling, and not only because the two days were recorded
    under different recorder versions. Pooling answers "what is the average across
    these days", which two days cannot support. Comparison answers "did the finding
    hold up", which is the question a second day is actually able to settle.
    """

    day_one_label: str
    day_two_label: str
    per_policy: list[dict[str, Any]] = field(default_factory=list)

    @property
    def negative_on_day_two(self) -> int:
        return sum(1 for r in self.per_policy if r["day_two_mean_negative"])

    @property
    def contained(self) -> int:
        return sum(1 for r in self.per_policy if r["inside_day_one_interval"])

    @property
    def contradicting(self) -> list[str]:
        """Policies whose day-2 interval sits entirely above zero."""
        return [r["policy"] for r in self.per_policy if r["day_two_excludes_zero_above"]]

    def verdict(self) -> tuple[str, str]:
        n = len(self.per_policy)
        if not n:
            return "INCONCLUSIVE", "no policy could be compared"
        if self.contradicting:
            return (
                "FAILED_TO_REPLICATE",
                f"day two's interval lies entirely above zero for "
                f"{', '.join(self.contradicting)}",
            )
        if self.negative_on_day_two == n and self.contained >= 3:
            return (
                "REPLICATED",
                f"all {n} policies negative on day two, and {self.contained} of {n} "
                "day-two means fall inside day one's interval",
            )
        return (
            "INCONCLUSIVE",
            f"{self.negative_on_day_two} of {n} policies negative on day two, "
            f"{self.contained} of {n} means inside day one's interval - the "
            "pre-registered rule needs all negative and at least three contained",
        )

    def as_dict(self) -> dict[str, Any]:
        verdict, why = self.verdict()
        return {
            "verdict": verdict,
            "why": why,
            "day_one": self.day_one_label,
            "day_two": self.day_two_label,
            "rule": (
                "REPLICATED needs all four policies negative on day two and at least "
                "three of four day-two means inside day one's 95% session-level "
                "interval; FAILED_TO_REPLICATE needs any day-two interval entirely "
                "above zero; anything else is INCONCLUSIVE"
            ),
            "not_pooled_because": (
                "the two days were recorded under different recorder versions, and a "
                "1.0.0 skew bound is inflated relative to a 1.1.0 one while a 1.0.0 "
                "session's snapshot count depended on how busy the tape was"
            ),
            "policies": self.per_policy,
        }


def compare_days(
    day_one: StudyReport,
    day_two: StudyReport,
    *,
    day_one_label: str,
    day_two_label: str,
) -> ReplicationReport:
    """Apply the pre-registered comparison, policy by policy."""
    one = {r.policy.name: r for r in day_one.results}
    report = ReplicationReport(day_one_label=day_one_label, day_two_label=day_two_label)

    for result in sorted(day_two.results, key=lambda r: r.policy.name):
        prior = one.get(result.policy.name)
        if prior is None:
            continue
        interval = prior.session_ci95
        mean = result.mean_pnl_per_quote
        own = result.session_ci95
        report.per_policy.append(
            {
                "policy": result.policy.name,
                "day_one_mean": str(prior.mean_pnl_per_quote),
                "day_one_ci95": (
                    None if interval is None else [str(interval[0]), str(interval[1])]
                ),
                "day_two_mean": str(mean),
                "day_two_ci95": None if own is None else [str(own[0]), str(own[1])],
                "day_two_sessions": result.sessions,
                "day_two_quotes": result.quotes,
                "day_two_mean_negative": mean < ZERO,
                "inside_day_one_interval": (
                    interval is not None and interval[0] <= mean <= interval[1]
                ),
                #: The only outcome that would overturn the first study, kept as its
                #: own flag so it cannot be lost inside a summary count.
                "day_two_excludes_zero_above": own is not None and own[0] > ZERO,
                "day_two_excludes_zero_below": own is not None and own[1] < ZERO,
            }
        )
    return report
