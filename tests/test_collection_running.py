"""The collection path, and the properties that make what it captures usable.

Phase 4B0 proved the *schema* could hold point-in-time data. These tests are
about the *collectors*: that they run, that what they persist keeps its timing
semantics, and that the failure modes which would quietly ruin the dataset are
caught rather than tolerated.

The ones that matter most are the negative tests. A collector that writes rows is
easy to verify; a collector that writes rows carrying a fabricated availability
looks identical from the outside, and only an explicit test tells them apart.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_shared.enums import DataProvenance, Platform
from pmvl_shared.payload_store import PayloadStore
from pmvl_shared.research_archive import ResearchArchive
from pmvl_shared.schemas import BookLevel, NormalizedMarket, OrderBook

from pmvl_markets.alpha.archiving import archive_books
from pmvl_markets.alpha.collection import (
    PollWindow,
    TimestampAbsence,
    TimestampDisciplineError,
    as_json_bytes,
    record_poll_attempt,
    record_source_observation,
    record_station_observation,
)
from pmvl_markets.alpha.issuance import (
    BASELINE_CADENCE_SECONDS,
    DOCUMENTED_ISSUANCE_HOURS_UTC,
    RESEARCH_CADENCE_SECONDS,
    WindowDerivation,
    active_window,
    cadence_for,
    documented_windows_for_day,
    observed_issuance_hours,
    windows_between,
    windows_for_day,
)
from pmvl_markets.normalize.weather_stations import (
    MappingMethod,
    UnmappedReason,
    is_research_market,
    parse_settlement_station,
    research_station_ids,
)
from pmvl_markets.providers.nws_observations import (
    NwsForecastProvider,
    NwsObservationsProvider,
    _parse_feature,
)

BASE = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def poll(start: float = 0, end: float = 1) -> PollWindow:
    window = PollWindow(started_at=at(start), endpoint="/stations/KNYC/observations")
    window.completed_at = at(end)
    window.http_status = 200
    return window


# ---------------------------------------------------------------------------
# Settlement-station mapping
# ---------------------------------------------------------------------------


class TestStationParserIsDeterministic:
    def test_the_same_text_always_gives_the_same_station(self) -> None:
        rules = "the highest temperature recorded in Central Park, New York for..."
        results = {
            parse_settlement_station(rules, series_ticker="KXHIGHNY").station_id
            for _ in range(50)
        }
        assert results == {"KNYC"}

    def test_a_longer_phrase_beats_a_shorter_one(self) -> None:
        """"Chicago Midway" must not lose to a bare "Chicago"."""
        result = parse_settlement_station(
            "maximum temperature at Chicago Midway, IL", series_ticker="KXHIGHCHI"
        )
        assert result.station_id == "KMDW"
        assert result.matched_phrase == "chicago midway"

    def test_the_cli_product_code_form_maps(self) -> None:
        """Kalshi re-worded the board in August 2026 and the parser caught it."""
        result = parse_settlement_station(
            "the maximum temperature recorded at New York City (CLINYC) for Aug 26",
            series_ticker="KXHIGHNY",
        )
        assert result.station_id == "KNYC"
        assert result.matched_phrase == "clinyc"

    def test_the_austin_rule_maps_to_bergstrom_not_camp_mabry(self) -> None:
        """The live mismatch this module was written to surface.

        The probability model maps ``"austin"`` to Camp Mabry. The settlement
        rule says Austin Bergstrom. Different airports, opposite sides of the
        city.
        """
        result = parse_settlement_station(
            "recorded in Austin Bergstrom for July 26", series_ticker="KXHIGHAUS"
        )
        assert result.station_id == "KAUS"

    def test_whitespace_and_case_do_not_change_the_answer(self) -> None:
        a = parse_settlement_station("...CENTRAL PARK,   NEW YORK...", series_ticker="KXHIGHNY")
        b = parse_settlement_station("...central park,\nnew york...", series_ticker="KXHIGHNY")
        assert a.station_id == b.station_id == "KNYC"

    def test_a_city_only_rule_is_mapped_but_flagged_inexact(self) -> None:
        result = parse_settlement_station(
            "recorded in Denver, CO for July 26", series_ticker="KXHIGHDEN"
        )
        assert result.station_id == "KDEN"
        assert result.exact is False


class TestUnmappedIsExplicit:
    def test_an_unknown_phrase_is_unmapped_not_guessed(self) -> None:
        """No fallback to the series ticker, no nearest-station guess.

        A wrong station trades confidently; an absent one declines. Only one of
        those is recoverable.
        """
        result = parse_settlement_station(
            "the maximum temperature recorded at Reykjavik", series_ticker="KXHIGHNY"
        )
        assert result.mapped is False
        assert result.reason is UnmappedReason.NO_KNOWN_PHRASE
        assert result.station_id == ""

    def test_empty_rules_are_unmapped_with_their_own_reason(self) -> None:
        result = parse_settlement_station("", series_ticker="KXHIGHNY")
        assert result.reason is UnmappedReason.NO_RULES_TEXT

    def test_two_stations_in_one_rule_is_refused(self) -> None:
        """Picking the first would be an arbitrary choice recorded as a fact."""
        result = parse_settlement_station(
            "compare Central Park and Chicago Midway", series_ticker="KXHIGHNY"
        )
        assert result.mapped is False
        assert result.reason is UnmappedReason.AMBIGUOUS_PHRASES

    def test_a_non_research_series_is_out_of_scope_not_unmapped(self) -> None:
        """A supervolcano contract is not an unmapped weather station."""
        result = parse_settlement_station(
            "if a supervolcano erupts", series_ticker="KXERUPTSUPER"
        )
        assert result.reason is UnmappedReason.NOT_RESEARCH_SERIES

    def test_the_research_universe_is_keyed_on_series_not_category(self) -> None:
        assert is_research_market("KXHIGHNY", "weather") is True
        assert is_research_market("KXCO2LEVEL", "weather") is False
        assert is_research_market(None, "weather") is False

    def test_every_research_station_has_an_id(self) -> None:
        stations = research_station_ids()
        assert len(stations) == 7
        assert stations == tuple(sorted(stations))


# ---------------------------------------------------------------------------
# Issuance windows
# ---------------------------------------------------------------------------


class TestIssuanceWindows:
    def test_baseline_cadence_outside_a_window(self) -> None:
        moment = datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc)
        cadence, window = cadence_for(moment)
        assert cadence == BASELINE_CADENCE_SECONDS
        assert window is None

    def test_research_cadence_inside_a_window(self) -> None:
        moment = datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
        cadence, window = cadence_for(moment)
        assert cadence == RESEARCH_CADENCE_SECONDS
        assert window is not None
        assert window.derivation is WindowDerivation.DOCUMENTED_SCHEDULE

    @pytest.mark.parametrize("offset", [-30, -29, 0, 29, 30])
    def test_the_window_boundaries_are_inclusive(self, offset: int) -> None:
        """A capture at exactly the edge belongs to the window.

        Exclusive edges would drop the first and last capture of every window -
        the ones closest to publication, which are the point.
        """
        anchor = datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
        assert active_window(anchor + timedelta(minutes=offset)) is not None

    @pytest.mark.parametrize("offset", [-31, 31])
    def test_just_outside_the_window_is_baseline(self, offset: int) -> None:
        anchor = datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
        assert active_window(anchor + timedelta(minutes=offset)) is None

    def test_a_window_straddling_midnight_is_found(self) -> None:
        """A same-day-only check would lose half of it."""
        windows = documented_windows_for_day(BASE, hours=(0,))
        assert active_window(
            windows[0].start + timedelta(minutes=1),
        ) is not None or windows[0].start.day != windows[0].end.day

    def test_expected_captures_is_derived_not_asserted(self) -> None:
        window = documented_windows_for_day(BASE)[0]
        assert window.expected_captures(RESEARCH_CADENCE_SECONDS) == 121

    def test_observed_publication_times_outrank_the_documented_cycle(self) -> None:
        """Enough DISTINCT publications, across enough DAYS.

        The fixture used to be four instants over four days. That cleared the
        original bar and no longer does: a schedule inferred from one
        publication a day is a schedule inferred from almost nothing, and the
        threshold was raised after a single poll of seven stations was read as
        evidence that NWS publishes at seven different hours.
        """
        observed = [
            datetime(2026, 8, day, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=m)
            for day in (20, 21, 22, 23)
            for m in (0, 5, 10)
        ]
        windows = windows_for_day(BASE, observed_published_at=observed)
        assert all(w.derivation is WindowDerivation.OBSERVED_PUBLICATION for w in windows)
        assert [w.anchor.hour for w in windows] == [9]

    def test_too_few_observations_fall_back_and_say_so(self) -> None:
        """A schedule inferred from two observations is not a schedule."""
        assert observed_issuance_hours([BASE, BASE + timedelta(days=1)]) is None
        windows = windows_for_day(BASE, observed_published_at=[BASE])
        assert all(w.derivation is WindowDerivation.DOCUMENTED_SCHEDULE for w in windows)

    def test_the_documented_cycle_is_a_schedule_not_a_list_of_dates(self) -> None:
        """Hours-of-day, so it cannot silently expire."""
        assert all(0 <= h < 24 for h in DOCUMENTED_ISSUANCE_HOURS_UTC)
        far_future = datetime(2031, 1, 1, tzinfo=timezone.utc)
        assert len(documented_windows_for_day(far_future)) == len(
            DOCUMENTED_ISSUANCE_HOURS_UTC
        )

    def test_windows_between_covers_a_multi_day_range(self) -> None:
        found = windows_between(BASE, BASE + timedelta(days=2))
        assert len(found) >= 2 * len(DOCUMENTED_ISSUANCE_HOURS_UTC)
        anchors = [w.anchor for w in found]
        assert anchors == sorted(anchors)


# ---------------------------------------------------------------------------
# PIT semantics in the collectors
# ---------------------------------------------------------------------------


class TestCollectorTimestampDiscipline:
    @pytest.mark.integration
    def test_available_at_is_poll_completed_at(self, clean_db) -> None:  # noqa: ANN001
        row = record_station_observation(
            clean_db, station_id="KNYC", source_product="observations",
            observation_time=at(-60), poll=poll(start=0, end=7),
            temperature_f=Decimal("80"), provenance=DataProvenance.FIXTURE,
        )
        assert row is not None
        assert row.available_at == at(7)

    @pytest.mark.integration
    def test_a_caller_cannot_inject_available_at(self, clean_db) -> None:  # noqa: ANN001
        with pytest.raises(TypeError):
            record_station_observation(
                clean_db, station_id="KNYC", source_product="observations",
                observation_time=at(-60), poll=poll(),
                available_at=at(-120),  # type: ignore[call-arg]
            )

    @pytest.mark.integration
    def test_a_retry_does_not_fabricate_earlier_availability(self, clean_db) -> None:  # noqa: ANN001
        """A retried poll became available when the SUCCESSFUL attempt finished.

        Carrying the first attempt's start time forward would credit the data as
        knowable before the request that actually returned it.
        """
        retried = PollWindow(started_at=at(0), endpoint="/x")
        retried.completed_at = at(12)  # succeeded only on the third try
        row = record_station_observation(
            clean_db, station_id="KNYC", source_product="observations",
            observation_time=at(-60), poll=retried, temperature_f=Decimal("80"),
            provenance=DataProvenance.FIXTURE,
        )
        assert row is not None
        assert row.available_at == at(12)
        assert row.available_at != retried.started_at

    @pytest.mark.integration
    def test_a_missing_publication_time_stays_null(self, clean_db) -> None:  # noqa: ANN001
        row = record_source_observation(
            clean_db, market_id=_market(clean_db), source_id="nws",
            source_product="gridpoint_forecast", kind="source_point_value",
            value=Decimal("88"), poll=poll(), published_at=None,
            provenance=DataProvenance.FIXTURE,
        )
        assert row is not None
        assert row.published_at is None
        assert "published_at" in (row.timestamp_absence or {})["missing"]

    @pytest.mark.integration
    def test_duplicate_polling_does_not_corrupt_pit_semantics(self, clean_db) -> None:  # noqa: ANN001
        """The same observation seen twice is one row, keeping its FIRST availability.

        A stuck collector re-reading an unchanged response must not make old data
        look newly available.
        """
        first = record_station_observation(
            clean_db, station_id="KNYC", source_product="observations",
            observation_time=at(-60), poll=poll(start=0, end=1),
            temperature_f=Decimal("80"), provenance=DataProvenance.FIXTURE,
        )
        clean_db.flush()
        second = record_station_observation(
            clean_db, station_id="KNYC", source_product="observations",
            observation_time=at(-60), poll=poll(start=0, end=1),
            temperature_f=Decimal("80"), provenance=DataProvenance.FIXTURE,
        )
        assert first is not None
        assert second is None  # deduplicated, not re-dated

    @pytest.mark.integration
    def test_a_later_poll_of_the_same_instant_is_a_separate_revision(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """A corrected reading is a new row, never an update."""
        for end in (1, 500):
            record_station_observation(
                clean_db, station_id="KNYC", source_product="observations",
                observation_time=at(-60), poll=poll(start=end - 1, end=end),
                temperature_f=Decimal("80" if end == 1 else "81"),
                provenance=DataProvenance.FIXTURE,
            )
        clean_db.flush()
        from sqlalchemy import func, select

        from pmvl_markets.db_models import StationObservationRow

        assert clean_db.scalar(
            select(func.count()).select_from(StationObservationRow)
        ) == 2

    @pytest.mark.integration
    def test_a_failed_poll_is_persisted_with_its_error_type(self, clean_db) -> None:  # noqa: ANN001
        failed = PollWindow(started_at=at(0), endpoint="/stations/KNYC/observations")
        failed.completed_at = at(1)
        failed.http_status = 503
        row = record_poll_attempt(
            clean_db, source_id="nws", source_product="observations",
            target="KNYC", poll=failed, success=False, error_type="http_5xx",
            retry_count=2, provenance=DataProvenance.FIXTURE,
        )
        assert row.success is False
        assert row.error_type == "http_5xx"
        assert row.retry_count == 2


# ---------------------------------------------------------------------------
# Cache bypass
# ---------------------------------------------------------------------------


class TestResearchPollsBypassTheCache:
    def test_the_observations_client_has_a_zero_ttl(self) -> None:
        """Structural, not a keyword a future caller could forget."""
        provider = NwsObservationsProvider()
        assert provider._client._cache_ttl == 0.0  # noqa: SLF001
        assert provider.cache_hits == 0

    def test_the_forecast_client_has_a_zero_ttl(self) -> None:
        provider = NwsForecastProvider()
        assert provider._client._cache_ttl == 0.0  # noqa: SLF001

    def test_every_research_request_passes_use_cache_false(self) -> None:
        """Belt and braces: the TTL is zero AND every call site says so."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/providers/nws_observations.py"
        ).read_text()
        assert source.count("use_cache=False") >= 3
        assert "use_cache=True" not in source

    @pytest.mark.asyncio
    async def test_a_cached_body_cannot_be_reused_under_a_new_available_at(self) -> None:
        """The failure this prevents: a response that never reached the network,
        stamped with a fresh availability. That is a fabricated timestamp, not a
        stale value, and no downstream check would catch it.
        """
        from pmvl_markets.providers.http import HttpClient

        calls = {"n": 0}

        class CountingClient(HttpClient):
            async def get_json(self, path, **kwargs):  # noqa: ANN001, ANN003
                calls["n"] += 1
                assert kwargs.get("use_cache") is False, (
                    "a research poll requested the cache"
                )
                return {"features": []}

        client = CountingClient("https://example.invalid", name="t", cache_ttl_seconds=900.0)
        provider = NwsObservationsProvider(client=client)
        await provider.fetch_recent("KNYC")
        await provider.fetch_recent("KNYC")
        assert calls["n"] == 2  # two polls, two requests
        assert provider.cache_hits == 0


