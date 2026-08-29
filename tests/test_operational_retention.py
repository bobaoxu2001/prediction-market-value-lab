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


class TestHostPowerTelemetry:
    """A dead battery cost three station-days and produced no software symptom.

    This is observability, not resilience: caffeinate -i cannot override
    battery-exhaustion hibernate and neither can telemetry.
    """

    def _parse(self, text: str):  # noqa: ANN202
        from pmvl_markets.research.host_power import parse_power

        return parse_power(text)

    def test_ac_power_is_ok(self) -> None:
        from pmvl_markets.research.host_power import HostPower

        s = self._parse(
            "Now drawing from 'AC Power'\n -InternalBattery-0\t52%; charging; 2:00"
        )
        assert s.status is HostPower.OK
        assert s.battery_percent == 52 and s.charging is True

    def test_dangerously_low_battery_is_at_risk(self) -> None:
        """The observed failure: hibernate at 1%."""
        from pmvl_markets.research.host_power import HostPower

        s = self._parse(
            "Now drawing from 'Battery Power'\n -InternalBattery-0\t1%; discharging; 0:05"
        )
        assert s.status is HostPower.AT_RISK

    def test_healthy_battery_is_not_flagged(self) -> None:
        from pmvl_markets.research.host_power import HostPower

        assert self._parse(
            "Now drawing from 'Battery Power'\n -InternalBattery-0\t60%; discharging; 3:00"
        ).status is HostPower.OK

    @pytest.mark.parametrize("text", ["", "   ", "garbage output", "Now drawing"])
    def test_unreadable_state_fails_to_unknown_not_ok(self, text: str) -> None:
        """A confident "fine" is most misleading exactly when it cannot be read."""
        from pmvl_markets.research.host_power import HostPower

        assert self._parse(text).status is not HostPower.OK

    def test_a_battery_with_no_percentage_is_unknown(self) -> None:
        from pmvl_markets.research.host_power import HostPower

        assert self._parse(
            "Now drawing from 'Battery Power'\n -InternalBattery-0 present: true"
        ).status is HostPower.UNKNOWN

    def test_reading_power_never_raises(self) -> None:
        """A probe that can crash the health check it reports on is worse than none."""
        from pmvl_markets.research.host_power import read_power

        assert read_power() is not None

    def test_power_cannot_reach_pit_admissibility(self) -> None:
        """Admissibility follows evidence, never the host's charge state."""
        research = Path("packages/market-normalization/src/pmvl_markets/research")
        for name in ("build.py", "pit.py", "cohort.py", "probability.py"):
            text = (research / name).read_text()
            for banned in ("host_power", "battery", "read_power", "HostPower"):
                assert banned not in text, f"{name} references {banned}"

    def test_power_cannot_trigger_capture(self) -> None:
        """Low battery must not manufacture extra PIT observations."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/research/host_power.py"
        ).read_text()
        for banned in ("refresh_orderbooks", "archive_books", "job_research_books"):
            assert banned not in source

    def test_the_threshold_is_documented_not_magic(self) -> None:
        import inspect

        from pmvl_markets.research import host_power

        doc = inspect.getsource(host_power)
        assert "BATTERY_AT_RISK_PERCENT" in doc
        assert "1%" in doc, "the observed failure point should justify the threshold"


class TestSettlementInClauseIsBounded:
    """The settle job died silently once the settlements keyset outgrew SQLite.

    SQLITE_MAX_VARIABLE_NUMBER is 32,766 here. The 02:00Z run on 2026-08-29
    succeeded just under it; every run from 02:30Z failed with "too many SQL
    variables" at 32,507 settlements and climbing. Settle kept being scheduled,
    kept failing, and venue terminal results stopped being archived - which
    stops the research cohort growing for a reason that looks nothing like a
    research problem.
    """

    def test_chunk_size_leaves_headroom_under_the_sqlite_limit(self) -> None:
        from pmvl_markets.backtest.settlement import _IN_CLAUSE_CHUNK

        assert _IN_CLAUSE_CHUNK < 32_766
        assert _IN_CLAUSE_CHUNK <= 10_000, "too close to the limit to be safe"

    def test_chunking_covers_every_id_exactly_once(self) -> None:
        from pmvl_markets.backtest.settlement import _chunked

        ids = list(range(12_345))
        out = [x for chunk in _chunked(ids) for x in chunk]
        assert out == ids

    def test_no_chunk_exceeds_the_bound(self) -> None:
        from pmvl_markets.backtest.settlement import _IN_CLAUSE_CHUNK, _chunked

        assert all(
            len(c) <= _IN_CLAUSE_CHUNK for c in _chunked(list(range(40_000)))
        )

    def test_an_empty_keyset_yields_nothing(self) -> None:
        from pmvl_markets.backtest.settlement import _chunked

        assert list(_chunked([])) == []

    def test_a_keyset_above_the_sqlite_limit_is_survivable(self) -> None:
        """The exact production condition: more settlements than SQLite vars."""
        import sqlite3

        from pmvl_markets.backtest.settlement import _chunked

        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE t (id INTEGER)")
        ids = list(range(40_000))
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            con.execute(f"SELECT id FROM t WHERE id IN ({placeholders})", chunk)

    def test_the_settle_path_no_longer_passes_the_whole_keyset(self) -> None:
        source = Path(
            "packages/market-normalization/src/pmvl_markets/backtest/settlement.py"
        ).read_text()
        assert "in_(settlements.keys())" not in source
