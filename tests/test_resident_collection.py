"""The resident collector: scheduling, cadence, isolation, and history.

Phase 4B1 proved the collectors *work*. These are about them running
**unattended**: that the canonical scheduler owns them, that the high-frequency
window keeps its cadence, that one failure does not take the rest down, and that
a day of downtime stays visible after a good day follows it.

The cadence tests are the ones that matter. A collector configured for 30 s that
actually runs at 60 s reports the configured number everywhere and silently
halves the sample - and that is exactly what the first implementation did, by
sleeping a fixed interval *after* a capture that itself took most of one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_shared.cadence import CADENCE_BY_JOB
from pmvl_shared.enums import DataProvenance, JobStatus
from pmvl_shared.pipeline_dag import PIPELINE, publication_blockers
from pmvl_shared.research_archive import ResearchArchive

from pmvl_markets.alpha.issuance import (
    DOCUMENTED_ISSUANCE_HOURS_UTC,
    MIN_DAYS_FOR_SCHEDULE,
    MIN_OBSERVATIONS_FOR_SCHEDULE,
    RESEARCH_CADENCE_SECONDS,
    WindowDerivation,
    observed_issuance_hours,
    windows_for_day,
)
from pmvl_markets.alpha.station_audit import (
    Axis,
    Severity,
    audit_stations,
    haversine_km,
    summarise,
)
from pmvl_markets.normalize.weather_stations import (
    STATIONS,
    parse_settlement_provenance,
    research_station_ids,
)

BASE = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

#: The research jobs this phase added to the canonical scheduler.
RESEARCH_JOBS = (
    "forecast_observations",
    "station_observations",
    "research_books",
    "research_trades",
    "map_stations",
    "collection_health",
)


def at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# The canonical scheduler owns the research jobs
# ---------------------------------------------------------------------------


class TestCanonicalSchedulerOwnsCollection:
    @pytest.mark.parametrize("job", RESEARCH_JOBS)
    def test_each_research_job_has_a_declared_cadence(self, job: str) -> None:
        """Cadences live in one place so the scheduler and the API cannot drift."""
        assert job in CADENCE_BY_JOB
        cadence = CADENCE_BY_JOB[job]
        assert cadence.interval_seconds or cadence.cron

    @pytest.mark.parametrize("job", RESEARCH_JOBS)
    def test_each_research_job_is_registered_with_the_scheduler(self, job: str) -> None:
        from pmvl_worker.scheduler import build_scheduler

        scheduler = build_scheduler()
        assert scheduler.get_job(job) is not None

    def test_no_second_scheduler_was_created(self) -> None:
        """One scheduler, one job registry.

        A parallel loop would double venue load and make `max_instances=1`
        meaningless, because the two schedulers would not know about each other.
        """
        worker = Path("services/worker/src/pmvl_worker")
        schedulers = [
            path.name
            for path in worker.glob("*.py")
            if "BlockingScheduler" in path.read_text()
            or "BackgroundScheduler" in path.read_text()
        ]
        assert schedulers == ["scheduler.py"]

    @pytest.mark.parametrize("job", RESEARCH_JOBS)
    def test_a_research_job_failure_cannot_block_publication(self, job: str) -> None:
        """Research collection must not gate the product, in either direction."""
        statuses = {spec.name: JobStatus.SUCCESS for spec in PIPELINE}
        statuses[job] = JobStatus.FAILED
        assert publication_blockers(statuses) == []

    def test_every_dag_job_has_an_invoker(self) -> None:
        """A job in the DAG with no implementation fails the run at the far end."""
        source = Path("scripts/run_automated_snapshot_pipeline.py").read_text()
        for spec in PIPELINE:
            assert f'name == "{spec.name}"' in source, f"{spec.name} has no invoker"


# ---------------------------------------------------------------------------
# Issuance-window cadence
# ---------------------------------------------------------------------------


class TestIssuanceCadence:
    def test_the_research_cadence_is_at_most_thirty_seconds(self) -> None:
        assert RESEARCH_CADENCE_SECONDS <= 30

    def test_the_capture_loop_sleeps_to_a_boundary_not_a_fixed_interval(self) -> None:
        """The defect this replaced.

        `sleep(30)` after a capture makes the period `capture + 30`. A
        research-universe capture measured ~30 s, so the effective cadence was
        ~60 s while every configured value still read 30 - half the sample, with
        nothing reporting a shortfall.
        """
        source = Path("services/worker/src/pmvl_worker/jobs.py").read_text()
        assert "next_tick" in source
        assert "asyncio.sleep(min(RESEARCH_CADENCE_SECONDS" not in source

    def test_an_overrunning_capture_skips_ticks_rather_than_shifting_them(self) -> None:
        """Otherwise one slow capture moves every later one off the grid."""
        source = Path("services/worker/src/pmvl_worker/jobs.py").read_text()
        assert "skipped += 1" in source
        assert "ticks_skipped" in source

    def test_a_full_window_at_cadence_clears_the_forty_book_gate(self) -> None:
        """The arithmetic behind the acceptance criterion, stated once."""
        window = windows_for_day(BASE)[0]
        assert window.expected_captures(RESEARCH_CADENCE_SECONDS) >= 40


class TestIssuanceScheduleDerivation:
    def test_market_fanout_is_not_read_as_publication_frequency(self) -> None:
        """One forecast is written once per market it is relevant to.

        A single poll of seven stations produced 84 rows and 7 publications, and
        the first version counted the 84 - concluding that NWS publishes at seven
        different hours a day, from one afternoon's snapshot.
        """
        one_poll = [BASE.replace(hour=h) for h in (6, 7, 8, 12, 14, 15, 16)]
        inflated = [ts for ts in one_poll for _ in range(12)]  # 84 rows, 7 instants
        assert observed_issuance_hours(inflated) is None

    def test_a_single_day_never_defines_a_schedule(self) -> None:
        """Twelve publications from one afternoon are facts about that afternoon."""
        same_day = [BASE + timedelta(minutes=7 * i) for i in range(30)]
        assert observed_issuance_hours(same_day) is None

    def test_a_recurring_hour_across_days_does_define_one(self) -> None:
        observed = [
            BASE.replace(hour=hour) + timedelta(days=day)
            for day in range(MIN_DAYS_FOR_SCHEDULE + 1)
            for hour in (2, 8, 14, 20)
        ]
        hours = observed_issuance_hours(observed)
        assert hours == (2, 8, 14, 20)

    def test_a_one_off_hour_does_not_widen_the_schedule(self) -> None:
        observed = [
            BASE.replace(hour=hour) + timedelta(days=day)
            for day in range(6)  # 12 instants clears MIN_OBSERVATIONS_FOR_SCHEDULE
            for hour in (2, 14)
        ]
        observed.append(BASE.replace(hour=5))  # a single late publication
        assert observed_issuance_hours(observed) == (2, 14)

    def test_the_documented_cycle_is_used_until_evidence_exists(self) -> None:
        windows = windows_for_day(BASE, observed_published_at=[BASE])
        assert all(
            w.derivation is WindowDerivation.DOCUMENTED_SCHEDULE for w in windows
        )
        assert [w.anchor.hour for w in windows] == list(DOCUMENTED_ISSUANCE_HOURS_UTC)

    def test_the_thresholds_are_stated_not_implied(self) -> None:
        assert MIN_OBSERVATIONS_FOR_SCHEDULE >= 12
        assert MIN_DAYS_FOR_SCHEDULE >= 3


# ---------------------------------------------------------------------------
# The >=40 gate
# ---------------------------------------------------------------------------


class TestTheFortyBookGate:
    def _archive_with(self, tmp_path: Path, per_market: dict[str, int]) -> ResearchArchive:
        archive = ResearchArchive(tmp_path / "a.db")
        anchor = datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc)
        with archive.connect() as con:
            for market, count in per_market.items():
                for i in range(count):
                    archive.archive_book(
                        con, platform="kalshi", platform_market_id=market,
                        observed_at=anchor + timedelta(seconds=30 * i - 900),
                        available_at=anchor + timedelta(seconds=30 * i - 899),
                        levels=[("yes", True, 0, Decimal("0.30"), Decimal("10"))],
                    )
        return archive

    def test_the_gate_counts_captures_per_market_not_rows(self, tmp_path: Path) -> None:
        """One capture of 252 markets is 252 rows and zero evidence of frequency.

        The row total passes trivially. The question is whether any single market
        was captured repeatedly THROUGH the window, which is a per-market count.
        """
        archive = self._archive_with(tmp_path, {f"M{i}": 1 for i in range(252)})
        with archive.connect() as con:
            best = con.execute(
                "SELECT MAX(n) FROM (SELECT COUNT(*) n FROM archived_books "
                "GROUP BY platform, platform_market_id)"
            ).fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM archived_books").fetchone()[0]
        assert total == 252  # would pass a row-count gate
        assert best == 1  # correctly fails the real one

    def test_a_genuinely_high_frequency_window_passes(self, tmp_path: Path) -> None:
        archive = self._archive_with(tmp_path, {"KXHIGHNY-T85": 60})
        with archive.connect() as con:
            best = con.execute(
                "SELECT MAX(n) FROM (SELECT COUNT(*) n FROM archived_books "
                "GROUP BY platform, platform_market_id)"
            ).fetchone()[0]
        assert best >= 40

    def test_the_gate_threshold_is_forty(self) -> None:
        """Pinned so it cannot be quietly lowered to make a run pass."""
        from pmvl_markets.alpha.collection_health import MIN_BOOKS_PER_ISSUANCE_WINDOW

        assert MIN_BOOKS_PER_ISSUANCE_WINDOW == 40

    @pytest.mark.integration
    def test_the_gate_calculation_is_deterministic(self, clean_db, tmp_path) -> None:  # noqa: ANN001
        """Hermetic on purpose.

        This read the real research archive on disk, so it was deterministic
        only while nothing was writing to that file. With a live collector two
        successive calls differ - 187,309 books then 187,393 - and the test
        failed for the one reason that is not a defect in the code under test.
        """
        from pmvl_markets.alpha.collection_health import build_report

        fixed = datetime(2026, 8, 25, 21, 0, tzinfo=timezone.utc)
        assert build_report(
            clean_db, days=1, now=fixed, archive_root=tmp_path
        ) == build_report(clean_db, days=1, now=fixed, archive_root=tmp_path)


# ---------------------------------------------------------------------------
# Health history is append-only
# ---------------------------------------------------------------------------


class TestHealthHistoryIsAppendOnly:
    def _report(self, day: str, passing: bool) -> dict:
        return {
            "generated_at": f"{day}T23:50:00+00:00",
            "all_gates_passing": passing,
            "station_observations": {"coverage": "100.0%" if passing else "10.0%"},
            "source_observations": {"published_at_coverage": "100.0%"},
            "settlement_mapping": {"mapped": 294, "unmapped": 0},
            "research_archive": {"books": 100, "trades": 50},
            "gates": [
                {"name": "station observations per station-day",
                 "actual": 36.0 if passing else 2.0, "expected": 20, "passing": passing},
                {"name": "research cache hits", "actual": 0, "expected": 0,
                 "passing": True},
                {"name": "payload integrity failures", "actual": 0, "expected": 0,
                 "passing": True},
                {"name": "books per issuance window (best market)",
                 "actual": 60 if passing else 0, "expected": 40, "passing": passing},
                {"name": "source poll success rate", "actual": 1.0, "expected": 0.95,
                 "passing": True},
            ],
        }

    def test_a_good_day_cannot_erase_a_bad_one(self, tmp_path: Path) -> None:
        """The entire point.

        A research result resting on a week with a silent gap is only detectable
        if the gap is still on record after the recovery.
        """
        archive = ResearchArchive(tmp_path / "a.db")
        with archive.connect() as con:
            archive.record_health(con, self._report("2026-08-14", passing=False))
            archive.record_health(con, self._report("2026-08-15", passing=True))
            history = archive.health_history(con)
        assert len(history) == 2
        assert {row["snapshot_date"] for row in history} == {
            "2026-08-14", "2026-08-15"
        }
        assert any(row["all_gates_passing"] == 0 for row in history)

    def test_two_snapshots_for_one_day_are_both_kept(self, tmp_path: Path) -> None:
        """Two rows for one day is a fact about that day, not a duplicate."""
        archive = ResearchArchive(tmp_path / "a.db")
        with archive.connect() as con:
            archive.record_health(con, self._report("2026-08-14", passing=False))
            archive.record_health(con, self._report("2026-08-14", passing=True))
            history = archive.health_history(con)
        assert len(history) == 2

    def test_the_full_report_survives_beside_the_promoted_scalars(
        self, tmp_path: Path
    ) -> None:
        """A field nobody promoted must still be recoverable later."""
        archive = ResearchArchive(tmp_path / "a.db")
        report = self._report("2026-08-14", passing=True)
        report["some_future_field"] = {"nested": [1, 2, 3]}
        with archive.connect() as con:
            archive.record_health(con, report)
            stored = con.execute(
                "SELECT payload_json FROM health_snapshots"
            ).fetchone()[0]
        assert json.loads(stored)["some_future_field"] == {"nested": [1, 2, 3]}

    def test_the_archive_has_no_update_or_delete_for_health(self) -> None:
        source = Path(
            "packages/shared/src/pmvl_shared/research_archive.py"
        ).read_text()
        assert "UPDATE health_snapshots" not in source
        assert "DELETE FROM health_snapshots" not in source


# ---------------------------------------------------------------------------
# Archived trades
# ---------------------------------------------------------------------------


class TestArchivedTrades:
    def test_product_retention_cannot_reach_archived_trades(self) -> None:
        """The product caps trades at 20 per market. The archive does not."""
        for path in (
            "scripts/build_snapshot.py",
            "packages/shared/src/pmvl_shared/snapshot_retention.py",
        ):
            source = Path(path).read_text()
            assert "archived_trades" not in source
            assert "research_archive" not in source

    def test_trades_are_archived_uncapped(self, tmp_path: Path) -> None:
        archive = ResearchArchive(tmp_path / "a.db")
        prints = [
            {"platform_trade_id": f"t{i}", "traded_at": at(i),
             "price": Decimal("0.30"), "size": Decimal("5"), "taker_side": "yes"}
            for i in range(500)
        ]
        with archive.connect() as con:
            written = archive.archive_trades(
                con, platform="kalshi", platform_market_id="KXHIGHNY-T85",
                trades=prints, available_at=at(600),
            )
        assert written == 500
        assert archive.stats().trades == 500

    def test_a_re_poll_does_not_duplicate_prints(self, tmp_path: Path) -> None:
        archive = ResearchArchive(tmp_path / "a.db")
        prints = [
            {"platform_trade_id": "t1", "traded_at": at(0),
             "price": Decimal("0.30"), "size": Decimal("5")}
        ]
        with archive.connect() as con:
            first = archive.archive_trades(
                con, platform="kalshi", platform_market_id="M",
                trades=prints, available_at=at(10),
            )
            second = archive.archive_trades(
                con, platform="kalshi", platform_market_id="M",
                trades=prints, available_at=at(20),
            )
        assert (first, second) == (1, 0)

    def test_a_print_carries_both_traded_at_and_available_at(
        self, tmp_path: Path
    ) -> None:
        """When it happened and when we could have known - different instants."""
        archive = ResearchArchive(tmp_path / "a.db")
        with archive.connect() as con:
            archive.archive_trades(
                con, platform="kalshi", platform_market_id="M",
                trades=[{"platform_trade_id": "t1", "traded_at": at(0),
                         "price": Decimal("0.3"), "size": Decimal("5")}],
                available_at=at(120),
            )
            traded, available = con.execute(
                "SELECT traded_at, available_at FROM archived_trades"
            ).fetchone()
        assert available > traded


# ---------------------------------------------------------------------------
# Station timestamp absence parity
# ---------------------------------------------------------------------------


class TestStationTimestampAbsence:
    @pytest.mark.integration
    def test_a_station_observation_documents_its_missing_publication_time(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """Parity with source_observations.

        The observations product carries no publication time at all, so without
        the note that NULL is an unexplained gap somebody could later "repair"
        with the observation time or the poll time.
        """
        from pmvl_markets.alpha.collection import PollWindow, record_station_observation

        poll = PollWindow(started_at=at(0), endpoint="/x")
        poll.completed_at = at(1)
        row = record_station_observation(
            clean_db, station_id="KNYC", source_product="observations",
            observation_time=at(-60), poll=poll, temperature_f=Decimal("80"),
            provenance=DataProvenance.FIXTURE,
        )
        assert row is not None
        assert row.published_at is None
        missing = (row.timestamp_absence or {})["missing"]
        assert "published_at" in missing
        assert "NOT defaulted" in missing["published_at"]

    @pytest.mark.integration
    def test_a_supplied_publication_time_records_no_absence(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.collection import PollWindow, record_station_observation

        poll = PollWindow(started_at=at(0), endpoint="/x")
        poll.completed_at = at(1)
        row = record_station_observation(
            clean_db, station_id="KNYC", source_product="cli_daily",
            observation_time=at(-60), poll=poll, published_at=at(-30),
            provenance=DataProvenance.FIXTURE,
        )
        assert row is not None
        assert row.published_at == at(-30)
        assert not (row.timestamp_absence or {}).get("missing")


# ---------------------------------------------------------------------------
# Station coordinate audit
# ---------------------------------------------------------------------------


class TestStationCoordinateAudit:
    def test_all_seven_stations_are_audited(self) -> None:
        report = summarise(audit_stations(model_stations={}))
        assert report["stations"] == 7
        assert len(research_station_ids()) == 7

    def test_the_model_token_is_declared_not_inferred(self) -> None:
        """A fuzzy matcher in an audit about silent mismatches manufactures
        findings and hides real ones - it reported KNYC's coordinate as absent
        because "new york city" does not resemble "Central Park"."""
        for station in STATIONS:
            assert station.model_token, f"{station.station_id} declares no token"

    def test_a_coordinate_disagreement_is_surfaced_with_its_distance(self) -> None:
        audits = audit_stations(
            model_stations={"austin": (30.3210, -97.7600)},
        )
        austin = next(a for a in audits if a.station.station_id == "KAUS")
        found = [d for d in austin.disagreements if d.axis is Axis.MODEL_COORDINATE]
        assert found
        assert found[0].severity is Severity.MODELLING
        assert 15 < found[0].distance_km < 18

    def test_an_agreeing_coordinate_raises_nothing(self) -> None:
        audits = audit_stations(
            model_stations={"philadelphia": (39.8683, -75.2311)},
        )
        phl = next(a for a in audits if a.station.station_id == "KPHL")
        assert not [
            d for d in phl.disagreements if d.axis is Axis.MODEL_COORDINATE
        ]

    def test_a_missing_model_coordinate_is_factual_not_modelling(self) -> None:
        audits = audit_stations(model_stations={})
        nyc = next(a for a in audits if a.station.station_id == "KNYC")
        found = [d for d in nyc.disagreements if d.axis is Axis.MODEL_COORDINATE]
        assert found and found[0].severity is Severity.FACTUAL

    def test_proximity_is_measured_not_used_to_resolve(self) -> None:
        """Matching by distance lets a WRONG coordinate select itself as right."""
        assert 15 < haversine_km((30.3210, -97.7600), (30.1975, -97.6664)) < 18

    def test_the_audit_calibrates_nothing(self) -> None:
        """Checked against the AST.

        The module docstring promises "does not calibrate a bias", and a
        substring scan flags that promise as a violation of itself.
        """
        import ast

        tree = ast.parse(
            Path(
                "packages/market-normalization/src/pmvl_markets/alpha/"
                "station_audit.py"
            ).read_text()
        )
        offenders = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and {"calibrate", "correct", "adjust", "fix"} & set(node.name.split("_"))
        ]
        assert offenders == []


# ---------------------------------------------------------------------------
# Settlement-source provenance
# ---------------------------------------------------------------------------


class TestSettlementSourceProvenance:
    def test_the_old_wording_parses_to_the_nws_directly(self) -> None:
        provenance = parse_settlement_provenance(
            "the highest temperature recorded in Central Park, New York as "
            "reported by the National Weather Service's Climatological Report",
            series_ticker="KXHIGHNY",
        )
        assert provenance.station_id == "KNYC"
        assert provenance.source_vendor == "national_weather_service"
        assert provenance.product_code == ""

    def test_the_new_wording_parses_to_the_vendor_and_a_product_code(self) -> None:
        provenance = parse_settlement_provenance(
            "the maximum temperature recorded at New York City (CLINYC) "
            "according to The Weather Company",
            series_ticker="KXHIGHNY",
        )
        assert provenance.station_id == "KNYC"
        assert provenance.source_vendor == "the_weather_company"
        assert provenance.product_code == "CLINYC"

    def test_the_station_survives_the_source_change(self) -> None:
        """The relay changed; the instrument did not. Recording them apart is
        what makes that statement checkable."""
        old = parse_settlement_provenance(
            "recorded in Central Park, New York per the National Weather Service",
            series_ticker="KXHIGHNY",
        )
        new = parse_settlement_provenance(
            "recorded at New York City (CLINYC) according to The Weather Company",
            series_ticker="KXHIGHNY",
        )
        assert old.station_id == new.station_id
        assert old.source_vendor != new.source_vendor

    def test_a_rewording_changes_the_hash_even_when_fields_match(self) -> None:
        """The August change kept the station and altered the words around it.
        A later equivalence audit needs to date the change from something."""
        first = parse_settlement_provenance(
            "recorded at New York City (CLINYC) according to The Weather Company",
            series_ticker="KXHIGHNY",
        )
        second = parse_settlement_provenance(
            "the maximum temperature recorded at New York City (CLINYC) for "
            "Aug 26 according to The Weather Company",
            series_ticker="KXHIGHNY",
        )
        assert first.station_id == second.station_id
        assert first.product_code == second.product_code
        assert first.rules_hash != second.rules_hash

    def test_the_hash_is_stable_across_whitespace_and_case(self) -> None:
        a = parse_settlement_provenance("At  New York City (CLINYC)", series_ticker="KXHIGHNY")
        b = parse_settlement_provenance("at new york city\n(clinyc)", series_ticker="KXHIGHNY")
        assert a.rules_hash == b.rules_hash


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class TestFailuresStayIsolated:
    def test_one_station_failure_does_not_end_the_pass(self) -> None:
        """Per-station try/except inside the loop, not around it.

        A station-level outage should cost that station's coverage, not the
        whole day's.
        """
        source = Path("services/worker/src/pmvl_worker/jobs.py").read_text()
        start = source.index("async def job_station_observations")
        body = source[start : source.index("async def job_research_books")]
        assert body.count("continue") >= 2
        assert "for station_id in research_station_ids()" in body

    def test_one_market_failure_does_not_end_the_trade_pass(self) -> None:
        source = Path("services/worker/src/pmvl_worker/jobs.py").read_text()
        start = source.index("async def job_research_trades")
        assert "errors.append" in source[start:]
        assert "continue" in source[start:]

    def test_an_archive_outage_does_not_fail_collection(self, tmp_path: Path) -> None:
        from pmvl_markets.alpha.archiving import archive_trades

        archive = ResearchArchive(Path("/dev/null/impossible/a.db"))
        from pmvl_shared.enums import Category, MarketStatus, Platform
        from pmvl_shared.schemas import NormalizedMarket

        market = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="KXHIGHNY-T85",
            series_ticker="KXHIGHNY", category=Category.WEATHER,
            status=MarketStatus.OPEN,
        )
        report = archive_trades(
            archive, {"kalshi:KXHIGHNY-T85": [object()]},
            {"kalshi:KXHIGHNY-T85": market},
        )
        assert report.errors
        assert report.trades_archived == 0


# ---------------------------------------------------------------------------
# Research firewall
# ---------------------------------------------------------------------------


class TestResearchFirewallHolds:
    MODULES = (
        "alpha/collection.py", "alpha/collection_health.py", "alpha/archiving.py",
        "alpha/issuance.py", "alpha/station_audit.py",
        "providers/nws_observations.py", "normalize/weather_stations.py",
    )

    FORBIDDEN = frozenset(
        {
            "markout", "forward", "future", "ic", "sharpe", "sortino", "pnl",
            "regress", "residualise", "residualize", "kelly", "fdr", "dsr",
            "pbo", "cpcv", "queue",
        }
    )

    @pytest.mark.parametrize("name", MODULES)
    def test_no_collection_module_computes_a_return(self, name: str) -> None:
        """Checked against the AST. The prose here deliberately SAYS
        "no markout", and a substring scan would flag the promise itself."""
        import ast

        root = Path("packages/market-normalization/src/pmvl_markets")
        tree = ast.parse((root / name).read_text())
        offenders = []
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            for identifier in names:
                hit = set(identifier.lower().split("_")) & self.FORBIDDEN
                if hit:
                    offenders.append(f"{identifier} ({sorted(hit)})")
        assert offenders == [], f"{name}: {offenders}"

    def test_the_collection_jobs_import_no_research_layer(self) -> None:
        import ast

        source = Path("services/worker/src/pmvl_worker/jobs.py").read_text()
        for node in ast.walk(ast.parse(source)):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            banned = set(module.split(".")) & {
                "simulator", "metrics", "strategy", "capacity", "evidence"
            }
            assert not banned, f"jobs.py imports {module}"

    def test_no_evidence_status_was_upgraded(self) -> None:
        from pmvl_markets.alpha.evidence import EvidenceStatus, assess

        result = assess(
            decisions=0, settled_decisions=0, synthetic=True,
            held_out=False, parameters_fitted_on_this_period=True,
        )
        assert result.status is EvidenceStatus.INSUFFICIENT_DATA
        assert result.may_claim_profitability is False