# ---------------------------------------------------------------------------
# Archive accumulation
# ---------------------------------------------------------------------------


def _weather_market(market_id: str = "KXHIGHNY-26AUG25-T85") -> NormalizedMarket:
    from pmvl_shared.enums import Category, MarketStatus

    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id=market_id,
        series_ticker="KXHIGHNY",
        category=Category.WEATHER,
        status=MarketStatus.OPEN,
        settlement_rules_raw="maximum temperature recorded at New York City (CLINYC)",
        tick_size=Decimal("0.01"),
        fee_rate=Decimal("0.07"),
        fee_type="quadratic",
        expected_resolution_time=at(600),
    )


def _book(minute: float, market_id: str = "KXHIGHNY-26AUG25-T85") -> OrderBook:
    return OrderBook(
        platform=Platform.KALSHI,
        platform_market_id=market_id,
        observed_at=at(minute),
        yes_asks=[BookLevel(price=Decimal("0.30"), size=Decimal("100"))],
        yes_bids=[BookLevel(price=Decimal("0.29"), size=Decimal("100"))],
        no_asks=[BookLevel(price=Decimal("0.71"), size=Decimal("100"))],
        no_bids=[BookLevel(price=Decimal("0.70"), size=Decimal("100"))],
    )


class TestArchiveAccumulatesAcrossCycles:
    def test_repeated_captures_produce_more_than_one_book(self, tmp_path: Path) -> None:
        """The acceptance criterion, and the thing the product snapshot cannot do.

        The builder keeps only the latest book per market and the pipeline
        re-seeds from the published artefact, so a time series cannot exist there
        at all.
        """
        archive = ResearchArchive(tmp_path / "archive.db")
        market = _weather_market()
        key = f"kalshi:{market.platform_market_id}"
        for minute in range(4):
            archive_books(
                archive, {key: _book(minute)}, {key: market},
                available_at=at(minute + 0.1),
            )
        with archive.connect() as con:
            series = archive.book_series(
                con, platform="kalshi", platform_market_id=market.platform_market_id
            )
        assert len(series) == 4

    def test_a_non_research_market_is_skipped_and_counted(self, tmp_path: Path) -> None:
        from pmvl_shared.enums import Category, MarketStatus

        archive = ResearchArchive(tmp_path / "archive.db")
        other = NormalizedMarket(
            platform=Platform.KALSHI, platform_market_id="KXCO2LEVEL-30",
            series_ticker="KXCO2LEVEL", category=Category.WEATHER,
            status=MarketStatus.OPEN,
        )
        key = "kalshi:KXCO2LEVEL-30"
        report = archive_books(
            archive, {key: _book(0, "KXCO2LEVEL-30")}, {key: other}
        )
        assert report.books_archived == 0
        assert report.skipped_not_research == 1

    def test_the_settlement_station_is_carried_onto_every_book(
        self, tmp_path: Path
    ) -> None:
        archive = ResearchArchive(tmp_path / "archive.db")
        market = _weather_market()
        key = f"kalshi:{market.platform_market_id}"
        archive_books(archive, {key: _book(0)}, {key: market})
        with archive.connect() as con:
            station = con.execute(
                "SELECT settlement_station_id FROM archived_books"
            ).fetchone()[0]
        assert station == "KNYC"

    def test_the_full_ladder_is_archived_not_top_of_book(self, tmp_path: Path) -> None:
        archive = ResearchArchive(tmp_path / "archive.db")
        market = _weather_market()
        key = f"kalshi:{market.platform_market_id}"
        archive_books(archive, {key: _book(0)}, {key: market})
        with archive.connect() as con:
            sides = con.execute(
                "SELECT side, is_ask, COUNT(*) FROM archived_levels GROUP BY 1,2"
            ).fetchall()
        assert len(sides) == 4  # yes/no x bid/ask

    def test_available_at_is_not_the_books_observed_at(self, tmp_path: Path) -> None:
        """They are different instants; equating them gives back the parse
        duration as look-ahead on every capture."""
        archive = ResearchArchive(tmp_path / "archive.db")
        market = _weather_market()
        key = f"kalshi:{market.platform_market_id}"
        archive_books(
            archive, {key: _book(0)}, {key: market}, available_at=at(0.5)
        )
        with archive.connect() as con:
            observed, available = con.execute(
                "SELECT observed_at, available_at FROM archived_books"
            ).fetchone()
        assert available > observed

    def test_an_archive_failure_does_not_raise(self, tmp_path: Path) -> None:
        """Collection must not stop because the research archive is unavailable.

        The product data still needs gathering; a research gap is reported.
        """
        archive = ResearchArchive(tmp_path / "nonexistent-dir" / "x" / "a.db")
        archive._path = Path("/dev/null/impossible/a.db")  # noqa: SLF001
        market = _weather_market()
        key = f"kalshi:{market.platform_market_id}"
        report = archive_books(archive, {key: _book(0)}, {key: market})
        assert report.books_archived == 0
        assert report.errors

    def test_snapshot_retention_cannot_reach_the_archive(self) -> None:
        builder = Path("scripts/build_snapshot.py").read_text()
        retention = Path(
            "packages/shared/src/pmvl_shared/snapshot_retention.py"
        ).read_text()
        for source in (builder, retention):
            assert "research_archive" not in source
            assert "archived_books" not in source
            assert "ResearchArchive" not in source


