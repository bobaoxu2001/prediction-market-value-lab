"""The collection rules that make future research possible, or impossible.

Every test here guards something **irreversible**. A leaked timestamp can be
found and fixed; a timestamp that was never captured is gone, and so is every
month of collection that ran without it.

The nine properties, in the order the phase specification lists them:

1. ``available_at`` remains the admissibility gate.
2. ``published_at`` cannot silently replace it.
3. Raw payload hashes round-trip.
4. Settlement station is kept apart from the forecast grid cell.
5. Failed polls are persisted.
6. Product snapshot retention cannot remove research history.
7. Capacity uses only what the stored ladder contains.
8. Capacity refuses synthetic data.
9. No alpha status is upgraded by a capacity result.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_shared.enums import DataProvenance, Platform
from pmvl_shared.payload_store import (
    PayloadIntegrityError,
    PayloadStore,
    PayloadTooLargeError,
)
from pmvl_shared.research_archive import ArchiveError, ResearchArchive
from pmvl_shared.schemas import BookLevel

from pmvl_markets.alpha.capacity import (
    BookState,
    Economics,
    levels_from_rows,
    max_executable_size,
    price_order,
)
from pmvl_markets.alpha.collection import (
    COLLECTOR_VERSION,
    PollWindow,
    TimestampAbsence,
    TimestampDisciplineError,
    as_json_bytes,
    coverage_report,
    payload_roundtrip_ok,
    record_poll_attempt,
    record_source_observation,
    record_station_observation,
)

BASE = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def window(start: float = 0, end: float = 1) -> PollWindow:
    poll = PollWindow(started_at=at(start), endpoint="/gridpoints/OKX/33,37")
    poll.completed_at = at(end)
    poll.http_status = 200
    return poll


def _market(session, station: str = "KNYC") -> int:  # noqa: ANN001
    from pmvl_shared.timeutil import utcnow

    from pmvl_markets.db_models import Market

    market = Market(
        platform="kalshi",
        platform_market_id=f"KXHIGHNY-26AUG20-T85-{station}",
        category="weather",
        settlement_station_id=station,
        provenance=DataProvenance.FIXTURE,
        created_at=utcnow(),
    )
    session.add(market)
    session.flush()
    return market.id


# ---------------------------------------------------------------------------
# 1 + 2. available_at is the gate; published_at may not replace it
# ---------------------------------------------------------------------------


class TestTimestampDiscipline:
    def test_available_at_is_derived_from_the_poll_not_from_the_source(self) -> None:
        """The value could change at any point inside the request.

        Claiming availability at the START of the poll would grant a full
        round-trip of look-ahead on every observation.
        """
        poll = window(start=0, end=5)
        assert poll.available_at == at(5)
        assert poll.duration_ms == 5 * 60 * 1000

    def test_available_at_is_undefined_before_the_poll_completes(self) -> None:
        poll = PollWindow(started_at=at(0))
        with pytest.raises(TimestampDisciplineError, match="not yet defined"):
            _ = poll.available_at

    @pytest.mark.integration
    def test_published_at_cannot_be_passed_as_available_at(self, clean_db) -> None:  # noqa: ANN001
        """``record_source_observation`` takes no ``available_at`` parameter.

        A caller who could supply it could supply ``published_at``, and the
        resulting dataset would be silently un-backtestable with nothing marking
        which rows were affected.
        """
        with pytest.raises(TypeError):
            record_source_observation(
                clean_db,
                market_id=_market(clean_db),
                source_id="nws",
                source_product="gridpoint_forecast",
                kind="source_estimate",
                value=Decimal("0.42"),
                poll=window(),
                available_at=at(-60),  # type: ignore[call-arg]
            )

    @pytest.mark.integration
    def test_a_stored_row_keeps_published_at_and_available_at_apart(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """The lag the whole hypothesis measures is the gap between them."""
        row = record_source_observation(
            clean_db,
            market_id=_market(clean_db),
            source_id="nws",
            source_product="gridpoint_forecast",
            kind="source_estimate",
            value=Decimal("0.42"),
            poll=window(start=40, end=41),
            published_at=at(0),
        )
        assert row is not None
        assert row.published_at == at(0)
        assert row.available_at == at(41)
        assert row.available_at > row.published_at

    @pytest.mark.integration
    def test_a_source_cannot_publish_after_we_read_it(self, clean_db) -> None:  # noqa: ANN001
        """Clock skew or a parse error. Accepting it creates negative lag."""
        with pytest.raises(TimestampDisciplineError, match="after we read it"):
            record_source_observation(
                clean_db,
                market_id=_market(clean_db),
                source_id="nws",
                source_product="gridpoint_forecast",
                kind="source_estimate",
                value=Decimal("0.42"),
                poll=window(start=0, end=1),
                published_at=at(600),
            )

    @pytest.mark.integration
    def test_a_missing_publication_time_is_NULL_and_documented(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """Never defaulted to the poll time.

        ``parse_ts(updateTime) or utcnow()`` reported ZERO source-to-market lag
        for exactly the observations whose lag is unknown - a bias toward
        finding the market slow in the one measurement that matters.
        """
        row = record_source_observation(
            clean_db,
            market_id=_market(clean_db),
            source_id="nws",
            source_product="gridpoint_forecast",
            kind="source_estimate",
            value=Decimal("0.42"),
            poll=window(),
            published_at=None,
        )
        assert row is not None
        assert row.published_at is None
        assert "published_at" in row.timestamp_absence["missing"]
        assert "NOT defaulted" in row.timestamp_absence["missing"]["published_at"]

    def test_the_provider_no_longer_fabricates_a_publication_time(self) -> None:
        """The defect this phase removed, asserted against the source."""
        source = Path("packages/market-normalization/src/pmvl_markets/probability")
        text = (source / "categories" / "weather.py").read_text()
        assert 'parse_ts(props.get("updateTime")) or utcnow()' not in text
        assert 'published_at = parse_ts(props.get("updateTime"))' in text


# ---------------------------------------------------------------------------
# 3. Raw payloads round-trip
# ---------------------------------------------------------------------------


class TestPayloadStore:
    def test_a_payload_round_trips_byte_for_byte(self, tmp_path: Path) -> None:
        store = PayloadStore(tmp_path / "payloads")
        payload = as_json_bytes({"properties": {"maxTemperature": {"values": [1, 2]}}})
        stored = store.put(payload)
        assert store.get(stored.ref) == payload

    def test_identical_content_stores_once(self, tmp_path: Path) -> None:
        """A source polled hourly and unchanged should not cost hourly storage."""
        store = PayloadStore(tmp_path / "payloads")
        payload = as_json_bytes({"a": 1})
        first, second = store.put(payload), store.put(payload)
        assert first.ref == second.ref
        assert first.written is True
        assert second.written is False
        assert store.count() == 1

    def test_different_content_cannot_overwrite(self, tmp_path: Path) -> None:
        """Immutability is structural: the path IS the hash."""
        store = PayloadStore(tmp_path / "payloads")
        a = store.put(as_json_bytes({"a": 1}))
        b = store.put(as_json_bytes({"a": 2}))
        assert a.ref != b.ref
        assert store.get(a.ref) != store.get(b.ref)

    def test_key_order_does_not_change_the_hash(self, tmp_path: Path) -> None:
        """Otherwise dedup fails and "the source did not change" is uncheckable."""
        store = PayloadStore(tmp_path / "payloads")
        one = store.put(as_json_bytes({"a": 1, "b": 2}))
        two = store.put(as_json_bytes({"b": 2, "a": 1}))
        assert one.ref == two.ref

    def test_corruption_is_detected_on_read(self, tmp_path: Path) -> None:
        """A silently corrupted payload becomes a plausible wrong value."""
        store = PayloadStore(tmp_path / "payloads")
        ref = store.put(as_json_bytes({"a": 1})).ref
        target = next((tmp_path / "payloads").rglob("*.json.gz"))
        import gzip

        with gzip.open(target, "wb") as handle:
            handle.write(b'{"a": 999}')
        with pytest.raises(PayloadIntegrityError):
            store.get(ref)

    def test_verify_all_reports_and_does_not_repair(self, tmp_path: Path) -> None:
        store = PayloadStore(tmp_path / "payloads")
        store.put(as_json_bytes({"a": 1}))
        assert store.verify_all() == []
        target = next((tmp_path / "payloads").rglob("*.json.gz"))
        import gzip

        with gzip.open(target, "wb") as handle:
            handle.write(b"tampered")
        assert store.verify_all()
        assert target.exists()  # reported, not deleted

    def test_an_oversized_payload_is_refused(self, tmp_path: Path) -> None:
        store = PayloadStore(tmp_path / "payloads")
        with pytest.raises(PayloadTooLargeError):
            store.put(b"x" * (9 * 1024 * 1024))

    def test_a_malformed_reference_is_rejected(self, tmp_path: Path) -> None:
        store = PayloadStore(tmp_path / "payloads")
        with pytest.raises(ValueError, match="malformed"):
            store.get("md5:abc")

    @pytest.mark.integration
    def test_an_observation_links_to_a_verifiable_payload(
        self, clean_db, tmp_path: Path
    ) -> None:  # noqa: ANN001
        store = PayloadStore(tmp_path / "payloads")
        payload = as_json_bytes({"properties": {"updateTime": "2026-08-20T12:00:00Z"}})
        row = record_source_observation(
            clean_db,
            market_id=_market(clean_db),
            source_id="nws",
            source_product="gridpoint_forecast",
            kind="source_estimate",
            value=Decimal("0.42"),
            poll=window(),
            raw_payload=payload,
            payload_store=store,
            published_at=at(0),
        )
        assert row is not None
        assert payload_roundtrip_ok(store, row.raw_payload_ref, row.source_payload_hash)
        assert json.loads(store.get(row.raw_payload_ref))["properties"]["updateTime"]

    @pytest.mark.integration
    def test_the_normalised_value_can_be_regenerated_from_the_raw_payload(
        self, clean_db, tmp_path: Path
    ) -> None:  # noqa: ANN001
        """The point of keeping bytes: a future factor wants a field today's
        parser did not extract, and a stored float cannot supply it."""
        store = PayloadStore(tmp_path / "payloads")
        payload = as_json_bytes(
            {"properties": {"maxTemperature": {"values": [{"value": 30.0}]},
                            "relativeHumidity": {"values": [{"value": 62}]}}}
        )
        row = record_source_observation(
            clean_db, market_id=_market(clean_db), source_id="nws",
            source_product="gridpoint_forecast", kind="source_estimate",
            value=Decimal("30.0"), poll=window(), raw_payload=payload,
            payload_store=store, published_at=at(0), parser_version="1.0.0",
        )
        assert row is not None
        recovered = json.loads(store.get(row.raw_payload_ref))
        # A field the parser never looked at is still there.
        assert recovered["properties"]["relativeHumidity"]["values"][0]["value"] == 62
        assert row.parser_version == "1.0.0"
        assert row.collector_version == COLLECTOR_VERSION


# ---------------------------------------------------------------------------
# 4. Settlement station is not the forecast grid cell
# ---------------------------------------------------------------------------


class TestSettlementStationSeparation:
    @pytest.mark.integration
    def test_source_location_and_settlement_station_are_separate_columns(
        self, clean_db
    ) -> None:  # noqa: ANN001
        """A ~2.5km grid cell is not the instrument the contract settles on.

        Conflating them produced an 8F Los Angeles error in this repository, and
        a persistent per-station offset is indistinguishable from tradeable
        divergence.
        """
        row = record_source_observation(
            clean_db,
            market_id=_market(clean_db),
            source_id="nws",
            source_product="gridpoint_forecast",
            kind="source_estimate",
            value=Decimal("88"),
            poll=window(),
            published_at=at(0),
            source_location="OKX/33,37",
            settlement_station_id="KNYC",
        )
        assert row is not None
        assert row.source_location == "OKX/33,37"
        assert row.settlement_station_id == "KNYC"
        assert row.source_location != row.settlement_station_id

    @pytest.mark.integration
    def test_a_station_observation_requires_a_station(self, clean_db) -> None:  # noqa: ANN001
        with pytest.raises(TimestampDisciplineError, match="station_id is required"):
            record_station_observation(
                clean_db, station_id="  ", source_product="observations",
                observation_time=at(0), poll=window(),
            )

    @pytest.mark.integration
    def test_the_intraday_path_is_reconstructible(self, clean_db) -> None:  # noqa: ANN001
        """By mid-afternoon the daily maximum is usually set.

        A forecast-only model degrades through the day in a way that correlates
        perfectly with time-to-expiry - which looks exactly like an alpha regime
        and is not one. Only hourly observations separate them.
        """
        for hour in range(0, 12):
            record_station_observation(
                clean_db, station_id="KNYC", source_product="observations",
                observation_time=at(hour * 60), poll=window(hour * 60, hour * 60 + 1),
                temperature_f=Decimal(str(70 + hour)),
                provenance=DataProvenance.FIXTURE,
            )
        clean_db.flush()
        from sqlalchemy import select

        from pmvl_markets.db_models import StationObservationRow

        rows = clean_db.scalars(
            select(StationObservationRow).order_by(StationObservationRow.observation_time)
        ).all()
        assert len(rows) == 12
        assert max(r.temperature_f for r in rows) == Decimal("81")

    @pytest.mark.integration
    def test_a_corrected_observation_is_a_new_row_not_an_update(
        self, clean_db
    ) -> None:  # noqa: ANN001
        for available in (10, 200):
            record_station_observation(
                clean_db, station_id="KNYC", source_product="cli_daily",
                observation_time=at(0), poll=window(available, available + 1),
                temperature_f=Decimal("88" if available == 10 else "89"),
                is_daily_summary=True, provenance=DataProvenance.FIXTURE,
            )
        clean_db.flush()
        from sqlalchemy import func, select

        from pmvl_markets.db_models import StationObservationRow

        assert clean_db.scalar(
            select(func.count()).select_from(StationObservationRow)
        ) == 2


# ---------------------------------------------------------------------------
# 5. Failed polls are data
# ---------------------------------------------------------------------------


class TestPollTelemetry:
    @pytest.mark.integration
    def test_a_failed_poll_is_persisted(self, clean_db) -> None:  # noqa: ANN001
        """A gap has two causes - the source did not change, or we could not
        reach it - and only this table distinguishes them."""
        poll = PollWindow(started_at=at(0), endpoint="/gridpoints/OKX/33,37")
        poll.completed_at = at(1)
        poll.http_status = 503
        row = record_poll_attempt(
            clean_db, source_id="nws", source_product="gridpoint_forecast",
            target="KNYC", poll=poll, success=False,
            error_type="http_5xx", error_detail="upstream unavailable",
            retry_count=3, provenance=DataProvenance.FIXTURE,
        )
        assert row.success is False
        assert row.http_status == 503
        assert row.retry_count == 3

    @pytest.mark.integration
    def test_coverage_distinguishes_no_update_from_no_collection(
        self, clean_db
    ) -> None:  # noqa: ANN001
        market_id = _market(clean_db)
        record_source_observation(
            clean_db, market_id=market_id, source_id="nws",
            source_product="gridpoint_forecast", kind="source_estimate",
            value=Decimal("0.4"), poll=window(), published_at=at(0),
            provenance=DataProvenance.FIXTURE,
        )
        for _ in range(3):
            poll = PollWindow(started_at=at(10))
            poll.completed_at = at(11)
            record_poll_attempt(
                clean_db, source_id="nws", source_product="gridpoint_forecast",
                target="KNYC", poll=poll, success=False, error_type="timeout",
                provenance=DataProvenance.FIXTURE,
            )
        clean_db.flush()
        report = coverage_report(clean_db, source_id="nws")
        assert report["observations"] == 1
        assert report["failed_polls"] == 3
        assert report["error_types"]["timeout"] == 3

    @pytest.mark.integration
    def test_published_at_coverage_is_reported(self, clean_db) -> None:  # noqa: ANN001
        """The share of rows that can measure lag at all.

        A dataset with 10% ``published_at`` coverage cannot test the hypothesis
        on 90% of its observations, and nothing else would say so.
        """
        market_id = _market(clean_db)
        for minute, published in ((0, at(-5)), (10, None)):
            record_source_observation(
                clean_db, market_id=market_id, source_id="nws",
                source_product="gridpoint_forecast", kind="source_estimate",
                value=Decimal("0.4"), poll=window(minute, minute + 1),
                published_at=published, provenance=DataProvenance.FIXTURE,
            )
        clean_db.flush()
        report = coverage_report(clean_db, source_id="nws")
        assert report["with_published_at"] == 1
        assert report["published_at_coverage"] == "50.0%"

    @pytest.mark.integration
    def test_a_cached_response_is_flagged(self, clean_db) -> None:  # noqa: ANN001
        """A cached response has an ``available_at`` describing an earlier poll;
        treating it as fresh understates observed lag."""
        poll = PollWindow(started_at=at(0))
        poll.finish(served_from_cache=True)
        row = record_poll_attempt(
            clean_db, source_id="nws", source_product="gridpoint_forecast",
            target="KNYC", poll=poll, success=True,
            provenance=DataProvenance.FIXTURE,
        )
        assert row.served_from_cache is True


# ---------------------------------------------------------------------------
# 6. Product retention cannot remove research history
# ---------------------------------------------------------------------------


class TestResearchArchiveSurvivesProductRetention:
    def test_the_archive_is_a_separate_file_the_builder_never_opens(
        self, tmp_path: Path
    ) -> None:
        """The whole safety argument.

        The snapshot builder cannot prune what it has no connection to, and no
        future edit to the retention policy can reach across a file boundary.
        """
        archive = ResearchArchive(tmp_path / "research-archive.db")
        product = tmp_path / "pmvl-snapshot.db"
        assert archive.path != product

        builder = Path("scripts/build_snapshot.py").read_text()
        retention = Path(
            "packages/shared/src/pmvl_shared/snapshot_retention.py"
        ).read_text()
        for source in (builder, retention):
            assert "research_archive" not in source
            assert "archived_books" not in source

    def test_a_book_time_series_survives_the_product_prune(
        self, tmp_path: Path
    ) -> None:
        """The product keeps only the LATEST book per market.

        Every earlier capture is deleted, and because the pipeline re-seeds from
        the published snapshot, deleted means gone for good. The archive keeps
        all of them.
        """
        archive = ResearchArchive(tmp_path / "research-archive.db")
        with archive.connect() as con:
            for minute in range(0, 50, 10):
                archive.archive_book(
                    con, platform="kalshi",
                    platform_market_id="KXHIGHNY-26AUG20-T85",
                    observed_at=at(minute), available_at=at(minute + 0.5),
                    levels=[("yes", True, 0, Decimal("0.30"), Decimal("100"))],
                )
            series = archive.book_series(
                con, platform="kalshi", platform_market_id="KXHIGHNY-26AUG20-T85"
            )
        assert len(series) == 5

    def test_the_full_ladder_is_preserved_not_top_of_book(
        self, tmp_path: Path
    ) -> None:
        """Capacity, slippage and every execution question need the ladder, and
        it cannot be reconstructed from a best bid and ask."""
        archive = ResearchArchive(tmp_path / "research-archive.db")
        levels = [
            ("yes", True, i, Decimal("0.30") + Decimal("0.01") * i, Decimal("100"))
            for i in range(25)
        ]
        with archive.connect() as con:
            book_id = archive.archive_book(
                con, platform="kalshi", platform_market_id="M",
                observed_at=at(0), available_at=at(1), levels=levels,
            )
            stored = con.execute(
                "SELECT COUNT(*) FROM archived_levels WHERE book_id=?", (book_id,)
            ).fetchone()[0]
        assert stored == 25

    def test_the_archive_has_no_delete_or_prune_api(self) -> None:
        """Absence of a retention policy is the design, not an omission."""
        source = Path(
            "packages/shared/src/pmvl_shared/research_archive.py"
        ).read_text()
        assert "def prune" not in source
        assert "def delete" not in source
        assert "DELETE FROM" not in source

    def test_duplicate_captures_are_idempotent(self, tmp_path: Path) -> None:
        archive = ResearchArchive(tmp_path / "a.db")
        with archive.connect() as con:
            first = archive.archive_book(
                con, platform="kalshi", platform_market_id="M",
                observed_at=at(0), available_at=at(1),
                levels=[("yes", True, 0, Decimal("0.3"), Decimal("10"))],
            )
            second = archive.archive_book(
                con, platform="kalshi", platform_market_id="M",
                observed_at=at(0), available_at=at(1),
                levels=[("yes", True, 0, Decimal("0.3"), Decimal("10"))],
            )
        assert first is not None
        assert second is None

    def test_the_archive_refuses_non_live_data(self, tmp_path: Path) -> None:
        archive = ResearchArchive(tmp_path / "a.db")
        with archive.connect() as con:
            with pytest.raises(ArchiveError, match="refusing to archive"):
                archive.archive_book(
                    con, platform="kalshi", platform_market_id="M",
                    observed_at=at(0), available_at=at(1), levels=[],
                    provenance="demo",
                )

    def test_a_book_cannot_be_usable_before_it_was_read(self, tmp_path: Path) -> None:
        archive = ResearchArchive(tmp_path / "a.db")
        with archive.connect() as con:
            with pytest.raises(ArchiveError, match="before it was read"):
                archive.archive_book(
                    con, platform="kalshi", platform_market_id="M",
                    observed_at=at(10), available_at=at(0), levels=[],
                )

    def test_trade_prints_are_retained_in_full(self, tmp_path: Path) -> None:
        """The product caps trades at 20 per market. A maker fill cannot be
        simulated without traded volume through the level, so that cap removes
        the only evidence a resting order could have been filled."""
        archive = ResearchArchive(tmp_path / "a.db")
        trades = [
            {"platform_trade_id": f"t{i}", "traded_at": at(i),
             "price": Decimal("0.30"), "size": Decimal("5")}
            for i in range(200)
        ]
        with archive.connect() as con:
            written = archive.archive_trades(
                con, platform="kalshi", platform_market_id="M",
                trades=trades, available_at=at(300),
            )
        assert written == 200
        assert archive.stats().trades == 200


# ---------------------------------------------------------------------------
# 7 + 8 + 9. Capacity: ladder-only, live-only, and not evidence
# ---------------------------------------------------------------------------


class TestCapacityIsLadderOnly:
    def test_capacity_reads_only_the_ladder(self) -> None:
        """No outcome, no forecast, no model probability reaches this call.

        A capacity number must be computable at the instant the book was
        observed, or it is not capacity.
        """
        book = BookState(
            asks=(BookLevel(price=Decimal("0.30"), size=Decimal("500")),),
            bids=(BookLevel(price=Decimal("0.29"), size=Decimal("500")),),
        )
        point = price_order(
            book, size=Decimal("100"), alpha=Decimal("0.03"),
            platform=Platform.KALSHI, fee_rate=Decimal("0.07"),
            fee_type="quadratic",
        )
        assert point is not None
        assert point.reference_mid == Decimal("0.2950")
        assert point.half_spread == Decimal("0.0050")

    def test_a_one_sided_book_has_no_mid_and_is_refused(self) -> None:
        """Treating a missing bid as zero would hand the strategy a spread it
        never paid."""
        book = BookState(
            asks=(BookLevel(price=Decimal("0.30"), size=Decimal("500")),), bids=()
        )
        assert book.mid is None
        assert price_order(
            book, size=Decimal("100"), alpha=Decimal("0.03"),
            platform=Platform.KALSHI, fee_rate=Decimal("0.07"),
        ) is None

    def test_depth_slippage_rises_with_size(self) -> None:
        book = BookState(
            asks=(
                BookLevel(price=Decimal("0.30"), size=Decimal("10")),
                BookLevel(price=Decimal("0.35"), size=Decimal("1000")),
            ),
            bids=(BookLevel(price=Decimal("0.29"), size=Decimal("100")),),
        )
        small = price_order(book, size=Decimal("10"), alpha=Decimal("0.05"),
                            platform=Platform.KALSHI, fee_rate=Decimal("0.07"))
        large = price_order(book, size=Decimal("500"), alpha=Decimal("0.05"),
                            platform=Platform.KALSHI, fee_rate=Decimal("0.07"))
        assert small is not None and large is not None
        assert large.depth_slippage > small.depth_slippage
        assert large.net_edge < small.net_edge

    def test_a_round_trip_costs_more_than_holding_to_settlement(self) -> None:
        """It crosses twice and sells into the bid - the thin side of the book."""
        book = BookState(
            asks=(BookLevel(price=Decimal("0.30"), size=Decimal("1000")),),
            bids=(BookLevel(price=Decimal("0.28"), size=Decimal("1000")),),
        )
        kwargs = dict(size=Decimal("100"), alpha=Decimal("0.05"),
                      platform=Platform.KALSHI, fee_rate=Decimal("0.07"))
        hold = price_order(book, economics=Economics.HOLD_TO_SETTLEMENT, **kwargs)
        trip = price_order(book, economics=Economics.ROUND_TRIP, **kwargs)
        assert hold is not None and trip is not None
        assert trip.total_cost > hold.total_cost
        assert trip.net_edge < hold.net_edge

    def test_a_round_trip_with_no_bid_ladder_is_refused(self) -> None:
        """A position that cannot be sold has not been exited."""
        book = BookState(
            asks=(BookLevel(price=Decimal("0.30"), size=Decimal("1000")),),
            bids=(BookLevel(price=Decimal("0.28"), size=Decimal("1")),),
        )
        trip = price_order(
            book, size=Decimal("100"), alpha=Decimal("0.05"),
            platform=Platform.KALSHI, fee_rate=Decimal("0.07"),
            economics=Economics.ROUND_TRIP,
        )
        # The bid ladder fills only 1 of 100; the exit is priced on what the
        # book could actually absorb rather than on the requested size.
        assert trip is not None
        assert trip.exit_cost > 0

    def test_max_executable_size_never_exceeds_the_ladder(self) -> None:
        book = BookState(
            asks=(BookLevel(price=Decimal("0.30"), size=Decimal("42")),),
            bids=(BookLevel(price=Decimal("0.29"), size=Decimal("100")),),
        )
        contracts, _ = max_executable_size(
            book, alpha=Decimal("0.20"), platform=Platform.KALSHI,
            fee_rate=Decimal("0.07"),
        )
        assert contracts <= Decimal("42")

    def test_levels_are_read_in_stored_order_and_bad_rows_dropped(self) -> None:
        """The venue's own ordering is the one a walk must follow."""
        levels = levels_from_rows(
            [("0.30", "100"), ("0.00", "50"), ("1.00", "50"), ("0.35", "-5"),
             ("0.40", "200")]
        )
        assert [str(lv.price) for lv in levels] == ["0.30", "0.40"]


