"""The LIP recorder: read-only, isolated from the maker study, and honest about gaps.

Every test here is a way the recording could mislead the analysis: a book stamped
with another lane's socket window, a queued read passed off as a fresh second, a tape
gap recorded as quiet, a removed hour that nobody notices, a recorder that runs over
the maker study, or a health report that leaks money into an interim.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_markets.lip.http import LaneClient, lane_provider
from pmvl_markets.lip.reader import load_day, tape_coverage
from pmvl_markets.lip.recorder import LipRecorder, LockHeld, WindowRefused, day_window, refused
from pmvl_markets.lip.store import BookEncoder, SegmentWriter, encode_side, read_stream
from pmvl_markets.providers.http import ProviderError
from pmvl_markets.providers.kalshi import SynchronizedBooks
from pmvl_markets.recording.writer import LogIntegrityError
from pmvl_shared.enums import Platform
from pmvl_shared.schemas import BookLevel, OrderBook

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 9, 25, 12, 25, tzinfo=UTC)


# ------------------------------------------------------------------ fakes
class VirtualClock:
    """Virtual time = start + real elapsed / scale, shared by every task."""

    def __init__(self, start: datetime, scale: float = 0.001) -> None:
        self.start, self.scale = start, scale
        self.t0 = None

    def _elapsed(self) -> float:
        loop = asyncio.get_event_loop()
        if self.t0 is None:
            self.t0 = loop.time()
        return (loop.time() - self.t0) / self.scale

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self._elapsed())

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) * self.scale)


def lv(p: str, q: str) -> BookLevel:
    return BookLevel(price=Decimal(p), size=Decimal(q))


class FakeClient:
    def __init__(self, venue: "FakeVenue", name: str) -> None:
        self.venue, self.name = venue, name
        self.rate_limited: list[float] = []
        self.rate = 1.0
        self.calls: list[tuple[str, dict]] = []

    def set_rate(self, rate: float) -> None:
        self.rate = rate

    async def get_json(self, path, params=None, use_cache=False, allow_404=False):
        params = dict(params or {})
        self.calls.append((path, params))
        return self.venue.answer(path, params)


class FakeProvider:
    def __init__(self, venue: "FakeVenue", name: str, clock: VirtualClock) -> None:
        self._client = FakeClient(venue, name)
        self.venue, self.clock, self.name = venue, clock, name

    async def get_orderbooks_synchronized(self, tickers, depth=None):
        sent = self.clock.now()
        if self.venue.down():  # the real provider turns a transport error into ok=False
            return SynchronizedBooks(requested=tuple(tickers), books={}, sent_at=sent, received_at=self.clock.now(),
                                     ok=False, detail=f"request failed: {self.venue.outage_error}")
        await self.clock.sleep(self.venue.book_delay_s)
        received = self.clock.now()
        self.venue.book_calls.append((self.name, list(tickers), depth))
        books = {t: OrderBook(platform=Platform.KALSHI, platform_market_id=t, observed_at=received,
                              yes_bids=[lv("0.40", "800"), lv("0.39", "900"), lv("0.20", "5000")],
                              no_bids=[lv("0.55", "1500")]) for t in tickers}
        return SynchronizedBooks(requested=tuple(tickers), books=books, sent_at=sent, received_at=received,
                                 ok=True, detail="")

    async def aclose(self):
        return None


def prow(pid, market, hours=24, usd=100):
    s = START
    return {"id": pid, "market_ticker": market, "incentive_type": "liquidity",
            "start_date": s.isoformat().replace("+00:00", "Z"),
            "end_date": (s + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "period_reward": usd * 10_000, "target_size_fp": "1000.00", "discount_factor_bps": 5000,
            "paid_out": False}


class FakeVenue:
    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.book_delay_s = 0.2
        self.book_calls: list = []
        self.programs = [prow("p1", "KXFOO-A"), prow("p2", "KXBTCD-B"), prow("p3", "KXBAR-C", usd=10)]
        #: [from, to) in venue time during which every request fails as it did on the
        #: maker study's days after a wake from sleep: DNS not yet back
        self.outage: tuple[datetime, datetime] | None = None
        self.outage_error = ("kalshi exhausted retries: kalshi transport: "
                             "[Errno 8] nodename nor servname provided, or not known")

    def down(self) -> bool:
        return self.outage is not None and self.outage[0] <= self.clock.now() < self.outage[1]

    def answer(self, path: str, params: dict):
        now = self.clock.now()
        if self.down():
            raise ProviderError(self.outage_error)
        if path == "/incentive_programs":
            return {"incentive_programs": self.programs if params.get("status") == "active" else [], "cursor": ""}
        if path == "/markets":
            tickers = str(params.get("tickers", "")).split(",")
            return {"markets": [{"ticker": t, "status": "active", "event_ticker": t.split("-")[0] + "-E",
                                 "close_time": (now + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
                                 "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}]}
                                for t in tickers if t]}
        if path.startswith("/series/"):
            return {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
        if path == "/exchange/status":
            return {"trading_active": True, "exchange_active": True}
        if path == "/markets/trades":
            t = (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            tid = f"t{int(now.timestamp())}"
            ticker = params.get("ticker", "KXFOO-A")
            return {"trades": [{"ticker": ticker, "trade_id": tid, "created_time": t, "yes_price_dollars": "0.40",
                                "count_fp": "10.00", "taker_side": "no", "is_block_trade": False},
                               {"ticker": "KXOTHER-Z", "trade_id": tid + "x", "created_time": t,
                                "yes_price_dollars": "0.50", "count_fp": "1.00", "taker_side": "yes"}],
                    "cursor": ""}
        raise AssertionError(f"unexpected path {path}")


def make_recorder(tmp_path: Path, clock: VirtualClock, venue: FakeVenue, *, busy=lambda: False,
                  stop_minutes: float = 3.0, lock: str = "lock") -> LipRecorder:
    providers = {}

    def make(name, rate, attempts=1):
        providers[name] = FakeProvider(venue, name, clock)
        return providers[name]

    rec = LipRecorder(out_dir=tmp_path / "day", state_path=tmp_path / "state.json",
                      settlements_path=tmp_path / "settlements.jsonl", known_series={"KXFOO"},
                      shakedown=True, stop_at=START + timedelta(minutes=stop_minutes), clock=clock,
                      make_provider=make, is_maker_busy=busy, run_sntp=lambda: 0.012,
                      lock_path=tmp_path / lock)
    rec.providers_made = providers
    return rec


# ------------------------------------------------------------------ window and locks
def test_the_recorder_refuses_the_maker_study_window():
    assert refused(datetime(2026, 9, 25, 2, 0).time()) and refused(datetime(2026, 9, 25, 12, 24).time())
    assert not refused(datetime(2026, 9, 25, 12, 25).time()) and not refused(datetime(2026, 9, 25, 1, 59).time())


def test_run_raises_inside_the_maker_window(tmp_path):
    clock = VirtualClock(datetime(2026, 9, 25, 5, 0, tzinfo=UTC))
    rec = make_recorder(tmp_path, clock, FakeVenue(clock))
    with pytest.raises(WindowRefused):
        asyncio.run(rec.run())


def test_the_day_spans_midnight_utc():
    w = day_window(datetime(2026, 9, 26, 1, 0, tzinfo=UTC))
    assert w.day.isoformat() == "2026-09-25"
    assert w.window_end == datetime(2026, 9, 26, 0, 45, tzinfo=UTC)


def test_it_waits_for_the_maker_study_and_gives_up_at_13_30(tmp_path):
    clock = VirtualClock(datetime(2026, 9, 25, 13, 20, tzinfo=UTC), scale=0.0001)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), busy=lambda: True)
    out = asyncio.run(rec.run())
    assert out["skipped"] is True
    assert not (tmp_path / "lock").exists(), "the lock is released on the way out"


def test_a_second_recorder_cannot_take_the_lock(tmp_path):
    (tmp_path / "lock").mkdir()
    clock = VirtualClock(START)
    with pytest.raises(LockHeld):
        asyncio.run(make_recorder(tmp_path, clock, FakeVenue(clock)).run())



def test_a_network_outage_is_recorded_as_a_gap_and_the_day_carries_on(tmp_path):
    """The maker study lost three days in a row to a DNS failure just after a wake from
    sleep, because one exhausted retry ended its run. A lost minute here must be a
    recorded gap in every lane - never the end of the day, never a quiet stretch."""
    clock = VirtualClock(START, scale=0.002)
    venue = FakeVenue(clock)
    down_from, down_to = START + timedelta(seconds=60), START + timedelta(seconds=120)
    venue.outage = (down_from, down_to)
    health = asyncio.run(make_recorder(tmp_path, clock, venue, stop_minutes=3.0).run())
    day = tmp_path / "day"
    assert (day / "health.json").exists() and health["streams"], "the day closed normally"

    lo, hi = down_from.timestamp(), down_to.timestamp()
    books = list(read_stream(day, "books"))
    during = [b for b in books if lo <= b["sent_at"] < hi]
    after = [b for b in books if b["sent_at"] >= hi + 5]
    assert during and not any(b["ok"] for b in during), "each failed read is a line, marked failed"
    assert after and all(b["ok"] for b in after), "the lane resumed on its own"

    polls = list(read_stream(day, "tape_polls"))
    failed = [p for p in polls if lo <= p["sent_at"] < hi]
    assert failed and not any(p["complete"] for p in failed)
    assert all("nodename" in p["error"] for p in failed)
    before = [p for p in polls if p["complete"] and p["sent_at"] < lo]
    resumed = next(p for p in polls if p["complete"] and p["sent_at"] >= hi)
    assert resumed["min_ts"] <= before[-1]["sent_at"], "the first poll back reaches over the gap"

    status = [b for b in read_stream(day, "status") if lo <= b["at"] < hi]
    assert status and all(b["error"] or b["exchange_error"] for b in status)
    assert load_day(day).pairs, "the day still loads"


class SleepingClock(VirtualClock):
    """The machine sleeps once: the wall clock jumps ahead, the event loop's monotonic
    clock (what asyncio sleeps on) does not - as on macOS after a lid close."""

    def __init__(self, start: datetime, scale: float, sleep_at_s: float, sleep_for_s: float) -> None:
        super().__init__(start, scale)
        self.sleep_at_s, self.sleep_for_s = sleep_at_s, sleep_for_s

    def now(self) -> datetime:
        e = self.awake_s = self._elapsed()
        return self.start + timedelta(seconds=e + (self.sleep_for_s if e >= self.sleep_at_s else 0.0))


def test_a_wake_from_sleep_does_not_burst_the_missed_ticks(tmp_path):
    clock = SleepingClock(START, scale=0.002, sleep_at_s=60, sleep_for_s=600)
    venue = FakeVenue(clock)
    health = asyncio.run(make_recorder(tmp_path, clock, venue, stop_minutes=12.0).run())
    awake_s = clock.awake_s
    calls = [c for c in venue.book_calls if c[0] == "book-A"]
    assert len(calls) <= awake_s / 1.0 + 5, "one read per awake second at most, not one per missed tick"
    lo, hi = (START + timedelta(seconds=60)).timestamp(), (START + timedelta(seconds=660)).timestamp()
    assert not any(lo < b["sent_at"] < hi for b in read_stream(tmp_path / "day", "books")), \
        "nothing is stamped inside the time the machine slept"
    assert health["streams"]

# ------------------------------------------------------------------ an evening against a fake venue
@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("lipday")
    clock = VirtualClock(START, scale=0.002)
    venue = FakeVenue(clock)
    venue.book_delay_s = 1.3  # slower than the 1 s grid: ticks must be skipped, not queued
    rec = make_recorder(tmp, clock, venue, stop_minutes=4.0)
    health = asyncio.run(rec.run())
    return tmp, rec, venue, health


def test_only_eligible_programs_are_admitted_and_every_decision_is_logged(recorded):
    tmp, *_ = recorded
    decisions = {b["program_id"]: (b["decision"], b["reason"]) for b in read_stream(tmp / "day", "universe")}
    assert decisions == {"p1": ("admitted", ""), "p2": ("rejected", "X4"), "p3": ("rejected", "rate")}


def test_book_lanes_skip_ticks_while_in_flight_and_request_full_depth(recorded):
    _, rec, venue, health = recorded
    assert health["lanes"]["book-A"]["skipped_in_flight"] > 0
    assert venue.book_calls and all(depth == 100 for _, _, depth in venue.book_calls)
    assert all(name == "book-A" for name, _, _ in venue.book_calls), "one admitted market: lane B idle"


def test_every_lane_has_its_own_provider(recorded):
    _, rec, _, _ = recorded
    made = rec.providers_made
    assert set(made) == {"book-A", "book-B", "tape", "status", "control"}
    assert len({id(p._client) for p in made.values()}) == 5


def test_books_are_bids_only_truncated_at_twice_the_target_with_the_cut_recorded(recorded):
    tmp, *_ = recorded
    first = next(b for b in read_stream(tmp / "day", "books") if b["markets"])
    m = first["markets"]["KXFOO-A"]
    assert m["yes"] == [[40, 800.0], [39, 900.0], [20, 5000.0]], "the level crossing 2T is kept whole"
    assert m["no"] == [[55, 1500.0]] and m["key"] is True


def test_unchanged_books_are_recorded_as_observations(recorded):
    tmp, *_ = recorded
    lines = [b for b in read_stream(tmp / "day", "books")]
    assert any("KXFOO-A" in b["unchanged"] for b in lines)
    day = load_day(tmp / "day")
    series = day.book_series("KXFOO-A")
    assert len(series.snapshots) == len(lines), "one snapshot per request that covered the market"


def test_the_tape_keeps_admitted_markets_only_and_polls_are_continuous(recorded):
    tmp, *_ = recorded
    prints = [p for b in read_stream(tmp / "day", "trades") for p in b["prints"]]
    assert prints and {p["ticker"] for p in prints} == {"KXFOO-A"}
    polls = list(read_stream(tmp / "day", "tape_polls"))
    complete = [p for p in polls if p["complete"]]
    for a, b in zip(complete, complete[1:]):
        assert b["min_ts"] <= a["sent_at"], "each poll starts no later than the last one began"


def test_no_line_is_stamped_with_the_maker_study_version(recorded):
    tmp, *_ = recorded
    streams = {f.name.split(".")[0] for f in (tmp / "day").rglob("*.jsonl*")}
    assert {"books", "trades", "tape_polls", "status", "programs", "universe", "markets", "series"} <= streams
    for stream in streams:
        for body in read_stream(tmp / "day", stream):
            assert body["v"] == "lip-1.0.0", stream
    study = json.loads((tmp / "day" / "study.json").read_text())
    assert study["tag"] == "shakedown" and study["recorder_version"] == "lip-1.0.0"


def test_a_shakedown_is_never_a_counted_day(recorded):
    _, _, _, health = recorded
    assert health["counted_day"] is False


def test_the_recorded_day_loads_into_the_shared_types(recorded):
    tmp, *_ = recorded
    day = load_day(tmp / "day")
    assert [p.market for p in day.pairs] == ["KXFOO-A"]
    md = day.market_day(day.pairs[0])
    assert md.family == "KXFOO" and md.fee_type == "quadratic"
    assert md.trades.ts.size > 0 and bool(md.trades.taker_yes.any()) is False
    assert md.books.snapshots[0].yes.best == 40 and md.books.snapshots[0].no.best == 55
    assert md.status.points and md.status.points[0].exchange_active is True


# ------------------------------------------------------------------ store
def test_encode_side_cuts_after_the_level_that_crosses_twice_the_target():
    kept, dropped = encode_side([lv("0.5", "1500"), lv("0.49", "600"), lv("0.48", "700"), lv("0.47", "5")], 1000)
    assert kept == [[50, 1500.0], [49, 600.0]] and dropped == [2.0, 705.0]


def test_the_encoder_writes_changes_and_periodic_keyframes_only():
    enc = BookEncoder(keyframe_every_s=60)
    book = OrderBook(platform=Platform.KALSHI, platform_market_id="M", observed_at=START,
                     yes_bids=[lv("0.4", "10")], no_bids=[lv("0.5", "10")])
    a = enc.encode(lane="A", sent_at=0, received_at=1, ok=True, detail="", requested=["M"], books={"M": book}, targets={})
    b = enc.encode(lane="A", sent_at=1, received_at=2, ok=True, detail="", requested=["M"], books={"M": book}, targets={})
    c = enc.encode(lane="A", sent_at=61, received_at=62, ok=True, detail="", requested=["M"], books={"M": book}, targets={})
    assert "M" in a["markets"] and b["unchanged"] == ["M"] and c["markets"]["M"]["key"] is True


def test_the_hash_chain_runs_across_gzipped_segments_and_a_removed_hour_breaks_it(tmp_path):
    w = SegmentWriter(tmp_path)
    for h in range(3):
        for i in range(2):
            w.append("books", {"h": h, "i": i}, at=START + timedelta(hours=h, minutes=i))
    w.close()
    assert [b["h"] for b in read_stream(tmp_path, "books")] == [0, 0, 1, 1, 2, 2]
    assert all(f.suffix == ".gz" for f in tmp_path.rglob("books.*"))
    middle = sorted(tmp_path.glob("segment-*"))[1]
    for f in middle.iterdir():
        f.unlink()
    with pytest.raises(LogIntegrityError):
        list(read_stream(tmp_path, "books"))


def test_a_resumed_writer_continues_the_chain(tmp_path):
    w = SegmentWriter(tmp_path, gzip_closed=False)
    w.append("s", {"n": 1}, at=START)
    w2 = SegmentWriter(tmp_path, gzip_closed=False)
    w2.append("s", {"n": 2}, at=START + timedelta(minutes=1))
    assert [b["n"] for b in read_stream(tmp_path, "s")] == [1, 2]


# ------------------------------------------------------------------ tape coverage
def test_incomplete_polls_and_audit_mismatches_are_gaps():
    polls = [{"min_ts": 0, "sent_at": 10, "complete": True}, {"min_ts": 8, "sent_at": 20, "complete": True},
             {"min_ts": 18, "sent_at": 30, "complete": False}, {"min_ts": 28, "sent_at": 40, "complete": True}]
    cov = tape_coverage(polls, [], {})
    assert cov.covered(1, 19) and not cov.covered(21, 29)
    audit = [{"ticker": "M", "min_ts": 0, "at": 12, "prints": [["x", "1970-01-01T00:00:05Z"]]}]
    cov = tape_coverage(polls, audit, {"M": set()})
    assert not cov.covered(3, 4), "a print the global feed missed voids the interval"


# ------------------------------------------------------------------ lanes
def test_lane_clients_have_no_burst_no_cache_and_one_attempt():
    p = lane_provider("book-A", 1.25, attempts=1)
    c = p._client
    assert isinstance(c, LaneClient) and c._limiter.capacity == 1 and c._cache_ttl == 0 and c._max_retries == 1
    assert lane_provider("book-B", 1.25)._client is not c


def test_concurrent_lanes_keep_their_own_socket_windows():
    import httpx

    async def slow(request):
        await asyncio.sleep(0.05 if "slow" in str(request.url) else 0.0)
        return httpx.Response(200, json={"ok": True})

    async def main():
        a, b = LaneClient("https://x.test", name="a", rate=100), LaneClient("https://x.test", name="b", rate=100)
        for c in (a, b):
            c._client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(slow))
        await asyncio.gather(a.get_json("/slow", use_cache=False), b.get_json("/fast", use_cache=False))
        return a.last_request_window, b.last_request_window

    wa, wb = asyncio.run(main())
    assert wa[1] - wa[0] >= 0.04 and wb[1] - wb[0] < 0.04


# ------------------------------------------------------------------ isolation
PINNED = [
    "packages/market-normalization/src/pmvl_markets/recording/recorder.py",
    "packages/market-normalization/src/pmvl_markets/recording/schema.py",
    "packages/market-normalization/src/pmvl_markets/recording/writer.py",
    "packages/market-normalization/src/pmvl_markets/recording/reader.py",
    "packages/market-normalization/src/pmvl_markets/providers/http.py",
    "packages/market-normalization/src/pmvl_markets/providers/kalshi.py",
    "packages/market-normalization/src/pmvl_markets/arbitrage/maker_experiment.py",
    "packages/market-normalization/src/pmvl_markets/arbitrage/maker_study.py",
    "services/worker/src/pmvl_worker/cli.py",
    "services/worker/src/pmvl_worker/record_cli.py",
    "services/worker/src/pmvl_worker/replay_cli.py",
    "scripts/record_maker_day.sh",
]


def test_the_maker_study_files_are_byte_identical_to_the_preregistration_state():
    pinned = json.loads((FIXTURES / "lip_pinned_files.json").read_text())
    assert sorted(pinned) == sorted(PINNED)
    for path, digest in pinned.items():
        assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == digest, path


def test_the_maker_cli_does_not_import_the_lip_cli():
    code = "import sys, pmvl_worker.cli; print('pmvl_worker.lip_cli' in sys.modules, 'pmvl_markets.lip' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120).stdout.split()
    assert out == ["False", "False"]


def test_health_cannot_import_the_modules_that_compute_money():
    tree = ast.parse((REPO / "packages/market-normalization/src/pmvl_markets/lip/health.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[-1])
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name.split(".")[-1] for a in node.names)
    assert not names & {"scoring", "fills", "simulator", "valuation", "study"}


def test_the_lip_package_has_no_write_path():
    src = REPO / "packages/market-normalization/src/pmvl_markets/lip"
    for f in src.glob("*.py"):
        text = f.read_text()
        for banned in ("post_json", ".post(", ".put(", ".delete(", "create_order", "api_key", "private_key",
                       "Authorization", "KALSHI-ACCESS"):
            assert banned not in text, f"{f.name} contains {banned!r}"