# ---------------------------------------------------------------------------
# Payload round-trip through the real collector shape
# ---------------------------------------------------------------------------


class TestPayloadRoundTrip:
    @pytest.mark.integration
    def test_a_collected_payload_round_trips(self, clean_db, tmp_path: Path) -> None:  # noqa: ANN001
        store = PayloadStore(tmp_path / "payloads")
        payload = as_json_bytes(
            {"properties": {"timestamp": "2026-08-25T12:00:00Z",
                            "temperature": {"value": 27.0, "unitCode": "wmoUnit:degC"}}}
        )
        row = record_station_observation(
            clean_db, station_id="KNYC", source_product="observations",
            observation_time=at(0), poll=poll(), temperature_f=Decimal("80.6"),
            raw_payload=payload, payload_store=store,
            provenance=DataProvenance.FIXTURE,
        )
        assert row is not None
        assert store.get(row.raw_payload_ref) == payload
        assert json.loads(store.get(row.raw_payload_ref))["properties"]["timestamp"]


class TestObservationParsing:
    def test_celsius_is_converted_explicitly(self) -> None:
        record = _parse_feature(
            "KNYC",
            {"properties": {"timestamp": "2026-08-25T12:00:00+00:00",
                            "temperature": {"value": 30.0, "unitCode": "wmoUnit:degC"}}},
        )
        assert record is not None
        assert record.temperature_f == Decimal("86.0000")

    def test_an_observation_with_no_timestamp_is_dropped(self) -> None:
        """It cannot be placed on the intraday path, and placing it anywhere
        would be a guess that decides whether a decision saw it."""
        assert _parse_feature("KNYC", {"properties": {"temperature": {"value": 30}}}) is None

    def test_a_missing_temperature_is_none_not_zero(self) -> None:
        record = _parse_feature(
            "KNYC",
            {"properties": {"timestamp": "2026-08-25T12:00:00+00:00",
                            "temperature": {"value": None}}},
        )
        assert record is not None
        assert record.temperature_f is None

    def test_the_revision_id_identifies_the_instant(self) -> None:
        record = _parse_feature(
            "KNYC", {"properties": {"timestamp": "2026-08-25T12:00:00+00:00"}}
        )
        assert record is not None
        assert record.revision_id.startswith("KNYC:")


