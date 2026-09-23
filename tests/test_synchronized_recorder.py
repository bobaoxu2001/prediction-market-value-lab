"""The recorder's job is to make cross-snapshot mixing impossible, not unlikely.

Every test here is about a way the previous phase's evidence went wrong: legs read at
different times, a degraded capture laundered into a clean one, an event silently
truncated to fit a request, or observations quietly disappearing from a log.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_markets.providers.http import _pairs
from pmvl_markets.providers.kalshi import MAX_BATCH_TICKERS, KalshiProvider
from pmvl_markets.recording.recorder import (
    RecorderConfig,
    SynchronizedRecorder,
)
from pmvl_markets.recording.schema import (
    STRATEGY_VERSION,
    CaptureMode,
    EventSnapshot,
    RejectionReason,
)
from pmvl_markets.recording.writer import (
    AppendOnlyLog,
    LogIntegrityError,
    read_log,
    verify_log,
)
from pmvl_shared.enums import Platform
from pmvl_shared.schemas import BookLevel, OrderBook

UTC = timezone.utc


def _book(ticker: str, yes_ask: str = "0.40", no_ask: str = "0.62") -> OrderBook:
    return OrderBook(
        platform=Platform.KALSHI,
        platform_market_id=ticker,
        observed_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        yes_asks=[BookLevel(price=Decimal(yes_ask), size=Decimal("500"))],
        no_asks=[BookLevel(price=Decimal(no_ask), size=Decimal("500"))],
        yes_bids=[BookLevel(price=Decimal("0.38"), size=Decimal("500"))],
        no_bids=[BookLevel(price=Decimal("0.60"), size=Decimal("500"))],
    )


def _snapshot(**kw) -> EventSnapshot:
    sent = kw.pop("sent", datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC))
    received = kw.pop("received", sent + timedelta(milliseconds=300))
    books = kw.pop("books", {"A": _book("A"), "B": _book("B")})
    return EventSnapshot(
        event_ticker=kw.pop("event", "EV"),
        platform=Platform.KALSHI,
        request_sent_at=sent,
        response_received_at=received,
        capture_mode=kw.pop("mode", CaptureMode.SINGLE_REQUEST),
        books=books,
        requested_tickers=tuple(books),
        **kw,
    )


# ----------------------------------------------------------- the repeated key
def test_comma_joined_tickers_cannot_express_a_batch_request():
    """The encoding that produced three hours of leg skew.

    A mapping collapses the repeated key, which is why the old code sent a
    comma-joined value - and the venue answers that with one empty book named after
    the joined string, so every leg reads as unquoted and every caller falls back to
    one request per market.
    """
    mapping = {"tickers": "A,B,C"}
    assert _pairs(mapping) == [("tickers", "A,B,C")]

    repeated = [("tickers", "A"), ("tickers", "B"), ("tickers", "C")]
    assert _pairs(repeated) == repeated
    assert len([v for k, v in _pairs(repeated) if k == "tickers"]) == 3


# ------------------------------------------------------------- skew is bounded
def test_skew_bound_is_the_request_round_trip_not_zero():
    snap = _snapshot(sent=datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC),
                     received=datetime(2026, 9, 18, 12, 0, 0, 450000, tzinfo=UTC))
    assert snap.skew_bound_ms == Decimal("450.0")
    assert snap.is_synchronized


def test_a_multi_request_capture_is_never_synchronized():
    snap = _snapshot(mode=CaptureMode.MULTI_REQUEST)
    assert not snap.is_synchronized


def test_replay_carries_the_original_mode_rather_than_laundering_it():
    degraded = _snapshot(mode=CaptureMode.MULTI_REQUEST)
    revived = EventSnapshot.from_dict(degraded.as_dict())
    assert revived.capture_mode is CaptureMode.MULTI_REQUEST
    assert not revived.is_synchronized


def test_round_trip_preserves_books_and_skew():
    snap = _snapshot()
    revived = EventSnapshot.from_dict(json.loads(json.dumps(snap.as_dict())))
    assert set(revived.books) == {"A", "B"}
    assert revived.books["A"].best_ask("yes") == Decimal("0.40")
    assert revived.skew_bound_ms == snap.skew_bound_ms
    assert revived.two_sided_legs == 2


def test_every_record_carries_the_strategy_version():
    assert _snapshot().as_dict()["strategy_version"] == STRATEGY_VERSION


# -------------------------------------------------------- the append-only log
def test_log_detects_a_deleted_line(tmp_path: Path):
    log = AppendOnlyLog(tmp_path / "l.jsonl")
    for i in range(4):
        log.append({"i": i})
    lines = (tmp_path / "l.jsonl").read_text().splitlines()
    # Remove the loser in the middle, which is the failure mode this guards.
    (tmp_path / "l.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    with pytest.raises(LogIntegrityError, match="chain broken"):
        list(read_log(tmp_path / "l.jsonl"))


def test_log_detects_an_edited_line(tmp_path: Path):
    log = AppendOnlyLog(tmp_path / "l.jsonl")
    log.append({"pnl": "-5"})
    raw = (tmp_path / "l.jsonl").read_text().replace('"-5"', '"+5"')
    (tmp_path / "l.jsonl").write_text(raw)
    with pytest.raises(LogIntegrityError, match="does not match its digest"):
        list(read_log(tmp_path / "l.jsonl"))


def test_log_resumes_the_chain_across_processes(tmp_path: Path):
    AppendOnlyLog(tmp_path / "l.jsonl").append({"i": 0})
    second = AppendOnlyLog(tmp_path / "l.jsonl")
    second.append({"i": 1})
    ok, message = verify_log(tmp_path / "l.jsonl")
    assert ok, message
    assert [e.body["i"] for e in read_log(tmp_path / "l.jsonl")] == [0, 1]


def test_log_survives_a_half_written_final_line(tmp_path: Path):
    log = AppendOnlyLog(tmp_path / "l.jsonl")
    log.append({"i": 0})
    with (tmp_path / "l.jsonl").open("a") as fh:
        fh.write('{"seq":1,"prev":"x","dig')
    resumed = AppendOnlyLog(tmp_path / "l.jsonl")
    assert resumed.count == 1  # chained from the last line that parsed


# --------------------------------------------------------------- the recorder
class _FakeProvider:
    """Stands in for Kalshi. Records what was asked for, never falls back."""

    def __init__(self, legs: dict[str, list[str]], *, fail: set[str] = frozenset()):
        self._legs = legs
        self._fail = set(fail)
        self.batch_calls: list[tuple[str, ...]] = []
        self.trade_calls: list[str] = []

    class _Client:
        def __init__(self, outer):
            self.outer = outer

        async def get_json(self, path, *, params=None, **kw):
            params = dict(params or {})
            if path == "/markets":
                event = params.get("event_ticker")
                return {"markets": [{"ticker": t} for t in self.outer._legs.get(event, [])]}
            if path == "/markets/trades":
                self.outer.trade_calls.append(params["ticker"])
                return {"trades": []}
            return None

    @property
    def _client(self):
        return self._Client(self)

    def _normalize_market(self, row):
        from pmvl_shared.enums import MarketStatus
        from pmvl_shared.schemas import NormalizedMarket

        return NormalizedMarket(
            platform=Platform.KALSHI,
            platform_market_id=row["ticker"],
            question=row["ticker"],
            status=MarketStatus.OPEN,
            close_time=datetime(2026, 12, 1, tzinfo=UTC),
            strike_type="greater",
            floor_strike=Decimal(str(abs(hash(row["ticker"])) % 100)),
        )

    async def get_orderbooks_synchronized(self, tickers, *, depth=25):
        from pmvl_markets.providers.kalshi import SynchronizedBooks
        from pmvl_shared.timeutil import utcnow

        requested = tuple(tickers)
        self.batch_calls.append(requested)
        sent = utcnow()
        if requested and requested[0].split("-")[0] in self._fail:
            return SynchronizedBooks(
                requested=requested, books={}, sent_at=sent, received_at=utcnow(),
                ok=False, detail="request failed: simulated 500",
            )
        return SynchronizedBooks(
            requested=requested,
            books={t: _book(t) for t in requested},
            sent_at=sent,
            received_at=utcnow(),
            ok=True,
            detail="",
        )


@pytest.mark.asyncio
async def test_an_event_wider_than_one_request_is_refused_not_truncated(tmp_path: Path):
    wide = [f"W-{i}" for i in range(MAX_BATCH_TICKERS + 1)]
    provider = _FakeProvider({"WIDE": wide})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("WIDE",), out_dir=tmp_path)
    )
    await rec.prepare()

    assert rec.capturable_events == []
    assert provider.batch_calls == []  # nothing was fetched at all
    bodies = [e.body for e in read_log(tmp_path / "snapshots.jsonl")]
    assert bodies[0]["kind"] == "rejection"
    assert bodies[0]["reason"] == RejectionReason.TOO_MANY_LEGS.value
    assert bodies[0]["requested_legs"] == MAX_BATCH_TICKERS + 1


@pytest.mark.asyncio
async def test_all_legs_of_an_event_go_out_in_one_request(tmp_path: Path):
    provider = _FakeProvider({"EV": ["EV-A", "EV-B", "EV-C"]})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("EV",), out_dir=tmp_path, record_trades=False)
    )
    await rec.capture_once()

    assert provider.batch_calls == [("EV-A", "EV-B", "EV-C")]
    snaps = [e.body for e in read_log(tmp_path / "snapshots.jsonl") if e.body["kind"] == "snapshot"]
    assert len(snaps) == 1
    assert set(snaps[0]["books"]) == {"EV-A", "EV-B", "EV-C"}
    assert snaps[0]["capture_mode"] == CaptureMode.SINGLE_REQUEST.value


@pytest.mark.asyncio
async def test_a_failed_capture_is_recorded_not_dropped(tmp_path: Path):
    provider = _FakeProvider({"BAD": ["BAD-A", "BAD-B"]}, fail={"BAD"})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("BAD",), out_dir=tmp_path, record_trades=False)
    )
    await rec.capture_once()

    bodies = [e.body for e in read_log(tmp_path / "snapshots.jsonl")]
    assert [b["kind"] for b in bodies] == ["rejection"]
    assert bodies[0]["reason"] == RejectionReason.REQUEST_FAILED.value
    assert rec.stats.snapshots == 0
    assert rec.stats.rejections == 1


@pytest.mark.asyncio
async def test_preparing_twice_does_not_double_count_refusals(tmp_path: Path):
    provider = _FakeProvider({"WIDE": [f"W-{i}" for i in range(MAX_BATCH_TICKERS + 1)]})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("WIDE",), out_dir=tmp_path)
    )
    await rec.prepare()
    await rec.prepare()
    assert rec.stats.rejections == 1


@pytest.mark.asyncio
async def test_stats_report_the_skew_bound_the_capture_actually_had(tmp_path: Path):
    provider = _FakeProvider({"EV": ["EV-A", "EV-B"]})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("EV",), out_dir=tmp_path, record_trades=False)
    )
    await rec.capture_once()
    stats = rec.stats.as_dict()
    assert stats["snapshots"] == 1
    assert stats["two_sided_legs"] == 2
    assert stats["skew_bound_ms_max"] is not None
    assert stats["skew_bound_ms_max"] >= 0


def test_an_empty_basket_of_events_produces_no_snapshot_and_says_so(tmp_path: Path):
    import asyncio

    provider = _FakeProvider({"GONE": []})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("GONE",), out_dir=tmp_path)
    )
    asyncio.run(rec.prepare())
    bodies = [e.body for e in read_log(tmp_path / "snapshots.jsonl")]
    assert bodies[0]["reason"] == RejectionReason.NO_MARKETS.value


@pytest.mark.asyncio
async def test_the_trade_watchlist_is_spread_across_events_not_alphabetical(tmp_path: Path):
    """One wide ladder must not consume the whole trade budget.

    Sorted-first-N gave all twenty slots to a single commodity event, leaving every
    other recorded event with quotes and no tape - a fill rate of zero produced by
    alphabetical order rather than by the market.
    """
    provider = _FakeProvider(
        {
            "AAA": [f"AAA-{i}" for i in range(40)],
            "ZZZ": [f"ZZZ-{i}" for i in range(4)],
        }
    )
    rec = SynchronizedRecorder(
        provider,
        RecorderConfig(
            event_tickers=("AAA", "ZZZ"), out_dir=tmp_path, max_trade_watch=8
        ),
    )
    await rec.capture_once()

    watched = provider.trade_calls
    assert len(watched) == 8
    assert sum(1 for t in watched if t.startswith("ZZZ")) == 4
    assert sum(1 for t in watched if t.startswith("AAA")) == 4


@pytest.mark.asyncio
async def test_the_rules_are_recorded_with_the_prices(tmp_path: Path):
    """A price without the rule that settles it cannot be replayed into a claim.

    Re-fetching the ladder at replay time would silently substitute whatever the
    venue lists *then* - relisted, corrected or expired rungs wearing the same
    tickers - for what was actually observed.
    """
    provider = _FakeProvider({"EV": ["EV-A", "EV-B"]})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("EV",), out_dir=tmp_path, record_trades=False)
    )
    await rec.prepare()

    bodies = [e.body for e in read_log(tmp_path / "markets.jsonl")]
    assert [b["market"]["platform_market_id"] for b in bodies] == ["EV-A", "EV-B"]
    assert all(b["market"]["strike_type"] == "greater" for b in bodies)


def test_a_snapshot_is_stamped_with_the_recorder_version_and_nothing_else():
    """A recording is evidence and does not change; the rules for reading it do.

    The two versions move independently - bumping one for both would mean a corrected
    analysis rule invalidates the recording it corrects, and re-reading old evidence
    under a fixed rule is exactly what recording it offline was for. What makes them
    independent is that a snapshot carries only the recorder's version and a report
    carries both; whether the two numbers happen to be equal on a given day says
    nothing, and asserting they differ would fail the moment they coincided.
    """
    from pmvl_markets.recording.schema import ANALYSIS_VERSION, STRATEGY_VERSION

    body = _snapshot().as_dict()
    assert body["strategy_version"] == STRATEGY_VERSION
    assert not any("analysis" in key for key in body)
    assert isinstance(ANALYSIS_VERSION, str) and ANALYSIS_VERSION


@pytest.mark.asyncio
async def test_the_tape_pull_cannot_starve_the_book_capture(tmp_path: Path):
    """The cadence is what the study measures; the tape is not allowed to eat it.

    Observed in a real 20-session study: sessions opened at 950 snapshots per half
    hour and ended at eight, while the trade count went *up*. A hundred trade
    requests on a busy evening take longer than the whole interval, and the book
    capture starves without anything reporting that it did.
    """
    provider = _SlowTradeProvider({"EV": [f"EV-{i}" for i in range(40)]})
    rec = SynchronizedRecorder(
        provider,
        RecorderConfig(
            event_tickers=("EV",), out_dir=tmp_path, interval_seconds=0.2,
            trade_budget_fraction=0.5, max_trade_watch=40,
        ),
    )
    await rec.capture_once()

    assert len(provider.trade_calls) < 40          # stopped early
    assert rec.stats.trade_budget_exhausted == 1   # and said so


@pytest.mark.asyncio
async def test_the_watchlist_rotates_so_every_leg_is_eventually_polled(tmp_path: Path):
    """Cutting the list off at the budget must not poll the first N tickers forever."""
    provider = _SlowTradeProvider({"EV": [f"EV-{i}" for i in range(40)]})
    rec = SynchronizedRecorder(
        provider,
        RecorderConfig(
            event_tickers=("EV",), out_dir=tmp_path, interval_seconds=0.2,
            trade_budget_fraction=0.5, max_trade_watch=40,
        ),
    )
    for _ in range(6):
        await rec.capture_once()

    first_cycle = set(provider.trade_calls[: rec.stats.trade_budget_exhausted])
    assert len(set(provider.trade_calls)) > len(first_cycle)


class _SlowTradeProvider(_FakeProvider):
    """Like the fake, but each trade fetch costs real time."""

    async def _sleep(self):
        import asyncio

        await asyncio.sleep(0.02)

    @property
    def _client(self):
        outer = self

        class _C(_FakeProvider._Client):
            async def get_json(self, path, *, params=None, **kw):
                if path == "/markets/trades":
                    await outer._sleep()
                return await super().get_json(path, params=params, **kw)

        return _C(outer)


@pytest.mark.asyncio
async def test_a_full_page_of_prints_is_flagged_as_lost_data(tmp_path: Path):
    """A poll that fills its page has dropped the older prints in the same window.

    The venue returns the most recent first and the cursor then advances past what
    was missed, so those prints are gone. Missing prints under-count fills, which
    pulls a maker result toward zero - a silent bias in the direction that makes a
    negative finding harder to see, which is the worst direction for it to be silent.

    Measured on a real study: the sessions whose cadence collapsed polled slowly
    enough that three of them hit the page limit exactly.
    """
    from pmvl_markets.recording.recorder import TRADE_PAGE_LIMIT

    provider = _FullPageProvider({"EV": ["EV-A"]})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("EV",), out_dir=tmp_path)
    )
    await rec.capture_once()   # backfill: no floor yet, nothing to have lost
    await rec.capture_once()   # incremental: a full page here is dropped prints

    assert rec.stats.trade_polls_at_limit == 1
    assert rec.stats.as_dict()["trade_polls_at_row_limit"] == 1
    assert rec.stats.trades == TRADE_PAGE_LIMIT


class _FullPageProvider(_FakeProvider):
    """Answers every trade poll with a completely full page."""

    @property
    def _client(self):
        outer = self

        class _C(_FakeProvider._Client):
            async def get_json(self, path, *, params=None, **kw):
                if path == "/markets/trades":
                    outer.trade_calls.append(dict(params or {})["ticker"])
                    return {
                        "trades": [
                            {
                                "trade_id": f"t{i}",
                                "created_time": "2026-09-20T12:00:00Z",
                                "yes_price_dollars": "0.40",
                                "count_fp": "1",
                                "taker_side": "no",
                            }
                            for i in range(200)
                        ]
                    }
                return await super().get_json(path, params=params, **kw)

        return _C(outer)


@pytest.mark.asyncio
async def test_the_first_poll_filling_its_page_is_backfill_not_loss(tmp_path: Path):
    """The opening poll has no floor and reaches back as far as the page allows.

    A full page there is the backfill working. Counting it would fire once per
    ticker per run and bury the real signal - which is exactly what it did on the
    first cycle of a live study before this guard existed.
    """
    provider = _FullPageProvider({"EV": ["EV-A"]})
    rec = SynchronizedRecorder(
        provider, RecorderConfig(event_tickers=("EV",), out_dir=tmp_path)
    )
    await rec.capture_once()
    assert rec.stats.trade_polls_at_limit == 0   # first poll: no floor, no claim

    await rec.capture_once()
    assert rec.stats.trade_polls_at_limit == 1   # second: a floor exists, so it counts
