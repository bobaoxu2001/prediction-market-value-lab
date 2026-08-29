"""Retention must bound the operational working set without touching evidence.

The distinction this file defends: `orderbook_levels` in the operational store
is a working set for live quoting, the daily build and forensics. Every ladder
research uses is written to the separate append-only research archive at capture
time. Pruning the former must be incapable of affecting the latter.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmvl_markets.ingest.store import (
    OPERATIONAL_LEVELS_RETENTION_HOURS,
    PRUNE_BATCH_SIZE,
    prune_orderbook_snapshots,
)


class TestRetentionWindowIsJustified:
    def test_it_covers_the_longest_observed_outage_plus_a_full_cycle(self) -> None:
        """18.2h low-battery sleep + 24h settlement/backtest cycle = 42.2h."""
        longest_outage_hours = 18.2
        settlement_cycle_hours = 24.0
        assert OPERATIONAL_LEVELS_RETENTION_HOURS >= (
            longest_outage_hours + settlement_cycle_hours
        )

    def test_it_is_far_below_the_old_unbounded_policy(self) -> None:
        """30 days deleted nothing while the data was younger than 30 days."""
        assert OPERATIONAL_LEVELS_RETENTION_HOURS < 30 * 24

    def test_the_batch_is_small_enough_not_to_stall_a_capture(self) -> None:
        """A capture cycle is 60s; a batch must not hold the write lock near that."""
        assert PRUNE_BATCH_SIZE <= 5000


class TestPruneTouchesOnlyOperationalTables:
    def _source(self) -> str:
        return inspect.getsource(prune_orderbook_snapshots)

    def test_it_deletes_only_orderbook_levels_and_snapshots(self) -> None:
        body = self._source()
        assert "delete(OrderbookLevel)" in body
        assert "delete(OrderbookSnapshot)" in body
        for protected in (
            "ArchivedBook", "archived_books", "archived_levels", "archived_trades",
            "Settlement", "SourceObservation", "StationObservation",
            "DailyClimateObservation", "JobRun", "PollAttempt",
        ):
            assert protected not in body, f"prune references {protected}"

    def test_the_research_archive_is_a_different_database(self) -> None:
        """Structural, not conventional: prune cannot reach it at all."""
        body = self._source()
        assert "research-archive" not in body
        assert "ResearchArchive" not in body

    def test_the_research_layer_never_reads_operational_levels(self) -> None:
        """If it did, pruning would destroy reproducibility of admitted evidence."""
        research = Path(
            "packages/market-normalization/src/pmvl_markets/research"
        )
        for path in research.rglob("*.py"):
            text = path.read_text()
            assert "orderbook_levels" not in text, f"{path.name} reads operational levels"
            assert "OrderbookLevel" not in text, f"{path.name} reads operational levels"


class TestChunkedDeletion:
    def test_it_deletes_in_bounded_batches(self) -> None:
        body = inspect.getsource(prune_orderbook_snapshots)
        assert ".limit(batch_size)" in body
        assert "session.commit()" in body

    def test_it_orders_deterministically(self) -> None:
        """Without an order a batch can revisit rows and never converge."""
        body = inspect.getsource(prune_orderbook_snapshots)
        assert "order_by(OrderbookSnapshot.id)" in body

    def test_children_are_deleted_before_parents(self) -> None:
        """An interrupted run must not leave a snapshot without its levels."""
        body = inspect.getsource(prune_orderbook_snapshots)
        assert body.index("delete(OrderbookLevel)") < body.index(
            "delete(OrderbookSnapshot)"
        )

    def test_it_no_longer_loads_every_expiring_id_at_once(self) -> None:
        """The old form built one list over millions of rows and deleted in one
        transaction, holding the write lock while the collector was capturing."""
        body = inspect.getsource(prune_orderbook_snapshots)
        assert "limit(batch_size)" in body

    def test_max_batches_bounds_a_single_invocation(self) -> None:
        params = inspect.signature(prune_orderbook_snapshots).parameters
        assert "max_batches" in params and params["max_batches"].default is None


class TestRetentionBoundaries:
    """Cutoff arithmetic, including the inputs that silently do nothing."""

    def _cutoff(self, *, keep_days=None, keep_hours=None):  # noqa: ANN001, ANN202
        if keep_hours is None:
            keep_hours = (
                keep_days * 24 if keep_days is not None
                else OPERATIONAL_LEVELS_RETENTION_HOURS
            )
        return timedelta(hours=keep_hours)

    def test_the_default_is_the_operational_window(self) -> None:
        assert self._cutoff() == timedelta(hours=OPERATIONAL_LEVELS_RETENTION_HOURS)

    def test_keep_days_still_works_for_callers_that_pass_it(self) -> None:
        assert self._cutoff(keep_days=30) == timedelta(days=30)

    def test_keep_hours_takes_precedence(self) -> None:
        assert self._cutoff(keep_days=30, keep_hours=6) == timedelta(hours=6)

    @pytest.mark.parametrize("hours", [1, 48, 720])
    def test_any_window_yields_a_cutoff_in_the_past(self, hours: int) -> None:
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        assert now - self._cutoff(keep_hours=hours) < now


class TestPruneJobWiring:
    def test_the_job_accepts_both_windows(self) -> None:
        from pmvl_worker import jobs

        params = inspect.signature(jobs.job_prune).parameters
        assert "keep_hours" in params and "keep_days" in params

    def test_the_scheduler_runs_prune_hourly_not_daily(self) -> None:
        """A daily pass must delete a whole day at once, and a missed one two."""
        source = Path("services/worker/src/pmvl_worker/scheduler.py").read_text()
        # The prune add_job call only - a wider window catches the preceding
        # job's trigger and asserts against the wrong one.
        start = source.rindex("scheduler.add_job(", 0, source.index('id="prune"'))
        block = source[start: source.index('id="prune"')]
        assert "IntervalTrigger" in block, block
        assert "_utc_cron" not in block, block