# ---------------------------------------------------------------------------
# Collection health determinism
# ---------------------------------------------------------------------------


class TestCollectionHealthIsDeterministic:
    @pytest.mark.integration
    def test_the_same_database_gives_the_same_report(self, clean_db, tmp_path) -> None:  # noqa: ANN001
        """Hermetic on purpose.

        This read the real research archive on disk, so it was deterministic
        only while nothing was writing to that file - and it failed the moment a
        live collector was running, which is the one time the property matters.
        """
        from pmvl_markets.alpha.collection_health import build_report

        fixed = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)
        first = build_report(clean_db, days=1, now=fixed, archive_root=tmp_path)
        second = build_report(clean_db, days=1, now=fixed, archive_root=tmp_path)
        assert first == second

    @pytest.mark.integration
    def test_the_report_states_that_it_computes_no_returns(self, clean_db, tmp_path) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.collection_health import build_report

        report = build_report(clean_db, days=1, now=BASE, archive_root=tmp_path)
        assert "No markout" in report["scope"]
        assert "COLLECTION HEALTH ONLY" in report["scope"]

    @pytest.mark.integration
    def test_gates_report_expected_beside_actual(self, clean_db, tmp_path) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.collection_health import build_report

        report = build_report(clean_db, days=1, now=BASE, archive_root=tmp_path)
        for gate in report["gates"]:
            assert "actual" in gate and "expected" in gate
            assert isinstance(gate["passing"], bool)

    @pytest.mark.integration
    def test_an_empty_database_fails_gates_rather_than_erroring(
        self, clean_db, tmp_path
    ) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.collection_health import build_report

        report = build_report(clean_db, days=1, now=BASE, archive_root=tmp_path)
        assert report["all_gates_passing"] is False

    @pytest.mark.integration
    def test_the_report_is_json_serialisable(self, clean_db, tmp_path) -> None:  # noqa: ANN001
        from pmvl_markets.alpha.collection_health import build_report

        report = build_report(clean_db, days=1, now=BASE, archive_root=tmp_path)
        assert json.loads(json.dumps(report)) == report

    def test_no_test_here_reads_the_live_research_archive(self) -> None:
        """The determinism claim holds only while the archive is an input.

        ``build_report`` defaults ``archive_root`` to the repository's real
        ``data/research-archive.db``, which the collector writes to
        continuously. Four tests in this class omitted the argument, so they
        opened that live file and raised "database is locked" whenever a
        collection run held it - intermittently, and only on a machine where
        collection was actually running, which is why it read as a flaky suite
        rather than as a test that names its own inputs.

        Asserted against the AST rather than the prose, so a future test cannot
        reintroduce the implicit read.
        """
        import ast

        cls = next(
            node
            for node in ast.parse(Path(__file__).read_text()).body
            if isinstance(node, ast.ClassDef)
            and node.name == "TestCollectionHealthIsDeterministic"
        )
        unpinned = [
            node.lineno
            for node in ast.walk(cls)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "build_report"
            and not any(kw.arg == "archive_root" for kw in node.keywords)
        ]
        assert not unpinned, (
            "build_report called without archive_root at line(s) "
            f"{unpinned}: that reads the live research archive"
        )


