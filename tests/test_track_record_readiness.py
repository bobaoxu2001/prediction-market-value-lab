"""Track-record accrual reporting: distance to each claim, and stall detection."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.db.models import JobRun, RecommendationSnapshot
from pmvl_shared.enums import DataProvenance, JobStatus

from pmvl_markets.backtest.calibration import MIN_FIT_SAMPLES
from pmvl_markets.backtest.readiness import (
    MIN_SETTLED_FOR_BRIER,
    STALL_THRESHOLD,
    track_record_readiness,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def add_snapshots(
    session,
    *,
    n: int,
    settled: int,
    first_day: date,
    last_day: date,
    provenance: str = DataProvenance.LIVE,
) -> None:
    for i in range(n):
        day = first_day if i < n // 2 else last_day
        session.add(
            RecommendationSnapshot(
                batch_id=f"batch-{i}",
                snapshot_date=day,
                recommendation_created_at=datetime(
                    day.year, day.month, day.day, tzinfo=UTC
                ),
                market_id=i,
                platform="kalshi",
                platform_market_id=f"M-{i}",
                market_title="test",
                horizon="24h",
                rank=i,
                side="yes",
                entry_price_at_recommendation=Decimal("0.5"),
                total_cost_at_recommendation=Decimal("0.52"),
                fair_probability=Decimal("0.6"),
                fair_probability_low=Decimal("0.55"),
                fair_probability_high=Decimal("0.65"),
                expected_value=Decimal("0.08"),
                conservative_net_ev=Decimal("0.03"),
                final_result="yes" if i < settled else None,
                provenance=provenance,
            )
        )
    session.flush()


def add_successful_run(session, *, finished_at: datetime) -> None:
    session.add(
        JobRun(
            job_name="rank",
            status=JobStatus.SUCCESS,
            started_at=finished_at - timedelta(minutes=1),
            finished_at=finished_at,
        )
    )
    session.flush()


def test_empty_database_reports_nothing_accrued(db):
    report = track_record_readiness(db, now=NOW)

    assert report.published_total == 0
    assert report.settled == 0
    assert report.stalled is True
    assert "starts accruing" in report.summary()


def test_milestones_describe_what_each_claim_needs(db):
    report = track_record_readiness(db, now=NOW)
    by_name = {m.name: m for m in report.milestones}

    assert by_name["brier_vs_market"].needs == MIN_SETTLED_FOR_BRIER
    assert by_name["calibration_fit"].needs == MIN_FIT_SAMPLES
    assert all(not m.met for m in report.milestones)


def test_counts_settled_and_pending_separately(db):
    add_snapshots(
        db, n=40, settled=25,
        first_day=date(2026, 7, 28), last_day=date(2026, 8, 6),
    )
    add_successful_run(db, finished_at=NOW - timedelta(minutes=5))

    report = track_record_readiness(db, now=NOW)

    assert report.published_total == 40
    assert report.settled == 25
    assert report.pending == 15


def test_demo_rows_never_count_toward_the_track_record(db):
    """The demo forecaster is synthetic and deliberately imperfect."""
    add_snapshots(
        db, n=50, settled=50,
        first_day=date(2026, 7, 28), last_day=date(2026, 8, 6),
        provenance=DataProvenance.DEMO,
    )

    report = track_record_readiness(db, now=NOW)

    assert report.published_total == 0
    assert report.settled == 0


def test_a_stalled_pipeline_is_reported_as_stalled_not_as_slow(db):
    """A count that has stopped moving and a quiet market look identical raw."""
    add_snapshots(
        db, n=40, settled=25,
        first_day=date(2026, 7, 28), last_day=date(2026, 8, 6),
    )
    add_successful_run(db, finished_at=NOW - STALL_THRESHOLD - timedelta(hours=1))

    report = track_record_readiness(db, now=NOW)

    assert report.stalled is True
    assert "stalled" in report.summary()


def test_a_stalled_pipeline_projects_no_completion_date(db):
    """Extrapolating a rate that has stopped would invent a date."""
    add_snapshots(
        db, n=40, settled=25,
        first_day=date(2026, 7, 28), last_day=date(2026, 8, 6),
    )
    add_successful_run(db, finished_at=NOW - timedelta(days=5))

    payload = track_record_readiness(db, now=NOW).as_dict()
    brier = next(m for m in payload["milestones"] if m["name"] == "brier_vs_market")

    assert brier["projected_date"] is None
    assert "no rate to extrapolate" in brier["projection_basis"]


def test_a_running_pipeline_projects_from_the_current_rate(db):
    add_snapshots(
        db, n=60, settled=30,
        first_day=date(2026, 7, 28), last_day=date(2026, 8, 6),
    )
    add_successful_run(db, finished_at=NOW - timedelta(minutes=10))

    payload = track_record_readiness(db, now=NOW).as_dict()
    brier = next(m for m in payload["milestones"] if m["name"] == "brier_vs_market")

    assert payload["pipeline_stalled"] is False
    assert brier["projected_date"] is not None
    assert "not a forecast" in brier["projection_basis"]


def test_a_single_day_of_history_does_not_divide_by_zero(db):
    add_snapshots(
        db, n=10, settled=5,
        first_day=date(2026, 8, 6), last_day=date(2026, 8, 6),
    )
    add_successful_run(db, finished_at=NOW - timedelta(minutes=5))

    report = track_record_readiness(db, now=NOW)

    assert report.days_of_history == 1.0
    assert report.settled_per_day == pytest.approx(5.0)


def test_met_milestones_are_marked_met(db):
    add_snapshots(
        db, n=MIN_FIT_SAMPLES + 20, settled=MIN_FIT_SAMPLES + 10,
        first_day=date(2026, 6, 1), last_day=date(2026, 8, 6),
    )
    add_successful_run(db, finished_at=NOW - timedelta(minutes=5))

    report = track_record_readiness(db, now=NOW)

    assert all(m.met for m in report.milestones)
    assert "Every downstream claim" in report.summary()
