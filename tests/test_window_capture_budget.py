"""The window universe must be small enough to actually capture at cadence.

Phase 4B2 shipped a 30-second in-window cadence and then measured 58 seconds.
Nothing was broken: the loop slept correctly, the window fired correctly, and
the venue answered every request. The universe was simply too large to poll in
30 seconds at the client's own rate limit, and no test asserted otherwise -
because every existing test checked the *sleep* arithmetic and none checked
whether the work between sleeps could fit.

These are the tests that would have caught it. They are arithmetic, not
integration: the fleet cannot dial a live venue, but it can multiply.
"""

from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from pathlib import Path

import pytest
from pmvl_shared.timeutil import utcnow

from pmvl_markets.alpha.issuance import (
    BASELINE_CADENCE_SECONDS,
    RESEARCH_CADENCE_SECONDS,
    WINDOW_HORIZON_HOURS,
    within_window_horizon,
)

#: The Kalshi client's declared limit. Read from source rather than restated, so
#: that lowering the limit fails this test instead of silently breaking cadence.
def _declared_rate_per_second() -> float:
    source = Path(
        "packages/market-normalization/src/pmvl_markets/providers/kalshi.py"
    ).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "rate_per_second":
            return float(ast.literal_eval(node.value))
    raise AssertionError("kalshi provider no longer declares rate_per_second")


class TestCaptureFitsInsideCadence:
    """The invariant the 58-second regression violated."""

    def test_near_dated_universe_polls_well_inside_one_period(self) -> None:
        rate = _declared_rate_per_second()
        # Seven stations, roughly six temperature buckets, today and tomorrow.
        near_dated = 7 * 6 * 2
        seconds = near_dated / rate
        # Half a period of headroom. The window loop is not the only caller of
        # the venue - the product orderbook and arbitrage jobs run through the
        # same limiter - and a capture sized to exactly fill its period has no
        # room for the contention that is always there in practice.
        assert seconds <= RESEARCH_CADENCE_SECONDS / 2, (
            f"{near_dated} markets at {rate}/s is {seconds:.1f}s of a "
            f"{RESEARCH_CADENCE_SECONDS}s period; no headroom for contention"
        )

    def test_full_research_universe_leaves_no_headroom(self) -> None:
        """Documents the arithmetic that forced the scoping decision.

        Note the failure mode: 252 markets at 9/s is 28 seconds, which *does*
        fit inside a 30-second period - on paper, with two seconds to spare.
        That is precisely why the regression was not obvious. Sharing the
        limiter with the product jobs turned those two seconds negative and the
        measured cadence was 58 seconds, not 31: each capture overran its tick,
        the loop skipped to the next boundary, and the period doubled.

        So the bar is headroom, not the raw bound. A capture allowed to consume
        its whole period has no slack to absorb contention that is always there.
        """
        rate = _declared_rate_per_second()
        full_universe = 252  # measured open weather markets, 2026-08-25
        assert full_universe / rate > RESEARCH_CADENCE_SECONDS / 2, (
            "the full universe now leaves real headroom - the window horizon "
            "may no longer be necessary, and the decision should be revisited"
        )

    def test_baseline_cadence_still_covers_the_whole_board(self) -> None:
        """Nothing is dropped: the far-dated markets keep their slow captures."""
        rate = _declared_rate_per_second()
        assert 252 / rate < BASELINE_CADENCE_SECONDS


class TestWindowHorizonPredicate:
    def test_market_resolving_tomorrow_is_eligible(self) -> None:
        assert within_window_horizon(utcnow() + timedelta(hours=20))

    def test_market_resolving_next_week_is_not(self) -> None:
        assert not within_window_horizon(utcnow() + timedelta(days=7))

    def test_already_resolved_market_is_not(self) -> None:
        """Its book no longer moves on a forecast revision."""
        assert not within_window_horizon(utcnow() - timedelta(hours=1))

    def test_unknown_resolution_is_excluded_not_admitted(self) -> None:
        """Admitting unknowns would let the unbounded set back in through a gap."""
        assert not within_window_horizon(None)

    @pytest.mark.parametrize("hours", [0.0, WINDOW_HORIZON_HOURS])
    def test_boundaries_are_inclusive(self, hours: float) -> None:
        now = utcnow()
        assert within_window_horizon(now + timedelta(hours=hours), now=now)

    def test_horizon_spans_at_least_tomorrow(self) -> None:
        """A revision to tomorrow's forecast moves tomorrow's contracts.

        A horizon under 24h would exclude exactly the markets the window is for.
        """
        assert WINDOW_HORIZON_HOURS >= 24


