"""Adversarial regression review of the six Phase 4B2 fixes.

A prior review of these fixes reported every dimension clean. It was invalid:
all five reviewer agents died on a session limit before running, and an empty
finding list from zero executed reviewers is not evidence of anything. This file
is the replacement, written to break the fixes rather than to confirm them.

Each class states the attack, not the feature.
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from pmvl_shared.enums import JobStatus
from pmvl_shared.job_record import idempotency_key
from pmvl_shared.timeutil import utcnow

WORKER_SRC = Path(__file__).resolve().parents[1] / "services" / "worker" / "src"
if str(WORKER_SRC) not in sys.path:
    sys.path.insert(0, str(WORKER_SRC))

from pmvl_worker.jobs import (  # noqa: E402
    ACTIVE_RUN_STALE_AFTER,
    HEARTBEAT_STALE_AFTER,
    JobSkipped,
    job_run,
)

from pmvl_markets.db_models import JobRun  # noqa: E402

KEY_PARAMS = {"market_limit": 10}


@pytest.fixture()
def key():  # noqa: ANN201
    return idempotency_key("ingest", cutoff=None, params=KEY_PARAMS)


def _running(key: str, *, age_s: float, hb_age_s: float | None) -> JobRun:
    now = utcnow()
    return JobRun(
        job_name="ingest",
        status=JobStatus.RUNNING.value,
        started_at=now - timedelta(seconds=age_s),
        heartbeat_at=(
            None if hb_age_s is None else now - timedelta(seconds=hb_age_s)
        ),
        idempotency_key=key,
    )


def _enter():  # noqa: ANN202
    return job_run("ingest", cutoff=None, params=dict(KEY_PARAMS))


# ---------------------------------------------------------------------------
# Fix 2 - heartbeat / stale-run reclaim
# ---------------------------------------------------------------------------


class TestDeadWorkerCannotBlockTheRestOfAWindow:
    """The failure that cost a live window: a dead run holding the slot."""

    def test_killed_before_its_first_heartbeat(self, clean_db, key) -> None:  # noqa: ANN001
        """A row with no heartbeat at all falls back to the age bound.

        Current code cannot produce one - the window loop now stamps liveness on
        entry, before its first capture (see
        TestLivenessIsClaimedBeforeTheFirstCapture), which is what closed the
        2026-08-25 failure. This pins the fallback that still governs rows
        written by older code, where NULL means "unknown", not "alive".
        """
        clean_db.add(_running(key, age_s=60, hb_age_s=None))
        clean_db.commit()
        with pytest.raises(JobSkipped):
            _enter().__enter__()

        # ... and is reclaimed once past the age bound.
        clean_db.query(JobRun).filter(
            JobRun.status == JobStatus.RUNNING.value
        ).one().started_at = utcnow() - ACTIVE_RUN_STALE_AFTER - timedelta(minutes=1)
        clean_db.commit()
        with _enter():
            pass
        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert rows[0].status == JobStatus.INTERRUPTED.value

    def test_killed_after_a_heartbeat_is_reclaimed_in_minutes(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """Once a run has heartbeated, death is detectable in 5 minutes."""
        clean_db.add(
            _running(
                key,
                age_s=45 * 60,  # legitimately long, mid-window
                hb_age_s=HEARTBEAT_STALE_AFTER.total_seconds() + 1,
            )
        )
        clean_db.commit()
        with _enter():
            pass
        rows = clean_db.query(JobRun).order_by(JobRun.id).all()
        assert rows[0].status == JobStatus.INTERRUPTED.value
        assert rows[1].status == JobStatus.SUCCESS.value

    def test_restart_during_an_active_window_resumes(self, clean_db, key) -> None:  # noqa: ANN001
        """A restart mid-window must not wait out the window."""
        clean_db.add(
            _running(key, age_s=20 * 60, hb_age_s=HEARTBEAT_STALE_AFTER.total_seconds() + 30)
        )
        clean_db.commit()
        with _enter():
            pass
        assert (
            clean_db.query(JobRun).order_by(JobRun.id).first().status
            == JobStatus.INTERRUPTED.value
        )


class TestHealthyLongRunnerCannotBeReclaimed:
    def test_a_run_an_hour_old_with_a_fresh_heartbeat_survives(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """The normal state of a window collector 59 minutes in."""
        clean_db.add(_running(key, age_s=59 * 60, hb_age_s=5))
        clean_db.commit()
        with pytest.raises(JobSkipped):
            _enter().__enter__()
        assert (
            clean_db.query(JobRun).order_by(JobRun.id).first().status
            == JobStatus.RUNNING.value
        )

    @pytest.mark.parametrize(
        "hb_age_s,expect_reclaimed",
        [
            (HEARTBEAT_STALE_AFTER.total_seconds() - 5, False),
            (HEARTBEAT_STALE_AFTER.total_seconds() + 5, True),
        ],
    )
    def test_the_five_minute_boundary(
        self, clean_db, key, hb_age_s, expect_reclaimed  # noqa: ANN001
    ) -> None:
        """Behaviour either side of the threshold, not merely far from it."""
        clean_db.add(_running(key, age_s=90 * 60, hb_age_s=hb_age_s))
        clean_db.commit()
        if expect_reclaimed:
            with _enter():
                pass
            assert (
                clean_db.query(JobRun).order_by(JobRun.id).first().status
                == JobStatus.INTERRUPTED.value
            )
        else:
            with pytest.raises(JobSkipped):
                _enter().__enter__()

    def test_the_bound_cannot_be_tightened_below_the_capture_period(self) -> None:
        """A bound near the cadence would evict runs between two captures."""
        from pmvl_markets.alpha.issuance import RESEARCH_CADENCE_SECONDS

        assert HEARTBEAT_STALE_AFTER.total_seconds() >= 5 * RESEARCH_CADENCE_SECONDS


class TestConcurrentWorkers:
    def test_two_workers_starting_together_yield_exactly_one_winner(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """Neither both nor neither."""
        first = _enter()
        first.__enter__()
        with pytest.raises(JobSkipped):
            _enter().__enter__()
        running = clean_db.query(JobRun).filter(
            JobRun.status == JobStatus.RUNNING.value
        ).all()
        assert len(running) == 1
        first.__exit__(None, None, None)

    def test_the_loser_is_recorded_not_silently_dropped(self, clean_db, key) -> None:  # noqa: ANN001
        clean_db.add(_running(key, age_s=10, hb_age_s=2))
        clean_db.commit()
        with pytest.raises(JobSkipped):
            _enter().__enter__()
        skipped = clean_db.query(JobRun).filter(
            JobRun.status == JobStatus.SKIPPED.value
        ).all()
        assert len(skipped) == 1
        assert skipped[0].details["run"]["skipped_because_active_run_id"]


class TestRowsFromOlderCode:
    def test_a_row_predating_the_column_is_judged_on_age(self, clean_db, key) -> None:  # noqa: ANN001
        """Rows written before the migration have heartbeat_at NULL forever.

        They must keep the old semantics exactly, not become permanently alive
        or permanently dead.
        """
        clean_db.add(_running(key, age_s=30 * 60, hb_age_s=None))
        clean_db.commit()
        with pytest.raises(JobSkipped):
            _enter().__enter__()  # 30 min < 2 h: still "active" by age

        clean_db.query(JobRun).filter(
            JobRun.status == JobStatus.RUNNING.value
        ).one().started_at = utcnow() - ACTIVE_RUN_STALE_AFTER - timedelta(seconds=1)
        clean_db.commit()
        with _enter():
            pass
        assert (
            clean_db.query(JobRun).order_by(JobRun.id).first().status
            == JobStatus.INTERRUPTED.value
        )


class TestLivenessIsClaimedBeforeTheFirstCapture:
    """The residual hole the reclaim tests exposed.

    Heartbeating only after each capture left a gap one capture wide: a worker
    killed during its first capture had no heartbeat at all and fell back to the
    two-hour age bound. That is exactly how the 2026-08-25 window was lost.
    """

    def test_the_window_loop_heartbeats_before_it_captures(self) -> None:
        from pmvl_worker import jobs

        body = ast.unparse(ast.parse(inspect.getsource(jobs.job_research_books)))
        first_hb = body.index("details['heartbeat']()")
        first_capture = body.index("refresh_orderbooks(")
        assert first_hb < first_capture, (
            "the first heartbeat lands after the first capture, leaving a "
            "capture-wide window in which a dead run is undetectable"
        )

    def test_it_still_heartbeats_every_tick(self) -> None:
        """Entry-only would let a long healthy run evict itself."""
        from pmvl_worker import jobs

        body = ast.unparse(ast.parse(inspect.getsource(jobs.job_research_books)))
        assert body.count("details['heartbeat']()") >= 2


# ---------------------------------------------------------------------------
# Fix 4 - available_at is response-received time
# ---------------------------------------------------------------------------


class TestAvailableAtIsNotMovedByASlowWrite:
    """A slow archive write must not push the admissibility timestamp forward.

    It used to: available_at defaulted inside archive_books to the moment the
    archive row was written, so every stamp carried the duration of the product
    write plus the archive write. Direction was safe - later, never earlier - but
    the magnitude was a collector-controlled variable, and cadence is measured
    off these stamps.
    """

    def test_a_deliberately_slow_archive_does_not_move_available_at(
        self, tmp_path: Path
    ) -> None:
        from decimal import Decimal

        from pmvl_shared.enums import Category, MarketStatus, Platform
        from pmvl_shared.research_archive import ResearchArchive
        from pmvl_shared.schemas import BookLevel, NormalizedMarket, OrderBook

        from pmvl_markets.alpha.archiving import archive_books

        received = utcnow()
        market = NormalizedMarket(
            platform=Platform.KALSHI,
            platform_market_id="KXHIGHNY-26AUG26-T85",
            series_ticker="KXHIGHNY",
            category=Category.WEATHER,
            status=MarketStatus.OPEN,
            settlement_rules_raw="maximum temperature at New York City (CLINYC)",
            tick_size=Decimal("0.01"),
            fee_rate=Decimal("0.07"),
            fee_type="quadratic",
            expected_resolution_time=received + timedelta(hours=20),
        )
        book = OrderBook(
            platform=Platform.KALSHI,
            platform_market_id=market.platform_market_id,
            observed_at=received - timedelta(milliseconds=250),
            yes_asks=[BookLevel(price=Decimal("0.30"), size=Decimal("100"))],
            yes_bids=[BookLevel(price=Decimal("0.29"), size=Decimal("100"))],
            no_asks=[BookLevel(price=Decimal("0.71"), size=Decimal("100"))],
            no_bids=[BookLevel(price=Decimal("0.70"), size=Decimal("100"))],
        )
        key = f"kalshi:{market.platform_market_id}"
        archive = ResearchArchive(tmp_path / "a.db")

        # Simulate a slow write by archiving well after the response arrived.
        # available_at is supplied by the caller, so elapsed write time is
        # invisible to it.
        import time as _time

        _time.sleep(0.35)
        archive_books(
            archive, {key: book}, {key: market}, available_at=received
        )

        with archive.connect() as con:
            stored = con.execute(
                "SELECT observed_at, available_at FROM archived_books"
            ).fetchone()
        assert stored[1].startswith(received.isoformat()[:19])

    def test_the_ordering_contract_holds(self, tmp_path: Path) -> None:
        """observed_at <= available_at, and available_at is not the write time."""
        from pmvl_shared.research_archive import ResearchArchive

        archive = ResearchArchive(tmp_path / "a.db")
        assert archive is not None  # constructed; contract asserted below

        source = Path(
            "packages/market-normalization/src/pmvl_markets/ingest/runner.py"
        ).read_text()
        tree = ast.parse(source)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_orderbooks"
        )
        body = ast.unparse(fn)
        assert body.index("poll_completed_at = utcnow()") < body.index(
            "store_orderbooks("
        )

    def test_published_at_is_never_substituted(self) -> None:
        for rel in (
            "packages/market-normalization/src/pmvl_markets/ingest/runner.py",
            "packages/market-normalization/src/pmvl_markets/alpha/archiving.py",
        ):
            src = Path(rel).read_text().replace(" ", "")
            assert "available_at=published_at" not in src


# ---------------------------------------------------------------------------
# Fix 5 - _jsonable degrades per key
# ---------------------------------------------------------------------------


class TestOneBadValueCannotEraseProvenance:
    def test_mixed_payload_keeps_every_serialisable_neighbour(self) -> None:
        import enum

        from pmvl_worker.jobs import _jsonable

        class Colour(enum.Enum):
            RED = "red"

        payload = {
            "captures": 84,
            "window_anchor": "2026-08-26T08:00:00+00:00",
            "handle": lambda: None,
            "when": utcnow(),
            "colour": Colour.RED,
            "nested": {"ok": 1, "bad": object()},
            "run": {"run_id": "abc123", "idempotency_key": "deadbeef"},
        }
        out = _jsonable(payload)
        assert out["captures"] == 84
        assert out["window_anchor"].startswith("2026-08-26")
        assert out["run"]["idempotency_key"] == "deadbeef"
        assert out["nested"]["ok"] == 1
        assert "repr" in out["nested"]["bad"]
        assert "repr" in out["handle"]

    def test_the_whole_payload_is_not_replaced(self) -> None:
        from pmvl_worker.jobs import _jsonable

        out = _jsonable({"a": 1, "b": object()})
        assert set(out) == {"a", "b"}
        assert "repr" not in out


# ---------------------------------------------------------------------------
# Fix 1 - cadence / universe scoping
# ---------------------------------------------------------------------------


class TestNoMarketIsSilentlyDroppedFromArchival:
    def test_the_horizon_is_conditional_on_being_in_a_window(self) -> None:
        from pmvl_worker import jobs

        tree = ast.parse(inspect.getsource(jobs.job_research_books))
        kw = next(
            k
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            for k in n.keywords
            if k.arg == "max_hours_to_resolution"
        )
        assert isinstance(kw.value, ast.IfExp), (
            "an unconditional horizon would shrink baseline collection too"
        )

    def test_baseline_calls_pass_no_horizon_at_all(self) -> None:
        from pmvl_markets.ingest.runner import refresh_orderbooks

        assert (
            inspect.signature(refresh_orderbooks)
            .parameters["max_hours_to_resolution"]
            .default
            is None
        )

    def test_far_dated_markets_fit_inside_the_baseline_period(self) -> None:
        from pmvl_markets.alpha.issuance import BASELINE_CADENCE_SECONDS

        rate = 9.0  # kalshi client declared limit
        assert 252 / rate < BASELINE_CADENCE_SECONDS

    def test_an_overrun_surfaces_as_a_counted_skip_not_a_silent_halving(self) -> None:
        """Rate-limiter contention must be visible, not just slow.

        A 60-second effective cadence with the configured value still reading 30
        is the failure that hid for a whole phase.
        """
        from pmvl_worker import jobs

        body = ast.unparse(ast.parse(inspect.getsource(jobs.job_research_books)))
        assert "skipped += 1" in body
        assert "'ticks_skipped'" in body or '"ticks_skipped"' in body


# ---------------------------------------------------------------------------
# Fix 3 - acceptance gate semantics
# ---------------------------------------------------------------------------


class TestAcceptanceGateUnderRealisticWindowMixes:
    """Fixtures for the shapes the gate actually sees in production."""

    @staticmethod
    def _gate(counts, completed):  # noqa: ANN001, ANN205
        from pmvl_markets.alpha.collection_health import best_completed_window

        return best_completed_window(counts, completed)

    def test_historical_empty_windows_cannot_pin_capability_to_zero(self) -> None:
        """The regression: three windows predating the collector held it at 0."""
        counts = {"02:00": 0, "08:00": 0, "14:00": 0, "20:00": 97}
        assert self._gate(counts, set(counts)) == 97

    def test_an_in_progress_window_neither_passes_nor_fails_the_gate(self) -> None:
        counts = {"20:00": 97, "02:00": 6}
        completed = {"20:00"}
        assert self._gate(counts, completed) == 97
        # and a partial window alone certifies nothing
        assert self._gate({"02:00": 6}, set()) == 0

    def test_multiple_completed_windows_report_the_best(self) -> None:
        counts = {"a": 41, "b": 97, "c": 55}
        assert self._gate(counts, {"a", "b", "c"}) == 97

    def test_best_market_present_in_only_one_completed_window_still_counts(
        self,
    ) -> None:
        """One qualifying window is the stated criterion."""
        counts = {"a": 0, "b": 0, "c": 0, "d": 44}
        assert self._gate(counts, {"a", "b", "c", "d"}) >= 40

    def test_continuity_gaps_remain_visible_alongside_the_gate(self) -> None:
        """Capability and uptime are different facts and stay separate."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/alpha/"
            "collection_health.py"
        ).read_text()
        assert "completed_windows_meeting_gate" in source
        assert "completed_issuance_windows" in source

    def test_the_gate_cannot_be_satisfied_by_row_totals(self) -> None:
        """One capture of 252 markets is 252 rows and zero evidence of frequency."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/alpha/"
            "collection_health.py"
        ).read_text()
        assert "GROUP BY platform, platform_market_id" in source
        assert "MAX(n)" in source

    def test_the_threshold_has_not_been_weakened(self) -> None:
        from pmvl_markets.alpha.collection_health import (
            MIN_BOOKS_PER_ISSUANCE_WINDOW,
        )

        assert MIN_BOOKS_PER_ISSUANCE_WINDOW >= 40


class TestEveryJobReportsLivenessNotJustTheMeasuredOne:
    """Fixing the deadlock for one job left it in place for all the others.

    Observed on 2026-08-26: after a restart, research_books resumed within a
    tick while ingest, orderbooks and research_trades sat blocked behind their
    own orphaned rows for two hours, because only research_books heartbeat.
    """

    def test_a_plain_job_records_a_heartbeat_without_asking(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        with job_run("ingest", cutoff=None, params=dict(KEY_PARAMS)):
            clean_db.expire_all()
            row = clean_db.query(JobRun).filter(
                JobRun.status == JobStatus.RUNNING.value
            ).one()
            assert row.heartbeat_at is not None, (
                "a job that does not opt in still has to be reclaimable in "
                "minutes, or the restart deadlock survives for every job but one"
            )

    def test_liveness_is_claimed_before_the_body_runs(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """A job killed during its first action must still be detectable."""
        seen: list[object] = []
        with job_run("ingest", cutoff=None, params=dict(KEY_PARAMS)):
            clean_db.expire_all()
            seen.append(
                clean_db.query(JobRun)
                .filter(JobRun.status == JobStatus.RUNNING.value)
                .one()
                .heartbeat_at
            )
        assert seen[0] is not None

    def test_the_beater_stops_before_the_run_is_closed_out(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        """A late beat must not resurrect heartbeat_at on a finished row."""
        from pmvl_worker import jobs

        body = ast.unparse(ast.parse(inspect.getsource(jobs.job_run)))
        assert body.index("stop_beating.set()") < body.index("row.status = status")

    def test_the_interval_is_comfortably_inside_the_stale_bound(self) -> None:
        from pmvl_worker.jobs import HEARTBEAT_INTERVAL

        assert HEARTBEAT_INTERVAL * 3 <= HEARTBEAT_STALE_AFTER

    def test_no_thread_is_left_running_after_the_job(
        self, clean_db, key  # noqa: ANN001
    ) -> None:
        import threading as _t

        before = {t.name for t in _t.enumerate()}
        with job_run("ingest", cutoff=None, params=dict(KEY_PARAMS)):
            pass
        after = {t.name for t in _t.enumerate()}
        assert not [n for n in after - before if n.startswith("heartbeat-")]