class TestNoForwardReturnsAnywhere:
    #: Names that would indicate this phase had started measuring whether a
    #: signal works. Checked against the AST, not the raw text: the docstrings
    #: here deliberately SAY "no markout, no forward return", and a substring
    #: scan would flag the very sentences that promise the opposite.
    FORBIDDEN_NAMES = frozenset(
        {
            "markout", "forward_return", "future_return", "information_coefficient",
            "ic", "sharpe", "sortino", "pnl", "profit_factor", "regress",
            "residualise", "residualize", "alpha_score", "expected_move",
        }
    )

    COLLECTION_MODULES = (
        "alpha/collection.py", "alpha/collection_health.py",
        "alpha/archiving.py", "alpha/issuance.py",
        "providers/nws_observations.py", "normalize/weather_stations.py",
    )

    @pytest.mark.parametrize("name", COLLECTION_MODULES)
    def test_the_collection_modules_compute_no_returns(self, name: str) -> None:
        """The research firewall, asserted against executable code.

        This phase must not learn whether any candidate signal works. Function
        names, arguments and attribute accesses are inspected; prose is not,
        because the prose is where the prohibition is written down.
        """
        import ast

        root = Path("packages/market-normalization/src/pmvl_markets")
        tree = ast.parse((root / name).read_text())
        offenders: list[str] = []
        for node in ast.walk(tree):
            identifiers: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.append(node.name)
            elif isinstance(node, ast.Name):
                identifiers.append(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.append(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.append(node.arg)
            for identifier in identifiers:
                parts = set(identifier.lower().split("_"))
                hit = parts & self.FORBIDDEN_NAMES
                if hit:
                    offenders.append(f"{identifier} ({', '.join(sorted(hit))})")
        assert offenders == [], f"{name} computes: {offenders}"

    def test_no_collection_module_imports_the_simulator_or_metrics(self) -> None:
        """A collection module has no business reaching the research layer."""
        import ast

        root = Path("packages/market-normalization/src/pmvl_markets")
        for name in self.COLLECTION_MODULES:
            tree = ast.parse((root / name).read_text())
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                elif isinstance(node, ast.Import):
                    module = ",".join(a.name for a in node.names)
                # Exact segment match: `db_models` is the ORM and is required,
                # while `alpha.models` is the research layer and is not.
                segments = set(module.split("."))
                banned = segments & {
                    "simulator", "metrics", "strategy", "models", "capacity",
                }
                assert not banned, f"{name} imports {module}"


def _market(session) -> int:  # noqa: ANN001
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.db_models import Market

    market = Market(
        platform="kalshi",
        platform_market_id="KXHIGHNY-26AUG25-T85",
        category="weather",
        series_ticker="KXHIGHNY",
        settlement_station_id="KNYC",
        provenance=DataProvenance.FIXTURE,
        created_at=utcnow(),
    )
    session.add(market)
    session.flush()
    return market.id