class TestHorizonAppliesOnlyInsideAWindow:
    def test_the_job_scopes_by_horizon_only_when_a_window_is_open(self) -> None:
        """Outside a window the full board must still be captured."""
        from pmvl_worker import jobs

        source = inspect.getsource(jobs.job_research_books)
        tree = ast.parse(ast.unparse(ast.parse(source)))
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword) and node.arg == "max_hours_to_resolution"
        ]
        assert found, "job no longer bounds the in-window universe"
        # The value must be conditional, never an unconditional constant.
        assert isinstance(found[0].value, ast.IfExp), (
            "max_hours_to_resolution is unconditional - this would silently "
            "shrink baseline collection to the near-dated markets too"
        )

    def test_refresh_orderbooks_defaults_to_the_whole_board(self) -> None:
        from pmvl_markets.ingest.runner import refresh_orderbooks

        default = inspect.signature(refresh_orderbooks).parameters[
            "max_hours_to_resolution"
        ].default
        assert default is None


class TestAvailableAtIsPollCompletion:
    """``available_at`` is the admissibility timestamp and may come from one
    place only: the moment the venue's response was in hand.

    It used to default inside ``archive_books`` to the archive-write time, which
    folded the duration of the product write into every availability stamp. The
    direction was safe - later, never earlier - but the magnitude was a variable
    the collector controlled, and cadence is measured off these timestamps.
    """

    def test_the_runner_stamps_completion_before_writing_anything(self) -> None:
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
        stamp = body.index("poll_completed_at = utcnow()")
        write = body.index("store_orderbooks(")
        assert stamp < write, (
            "poll completion is stamped after the product write, so the write "
            "duration is being reported as data availability"
        )

    def test_the_archive_call_passes_it_explicitly(self) -> None:
        from pmvl_markets.ingest import runner

        fn = next(
            n
            for n in ast.walk(ast.parse(inspect.getsource(runner.refresh_orderbooks)))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "archive_books"
        )
        passed = {k.arg for k in fn.keywords}
        assert "available_at" in passed, (
            "archive_books would default available_at to write time"
        )

    def test_published_at_is_never_a_substitute(self) -> None:
        """The one substitution that would create look-ahead."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/ingest/runner.py"
        ).read_text()
        assert "available_at=published_at" not in source.replace(" ", "")


class TestAcceptanceGateSemantics:
    """The gate is the acceptance instrument, so its arithmetic is load-bearing.

    It read `min(window_counts.values())` under the label "best market": it
    failed unless *every* window in the lookback was well sampled, so the four
    windows that predate the collector's existence pinned it to zero. A collector
    running perfectly could not have cleared it, and the number it printed was
    not the number its own name described.
    """

    def test_one_good_completed_window_clears_the_gate(self) -> None:
        from pmvl_markets.alpha.collection_health import (
            MIN_BOOKS_PER_ISSUANCE_WINDOW,
            best_completed_window,
        )

        counts = {"w1": 0, "w2": 0, "w3": 0, "w4": 55}
        got = best_completed_window(counts, {"w1", "w2", "w3", "w4"})
        assert got == 55 >= MIN_BOOKS_PER_ISSUANCE_WINDOW

    def test_windows_predating_the_collector_do_not_pin_it_to_zero(self) -> None:
        """The exact regression, as observed on 2026-08-25."""
        from pmvl_markets.alpha.collection_health import best_completed_window

        counts = {"20:00": 0, "02:00": 0, "08:00": 0, "14:00": 0, "20:00b": 55}
        assert best_completed_window(counts, set(counts)) == 55

    def test_an_in_progress_window_cannot_fail_the_gate(self) -> None:
        """It has not had its chance yet.

        Counting it would report a healthy collector as failing for thirty
        minutes out of every six hours.
        """
        from pmvl_markets.alpha.collection_health import best_completed_window

        counts = {"done": 55, "running": 3}
        assert best_completed_window(counts, {"done"}) == 55

    def test_an_in_progress_window_cannot_pass_it_either(self) -> None:
        """Symmetry matters: a partial window must not certify the acceptance."""
        from pmvl_markets.alpha.collection_health import best_completed_window

        assert best_completed_window({"running": 99}, set()) == 0

    def test_no_windows_at_all_is_zero_not_a_pass(self) -> None:
        from pmvl_markets.alpha.collection_health import best_completed_window

        assert best_completed_window({}, set()) == 0

    def test_the_threshold_is_not_weakened(self) -> None:
        """The stated acceptance criterion, pinned so it cannot drift down."""
        from pmvl_markets.alpha.collection_health import (
            MIN_BOOKS_PER_ISSUANCE_WINDOW,
        )

        assert MIN_BOOKS_PER_ISSUANCE_WINDOW >= 40

    def test_continuity_is_reported_separately_from_the_gate(self) -> None:
        """A gap in the past is a real fact, and must not be silently folded
        into a gate about present capability - nor dropped entirely."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/alpha/"
            "collection_health.py"
        ).read_text()
        assert "completed_windows_meeting_gate" in source
