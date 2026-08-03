"""The weekly outcome review.

The daily digest asks "is there anything worth doing?". This asks the harder
question: "was last week's research any good?" - and it has to be answerable on a
week when the honest answer to the daily question was "no" every single day.

So it reports two different things and never conflates them:

1. **Recommendations published and how they resolved.** The direct scorecard. On
   a week with no published recommendations this is empty, and an empty scorecard
   is reported as empty rather than padded.

2. **Forecast accuracy on everything that settled.** Every market the model
   scored which resolved during the window, whether or not it was ever
   recommended. This is the part that keeps working when the scorecard is empty,
   and it is the part that can embarrass us: a model that is systematically wrong
   shows up here even in a week when it recommended nothing.

The second is split by whether the estimate had an independent prior. Scoring a
market-derived estimate against the market is measuring an echo, and averaging
the two together would flatter the model by burying the hard cases in the easy
ones.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pmvl_shared.timeutil import ensure_utc

from .digest import _dec, _time, open_snapshot
from .gate import GateResult


@dataclass
class BrierBucket:
    """Forecast accuracy for one class of estimate. Lower is better."""

    label: str
    n: int
    model_brier: float | None
    market_brier: float | None
    #: Positive means the model was more accurate than the market on this set.
    improvement: float | None
    note: str = ""


@dataclass
class ResolvedRecommendation:
    title: str
    platform: str
    side: str
    published_at: datetime | None
    settled_at: datetime | None
    result: str
    entry_all_in: Decimal | None
    realized_profit_per_contract: Decimal | None
    won: bool | None


@dataclass
class OutcomeReview:
    kind: str  # "weekly"
    generated_at: datetime
    gate: GateResult
    window_start: datetime
    window_end: datetime

    recommendations_published: int = 0
    resolved: list[ResolvedRecommendation] = field(default_factory=list)
    markets_settled_in_window: int = 0
    accuracy: list[BrierBucket] = field(default_factory=list)
    #: Statements about what this week's data cannot support. Always populated:
    #: a review with no caveats is a review that has stopped thinking.
    limitations: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        if self.recommendations_published:
            wins = sum(1 for r in self.resolved if r.won)
            return (
                f"{self.recommendations_published} recommendation"
                f"{'s' if self.recommendations_published != 1 else ''} published, "
                f"{len(self.resolved)} resolved, {wins} won"
            )
        if self.markets_settled_in_window:
            return (
                "No recommendations were published this week — "
                f"{self.markets_settled_in_window} scored markets settled and were graded anyway"
            )
        return "No recommendations published and nothing settled in this window"


def _brier(pairs: list[tuple[float, float]]) -> float | None:
    """Mean squared error of a probability forecast. ``pairs`` is (forecast, outcome)."""
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def _accuracy_buckets(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> tuple[list[BrierBucket], int]:
    """Grade every scored market that settled inside the window."""
    rows = conn.execute(
        """
        select s.result, s.settled_at,
               mp.fair_probability_mean, mp.market_implied_probability,
               mp.has_independent_prior
        from settlements s
        join markets m on m.id = s.market_id
        join model_predictions mp on mp.market_id = m.id and mp.provenance = 'live'
        where s.provenance = 'live' and s.result in ('yes', 'no')
        """
    ).fetchall()

    independent: list[tuple[float, float]] = []
    independent_market: list[tuple[float, float]] = []
    derived: list[tuple[float, float]] = []
    derived_market: list[tuple[float, float]] = []
    settled_in_window = 0

    for row in rows:
        settled_at = _time(row["settled_at"])
        if settled_at is None or not (start <= settled_at <= end):
            continue
        settled_in_window += 1

        outcome = 1.0 if row["result"] == "yes" else 0.0
        model_p = _dec(row["fair_probability_mean"])
        market_p = _dec(row["market_implied_probability"])
        if model_p is None:
            continue

        if row["has_independent_prior"]:
            independent.append((float(model_p), outcome))
            if market_p is not None:
                independent_market.append((float(market_p), outcome))
        else:
            derived.append((float(model_p), outcome))
            if market_p is not None:
                derived_market.append((float(market_p), outcome))

    def bucket(label: str, model: list, market: list, note: str) -> BrierBucket:
        mb, kb = _brier(model), _brier(market)
        return BrierBucket(
            label=label,
            n=len(model),
            model_brier=mb,
            market_brier=kb,
            improvement=(None if mb is None or kb is None else kb - mb),
            note=note,
        )

    buckets = [
        bucket(
            "Estimates with an independent prior",
            independent,
            independent_market,
            "The only set where beating the market means anything: these estimates "
            "never saw the target market's own price.",
        ),
        bucket(
            "Estimates derived from the market price",
            derived,
            derived_market,
            "Scored for completeness only. An estimate built from the market price "
            "cannot meaningfully beat that price, and a good score here is not evidence "
            "of skill.",
        ),
    ]
    return [b for b in buckets if b.n > 0], settled_in_window


def _resolved_recommendations(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> tuple[list[ResolvedRecommendation], int]:
    """Published recommendations and how they turned out.

    Reads `recommendation_snapshots`, the immutable record frozen at publication,
    rather than `recommendations`, which is mutated as state changes. A scorecard
    built from mutable rows would be a scorecard that can be edited after the fact.
    """
    published = conn.execute(
        "select count(*) from recommendation_snapshots where provenance = 'live' "
        "and recommendation_created_at between ? and ?",
        (start.isoformat(sep=" "), end.isoformat(sep=" ")),
    ).fetchone()[0]

    rows = conn.execute(
        """
        select market_title, platform, side, recommendation_created_at, settled_at,
               final_result, total_cost_at_recommendation, realized_profit_per_contract
        from recommendation_snapshots
        where provenance = 'live' and settled_at is not null
        order by settled_at desc
        """
    ).fetchall()

    resolved: list[ResolvedRecommendation] = []
    for row in rows:
        settled_at = _time(row["settled_at"])
        if settled_at is None or not (start <= settled_at <= end):
            continue
        realized = _dec(row["realized_profit_per_contract"])
        resolved.append(
            ResolvedRecommendation(
                title=row["market_title"] or "",
                platform=row["platform"] or "",
                side=row["side"] or "",
                published_at=_time(row["recommendation_created_at"]),
                settled_at=settled_at,
                result=row["final_result"] or "",
                entry_all_in=_dec(row["total_cost_at_recommendation"]),
                realized_profit_per_contract=realized,
                won=None if realized is None else realized > 0,
            )
        )
    return resolved, published


def build_weekly_review(
    manifest_path: Path,
    gate_result: GateResult,
    *,
    window_days: int = 7,
) -> OutcomeReview:
    """Assemble the weekly review.

    Unlike the daily digest this runs even when the gate refused. A retrospective
    over already-settled outcomes does not depend on the data being fresh enough
    to trade against - the events have already happened. The gate result travels
    with the report so the reader can see the Snapshot's state either way.
    """
    end = ensure_utc(gate_result.source_data_cutoff or gate_result.as_of) or gate_result.as_of
    start = end - timedelta(days=window_days)

    conn = open_snapshot(manifest_path)
    try:
        resolved, published = _resolved_recommendations(conn, start, end)
        accuracy, settled = _accuracy_buckets(conn, start, end)
    finally:
        conn.close()

    limitations: list[str] = []
    if not published:
        limitations.append(
            "No recommendations were published in this window, so there is no "
            "recommendation scorecard to report. That is the expected outcome on a week "
            "when nothing cleared the admission gate — it is not a data problem."
        )
    if not settled:
        limitations.append(
            "No scored market settled inside this window, so no accuracy figure can be "
            "computed from it."
        )
    for bucket in accuracy:
        if bucket.n < 30:
            limitations.append(
                f"'{bucket.label}' covers {bucket.n} settled market"
                f"{'s' if bucket.n != 1 else ''}. That is too small a sample to "
                "distinguish skill from luck; read it as a running tally, not a verdict."
            )
    limitations.append(
        "Forecast accuracy is not profitability, and profitability is not skill. A "
        "well-calibrated forecast can still lose money after costs, and a lucky week "
        "can flatter a poor model."
    )

    return OutcomeReview(
        kind="weekly",
        generated_at=gate_result.as_of,
        gate=gate_result,
        window_start=start,
        window_end=end,
        recommendations_published=published,
        resolved=resolved,
        markets_settled_in_window=settled,
        accuracy=accuracy,
        limitations=limitations,
    )
