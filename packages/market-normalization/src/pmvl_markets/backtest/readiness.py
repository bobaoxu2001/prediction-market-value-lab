"""How far along the track record is, stated as a distance rather than a promise.

The platform's central claim - that its estimates beat the market's own price - is
not answerable yet, and will not be until the pipeline has run continuously for
weeks and the recommendations it published have resolved. Everything downstream of
the snapshot is built and tested; it has simply never had real settled history to
read.

"We do not have a track record yet" is true and unhelpful. This module turns it
into a measurement: how many settled recommendations exist, how many are still
pending, how fast they are accruing, what each downstream claim needs, and the
date each becomes answerable at the current rate.

Two things it is careful about.

**Accrual is only real if something is running.** A recommendation count that has
not moved in a week means the scheduler stopped, not that the market went quiet,
and those look identical in a bare total. So the job-run history is part of the
report, and a stalled pipeline is reported as stalled rather than as slow progress.

**Projections are arithmetic, not forecasts.** The projected dates are the current
rate extrapolated, nothing more. They are labelled that way, and they are omitted
entirely rather than guessed at when there is no rate to extrapolate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmvl_shared.db.models import JobRun, RecommendationSnapshot
from pmvl_shared.enums import DataProvenance
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import ensure_utc, iso, utcnow

from .calibration import MIN_FIT_SAMPLES

log = get_logger(__name__)

#: Settled recommendations before a Brier-versus-market figure is worth publishing.
#:
#: Lower than the calibration floor because the two claims are different sizes.
#: Quoting a Brier comparison with an honest confidence caveat needs far less data
#: than *fitting a whole correction function* and applying it silently to every
#: future estimate.
MIN_SETTLED_FOR_BRIER = 60

#: Beyond this with no successful run, the pipeline is stalled, not slow.
STALL_THRESHOLD = timedelta(hours=24)


@dataclass
class Milestone:
    """One downstream claim, and what it is still waiting for."""

    name: str
    needs: int
    have: int
    description: str

    @property
    def met(self) -> bool:
        return self.have >= self.needs

    @property
    def remaining(self) -> int:
        return max(0, self.needs - self.have)

    def as_dict(self, *, per_day: float | None, now: datetime) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "needs": self.needs,
            "have": self.have,
            "remaining": self.remaining,
            "met": self.met,
        }
        if not self.met:
            # Only project when there is a rate. A zero or unknown accrual rate
            # gives a date of "never" or a division by zero, and printing either
            # as an estimate would be worse than printing nothing.
            if per_day and per_day > 0:
                days = self.remaining / per_day
                payload["projected_days"] = round(days, 1)
                payload["projected_date"] = iso(now + timedelta(days=days))
                payload["projection_basis"] = (
                    "current accrual rate extrapolated; arithmetic, not a forecast"
                )
            else:
                payload["projected_date"] = None
                payload["projection_basis"] = (
                    "nothing is accruing, so there is no rate to extrapolate"
                )
        return payload


@dataclass
class TrackRecordReadiness:
    settled: int = 0
    pending: int = 0
    published_total: int = 0
    first_snapshot: datetime | None = None
    last_snapshot: datetime | None = None
    days_of_history: float = 0.0
    settled_per_day: float | None = None
    last_successful_run: datetime | None = None
    stalled: bool = True
    milestones: list[Milestone] = field(default_factory=list)
    generated_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        now = self.generated_at or utcnow()
        rate = None if self.stalled else self.settled_per_day
        return {
            "settled_recommendations": self.settled,
            "pending_recommendations": self.pending,
            "published_total": self.published_total,
            "first_snapshot": iso(self.first_snapshot),
            "last_snapshot": iso(self.last_snapshot),
            "days_of_history": round(self.days_of_history, 2),
            "settled_per_day": (
                round(self.settled_per_day, 2) if self.settled_per_day else None
            ),
            "last_successful_run": iso(self.last_successful_run),
            "pipeline_stalled": self.stalled,
            "milestones": [m.as_dict(per_day=rate, now=now) for m in self.milestones],
            "summary": self.summary(),
            "generated_at": iso(now),
        }

    def summary(self) -> str:
        if self.published_total == 0:
            return (
                "No live recommendations have been published yet. The track record "
                "starts accruing once `pmvl schedule` runs continuously."
            )
        if self.stalled:
            return (
                f"{self.settled} settled of {self.published_total} published, but "
                "no job has succeeded in over a day - the pipeline is stalled, so "
                "nothing is accruing."
            )
        unmet = [m for m in self.milestones if not m.met]
        if not unmet:
            return (
                f"{self.settled} settled recommendations over "
                f"{self.days_of_history:.0f} days. Every downstream claim now has "
                "enough data behind it."
            )
        nearest = min(unmet, key=lambda m: m.remaining)
        return (
            f"{self.settled} settled recommendations over "
            f"{self.days_of_history:.0f} days, accruing about "
            f"{self.settled_per_day:.1f}/day. Nearest milestone: {nearest.name} "
            f"needs {nearest.remaining} more."
        )


def track_record_readiness(
    session: Session, *, now: datetime | None = None
) -> TrackRecordReadiness:
    """Measure the distance between where the track record is and where it works."""
    now = now or utcnow()
    report = TrackRecordReadiness(generated_at=now)

    live = RecommendationSnapshot.provenance == DataProvenance.LIVE
    report.published_total = (
        session.scalar(select(func.count()).select_from(RecommendationSnapshot).where(live))
        or 0
    )
    report.settled = (
        session.scalar(
            select(func.count())
            .select_from(RecommendationSnapshot)
            .where(live, RecommendationSnapshot.final_result.is_not(None))
        )
        or 0
    )
    report.pending = report.published_total - report.settled

    bounds = session.execute(
        select(
            func.min(RecommendationSnapshot.snapshot_date),
            func.max(RecommendationSnapshot.snapshot_date),
        ).where(live)
    ).one_or_none()
    if bounds and bounds[0] is not None:
        report.first_snapshot = _as_datetime(bounds[0])
        report.last_snapshot = _as_datetime(bounds[1])
        if report.first_snapshot and report.last_snapshot:
            span = (report.last_snapshot - report.first_snapshot).total_seconds()
            # A single day of history is one day, not zero - otherwise the first
            # day's rate is a division by zero and reads as "nothing is happening".
            report.days_of_history = max(span / 86400.0, 1.0)
            report.settled_per_day = report.settled / report.days_of_history

    report.last_successful_run = _last_successful_run(session)
    report.stalled = (
        report.last_successful_run is None
        or (now - report.last_successful_run) > STALL_THRESHOLD
    )

    report.milestones = [
        Milestone(
            name="brier_vs_market",
            needs=MIN_SETTLED_FOR_BRIER,
            have=report.settled,
            description=(
                "Publish a Brier-versus-market figure from live settled "
                "recommendations rather than from retrodiction"
            ),
        ),
        Milestone(
            name="calibration_fit",
            needs=MIN_FIT_SAMPLES,
            have=report.settled,
            description=(
                "Fit a calibration map walk-forward without memorising noise"
            ),
        ),
    ]
    return report


def _last_successful_run(session: Session) -> datetime | None:
    from pmvl_shared.enums import JobStatus

    value = session.scalar(
        select(func.max(JobRun.finished_at)).where(
            JobRun.status.in_([JobStatus.SUCCESS, JobStatus.PARTIAL_SUCCESS])
        )
    )
    return ensure_utc(value)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    # `snapshot_date` is a Date column, which SQLite hands back as a date.
    return ensure_utc(datetime(value.year, value.month, value.day))
