"""A snapshot's timestamps must stay distinguishable from each other.

The PR body for this work described the deployment as "quotes captured
2026-07-27 02:31:40 UTC". That value was the newest *order book* row; the
newest market quote was 2026-07-28 08:07:03, over a day later; and neither is a
capture time for the dataset, because on that artefact 12 of 1850 markets were
observed on Jul 28 while 1516 were more than a day older.

Every timestamp here answers a different question. These tests fail if they are
ever collapsed back into one.
"""

from datetime import datetime, timedelta

import pytest

from pmvl_shared.enums import DataProvenance, MarketStatus, Platform

from pmvl_api.snapshot_timing import (
    QUOTE_SPREAD_NOTE,
    UNAVAILABLE_NOTES,
    snapshot_timing,
)


@pytest.fixture()
def timed_session(clean_db):  # noqa: ANN001
    """Markets observed across a range, plus ingest and arbitrage job runs."""
    from pmvl_markets.db_models import JobRun, Market

    base = datetime(2026, 7, 20, 0, 0, 0)
    # Deliberately uneven: one recent observation and a long stale tail, which is
    # the shape that makes a single "capture time" misleading.
    markets = []
    for offset in range(5):
        market = Market(
            platform=Platform.KALSHI.value,
            platform_market_id=f"TIMING-{offset}",
            title=f"Timing market {offset}",
            status=MarketStatus.OPEN.value,
            provenance=DataProvenance.LIVE.value,
            created_at=base,
            quote_observed_at=base + timedelta(days=0 if offset else 8),
        )
        clean_db.add(market)
        markets.append(market)
    clean_db.flush()

    clean_db.add_all(
        [
            JobRun(
                job_name="ingest",
                status="success",
                started_at=base + timedelta(days=8, hours=1),
                finished_at=base + timedelta(days=8, hours=1, minutes=2),
            ),
            JobRun(
                job_name="arbitrage",
                status="success",
                started_at=base + timedelta(days=8, hours=3),
                finished_at=base + timedelta(days=8, hours=3, seconds=2),
            ),
        ]
    )
    clean_db.flush()
    return clean_db, base, markets


def test_each_timestamp_is_reported_under_its_own_name(timed_session):
    session, base, _ = timed_session
    timing = snapshot_timing(session)

    assert timing["market_ingest_started_at"] == base + timedelta(days=8, hours=1)
    assert timing["market_ingest_finished_at"] == base + timedelta(
        days=8, hours=1, minutes=2
    )
    assert timing["arbitrage_scan_at"] == base + timedelta(days=8, hours=3)
    assert timing["freshest_quote_observed_at"] == base + timedelta(days=8)
    assert timing["oldest_quote_observed_at"] == base


def test_freshest_quote_is_not_the_ingest_time(timed_session):
    """The specific conflation this module exists to prevent.

    Ingest runs after the venues observed their books, so the two are never the
    same instant, and labelling one as the other overstates freshness.
    """
    session, _, _ = timed_session
    timing = snapshot_timing(session)
    assert timing["freshest_quote_observed_at"] != timing["market_ingest_started_at"]
    assert timing["freshest_quote_observed_at"] != timing["market_ingest_finished_at"]


def test_freshest_quote_is_not_the_arbitrage_scan_time(timed_session):
    session, _, _ = timed_session
    timing = snapshot_timing(session)
    assert timing["freshest_quote_observed_at"] != timing["arbitrage_scan_at"]


def test_artifact_build_and_deployment_times_are_absent_not_guessed(timed_session):
    """Neither is recorded, so neither may be approximated from what is.

    A null with a stated reason is honest; a plausible-looking value derived
    from ingest time or file mtime is invented provenance.
    """
    session, _, _ = timed_session
    timing = snapshot_timing(session)

    assert timing["snapshot_artifact_built_at"] is None
    assert timing["deployment_created_at"] is None
    for field in ("snapshot_artifact_built_at", "deployment_created_at"):
        assert field in timing["unavailable"]
        assert timing["unavailable"][field] == UNAVAILABLE_NOTES[field]

    # It must never silently equal a timestamp that *is* recorded.
    recorded = {
        timing["market_ingest_finished_at"],
        timing["freshest_quote_observed_at"],
        timing["arbitrage_scan_at"],
    }
    assert timing["snapshot_artifact_built_at"] not in (recorded - {None})


def test_the_spread_between_oldest_and_freshest_is_reported(timed_session):
    """A single timestamp cannot describe a dataset spanning eight days."""
    session, _, _ = timed_session
    timing = snapshot_timing(session)

    assert timing["oldest_quote_observed_at"] < timing["freshest_quote_observed_at"]
    assert timing["median_quote_observed_at"] is not None
    assert (
        timing["oldest_quote_observed_at"]
        <= timing["median_quote_observed_at"]
        <= timing["freshest_quote_observed_at"]
    )


def test_note_says_quotes_were_not_captured_together(timed_session):
    session, _, _ = timed_session
    assert snapshot_timing(session)["note"] == QUOTE_SPREAD_NOTE
    assert "not all at once" in QUOTE_SPREAD_NOTE


@pytest.fixture()
def client(clean_db):  # noqa: ANN001
    from fastapi.testclient import TestClient

    from pmvl_api.main import app

    clean_db.commit()
    return TestClient(app)


def test_system_route_exposes_the_timing_block_and_the_caveat(client):  # noqa: ANN001
    payload = client.get("/system").json()["data"]

    timing = payload["snapshot_timing"]
    for field in (
        "market_ingest_started_at",
        "market_ingest_finished_at",
        "freshest_quote_observed_at",
        "oldest_quote_observed_at",
        "median_quote_observed_at",
        "arbitrage_scan_at",
        "snapshot_artifact_built_at",
        "deployment_created_at",
    ):
        assert field in timing, field

    # The top-level field is retained for clients, but never alone: without the
    # caveat it reads as a capture time for everything.
    assert payload["freshest_quote_observed_at"] == timing["freshest_quote_observed_at"]
    assert payload["freshest_quote_observed_at_note"] == QUOTE_SPREAD_NOTE


def test_notice_is_absent_unless_a_snapshot_is_actually_being_served(client):  # noqa: ANN001
    """A live deployment must not warn about frozen data it is not serving."""
    assert client.get("/system").json()["data"]["snapshot_notice"] is None


def test_snapshot_notice_does_not_claim_a_single_capture_time(
    monkeypatch, clean_db  # noqa: ANN001
):
    """The notice used to end "See 'freshest_quote_observed_at' for the capture
    time", which named the one field that is not one."""
    from fastapi.testclient import TestClient

    from pmvl_api.main import app
    from pmvl_api.routers import system as system_router

    monkeypatch.setattr(system_router, "SNAPSHOT_MODE", True)
    clean_db.commit()

    notice = TestClient(app).get("/system").json()["data"]["snapshot_notice"]
    assert notice, "snapshot mode must produce a notice"
    assert "captured at build time" not in notice
    assert "no single capture time" in notice.lower()
    assert "snapshot_timing" in notice