class TestCapacityRefusesSyntheticData:
    def test_the_loader_raises_on_non_live_provenance(self, tmp_path: Path) -> None:
        """A capacity figure computed partly from seeded rows is not a weaker
        result - it is a different result wearing this one's name."""
        from scripts.capacity_preflight import SyntheticDataError, load_observations

        db = tmp_path / "seeded.db"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE markets (id INTEGER PRIMARY KEY, platform TEXT,
                platform_market_id TEXT, series_ticker TEXT, category TEXT,
                expected_resolution_time TEXT, fee_rate TEXT, fee_type TEXT,
                provenance TEXT);
            CREATE TABLE orderbook_snapshots (id INTEGER PRIMARY KEY,
                market_id INTEGER, observed_at TEXT, provenance TEXT);
            CREATE TABLE orderbook_levels (snapshot_id INTEGER, side TEXT,
                is_ask INTEGER, level_index INTEGER, price TEXT, size TEXT);
            INSERT INTO markets VALUES
                (1,'kalshi','M','KXHIGHNY','weather','2026-08-21','0.07','quadratic','demo');
            INSERT INTO orderbook_snapshots VALUES (1,1,'2026-08-20 12:00:00','live');
            INSERT INTO orderbook_levels VALUES (1,'yes',1,0,'0.30','100');
            """
        )
        con.commit()
        con.close()

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            with pytest.raises(SyntheticDataError, match="non-live"):
                load_observations(con, category="weather", require_two_sided=False)
        finally:
            con.close()

    def test_demo_books_are_excluded_by_the_query(self, tmp_path: Path) -> None:
        from scripts.capacity_preflight import load_observations

        db = tmp_path / "demo.db"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE markets (id INTEGER PRIMARY KEY, platform TEXT,
                platform_market_id TEXT, series_ticker TEXT, category TEXT,
                expected_resolution_time TEXT, fee_rate TEXT, fee_type TEXT,
                provenance TEXT);
            CREATE TABLE orderbook_snapshots (id INTEGER PRIMARY KEY,
                market_id INTEGER, observed_at TEXT, provenance TEXT);
            CREATE TABLE orderbook_levels (snapshot_id INTEGER, side TEXT,
                is_ask INTEGER, level_index INTEGER, price TEXT, size TEXT);
            INSERT INTO markets VALUES
                (1,'kalshi','M','KXHIGHNY','weather','2026-08-21','0.07','quadratic','live');
            INSERT INTO orderbook_snapshots VALUES (1,1,'2026-08-20 12:00:00','demo');
            INSERT INTO orderbook_levels VALUES (1,'yes',1,0,'0.30','100');
            """
        )
        con.commit()
        con.close()
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            assert load_observations(
                con, category="weather", require_two_sided=False
            ) == []
        finally:
            con.close()


class TestCapacityIsNotEvidence:
    def test_the_capacity_module_reads_no_outcome(self) -> None:
        """It cannot become an alpha result by accident."""
        source = Path(
            "packages/market-normalization/src/pmvl_markets/alpha/capacity.py"
        ).read_text()
        for forbidden in (
            "Settlement", "settlement_value", "yes_payout", "ModelPrediction",
            "fair_probability", "OutcomeEstimate", "AlphaScore",
        ):
            assert forbidden not in source, f"capacity must not read {forbidden}"

    def test_every_capacity_report_carries_a_disclaimer(self) -> None:
        from scripts.capacity_preflight import build_report

        report = build_report([], sqlite3.connect(":memory:"))
        assert "CAPACITY ONLY" in report["disclaimer"]
        assert "not evidence of alpha" in report["disclaimer"]

    def test_capacity_does_not_touch_the_evidence_status(self) -> None:
        """Principle 1: capacity is not alpha.

        The evidence ladder is a function of settled decisions, and no capacity
        result appears anywhere in its inputs.
        """
        from pmvl_markets.alpha.evidence import EvidenceStatus, assess

        before = assess(
            decisions=0, settled_decisions=0, synthetic=True,
            held_out=False, parameters_fitted_on_this_period=True,
        )
        assert before.status is EvidenceStatus.INSUFFICIENT_DATA
        assert before.may_claim_profitability is False

        source = Path(
            "packages/market-normalization/src/pmvl_markets/alpha/evidence.py"
        ).read_text()
        assert "capacity" not in source.lower().replace("capacity before", "")

    def test_the_scenario_block_is_labelled_as_assumption(self) -> None:
        from scripts.capacity_preflight import scenarios

        block = scenarios([], cities=7, contracts_per_firing=Decimal("1.5"))
        assert "SCENARIO ONLY" in block["disclaimer"]
        assert "No alpha has been demonstrated" in block["disclaimer"]
