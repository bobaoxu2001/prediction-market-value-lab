"""The LIP recorder: public books, prints and status for admitted incentive markets.

Read-only. It sends GET requests to public endpoints and writes files; it has no
credentials, no order methods and no way to acquire either.

It runs in the evening UTC window (pre-registration 8), after the maker study's
recorder has finished, and refuses outright inside the maker study's window - the two
would otherwise share one read budget and each would record a market the other was
also polling. Four kinds of lane run concurrently, each on its own HTTP client:

- **book lanes** read up to 100 admitted markets per batched request on a 1-second
  grid with seeded jitter, skipping a tick rather than queueing when the last request
  is still in flight (a queued read would be stamped as a later second's book);
- the **tape lane** polls the global trade feed from a watermark and logs every poll,
  so the reader can tell a quiet interval from one the recorder did not see;
- the **status lane** reads market and exchange status every 30 s;
- the **control lane** reads the program lists, admits pairs, fetches metadata and
  fee terms, audits the tape against per-market reads, and samples the clock.

Everything it writes is stamped with ``LIP_RECORDER_VERSION``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ..providers.kalshi import KalshiProvider
from ..recording.writer import AppendOnlyLog
from .http import LaneClient, lane_provider
from .params import RECORDING, RecordingParams
from .programs import ProgramRegistry
from .store import BookEncoder, SegmentWriter, write_checkpoint
from .universe import AdmissionState, AdmittedPair, program_eligible
from .version import LIP_RECORDER_VERSION, LIP_SIM_VERSION, SAMPLE_SALT

__all__ = [
    "Clock",
    "DayWindow",
    "LipRecorder",
    "LockHeld",
    "RealClock",
    "WindowRefused",
    "day_window",
    "maker_busy",
    "refused",
]


class WindowRefused(RuntimeError):
    """Asked to record inside the maker study's window."""


class LockHeld(RuntimeError):
    """Another LIP recorder holds the lock."""


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


def refused(t: time, params: RecordingParams = RECORDING) -> bool:
    return params.refuse_from <= t < params.refuse_until


@dataclass(frozen=True)
class DayWindow:
    """One study day: admission and quoting 12:25/12:30 on D to 00:45 on D+1, drain at 01:50."""

    day: date
    admission_from: datetime
    window_start: datetime
    window_end: datetime
    drain_at: datetime
    hard_stop: datetime


def day_window(now: datetime, params: RecordingParams = RECORDING) -> DayWindow:
    now = now.astimezone(timezone.utc)
    d = now.date() if now.time() >= params.refuse_from else now.date() - timedelta(days=1)

    def at(day: date, t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=timezone.utc)

    nxt = d + timedelta(days=1)
    return DayWindow(d, at(d, params.admission_from), at(d, params.window_start),
                     at(nxt, params.window_end), at(nxt, params.drain_at), at(nxt, params.hard_stop))


def maker_busy(params: RecordingParams = RECORDING) -> bool:
    if Path(params.maker_lock).exists():
        return True
    try:
        return subprocess.run(["pgrep", "-f", params.maker_process], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell - assume busy rather than risk sharing the budget


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


@dataclass
class LaneStats:
    requests: int = 0
    ok: int = 0
    failed: int = 0
    skipped_in_flight: int = 0
    #: grid ticks that passed while the lane was not running (the machine slept)
    late_ticks: int = 0
    #: ticks in the quoting window on which this lane had markets to read, and on which
    #: it read them - the counted-day test is on these, at the lane's own cadence
    due_ticks_in_window: int = 0
    ok_ticks_in_window: int = 0
    rate_limited: int = 0
    #: liveness, for the heartbeat and the day manifest
    consecutive_failures: int = 0
    last_ok_at: float | None = None
    last_failure_at: float | None = None
    last_error: str = ""


@dataclass
class LipRecorder:
    out_dir: Path
    state_path: Path
    settlements_path: Path
    known_series: set[str] = field(default_factory=set)
    params: RecordingParams = RECORDING
    shakedown: bool = False
    #: stop early (shakedown); None means the pre-registered drain/hard-stop times
    stop_at: datetime | None = None
    prereg_commit: str = ""
    #: the commit the recorder ran from, so a day can be tied to exact code
    code_commit: str = ""
    cadence_sha256: str = ""
    #: sha256 of artifacts/forward/lip-freeze.json: the exact code and config this day ran
    freeze_sha256: str = ""
    clock: Clock = field(default_factory=RealClock)
    make_provider: Callable[..., KalshiProvider] = lane_provider
    is_maker_busy: Callable[[], bool] = maker_busy
    run_sntp: Callable[[], float | None] | None = None
    lock_path: Path | None = None
    #: set by the cron wrapper so its run records and this day's files share one id
    run_id: str = ""

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.registry = ProgramRegistry()
        self.admission = (AdmissionState.from_json(json.loads(Path(self.state_path).read_text()))
                          if Path(self.state_path).exists() else AdmissionState())
        self.admission.known_series.update(self.known_series)
        self.admission.params = self.params
        #: lane -> [minute, count, first, last]: every request, for the isolation check
        self._req_minute: dict[str, list[float]] = {}
        #: lane -> recent (t, was_429) for the trailing ten-minute 429 share
        self._recent: dict[str, list[tuple[float, bool]]] = {}
        self._tape_marks: dict[str, float] = {}
        #: lane -> grid tick indices (at the recorded cadence) with a successful read
        self._ok_ticks: dict[str, set[int]] = {}
        self._tape_last_poll: dict[str, float] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.fees: dict[str, dict[str, Any]] = {}
        self.encoder = BookEncoder(self.params.keyframe_every_s, self.params.truncate_multiple)
        self.lanes: dict[str, LaneStats] = {}
        self.seen_trades: set[str] = set()
        self.recorded_ever: set[str] = set()
        self.watermark: float | None = None
        self._stop = asyncio.Event()
        self._providers: dict[str, KalshiProvider] = {}
        self._pause_books_until: float = 0.0
        self._tape_stats: dict[str, Any] = {"polls": 0, "complete": 0, "incomplete": 0, "pages": 0,
                                            "page_cap_hits": 0, "consecutive_incomplete": 0,
                                            "last_complete_at": None}
        self._wall_jumps: list[dict[str, float]] = []
        self._state = "starting"
        self._started_mono: float | None = None
        self._last_checkpoint: float | None = None
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> dict[str, Any]:
        now = self.clock.now()
        if refused(now.time(), self.params):
            raise WindowRefused(f"{now.time()} UTC is inside the maker study's window "
                                f"{self.params.refuse_from}-{self.params.refuse_until}")
        self.window = day_window(now, self.params)
        lock = Path(self.lock_path or self.params.own_lock)
        try:
            lock.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise LockHeld(f"{lock} exists: another LIP recorder is running (or crashed)") from exc
        try:
            if not await self._wait_for_maker():
                return self._write_skipped("maker study still running at give-up time")
            if refused(self.clock.now().time(), self.params):
                return self._write_skipped("the wait ran into the maker study's window")
            self.writer = SegmentWriter(self.out_dir)
            self._write_study_json()
            self._checkpoint()
            self._providers = {
                "book-A": self.make_provider("book-A", self.params.book_lane_rate, attempts=1),
                "tape": self.make_provider("tape", self.params.tape_rate, attempts=2),
                "status": self.make_provider("status", self.params.status_rate, attempts=2),
                "control": self.make_provider("control", self.params.control_rate, attempts=2),
            }
            if self.params.book_lanes > 1:
                self._providers["book-B"] = self.make_provider("book-B", self.params.book_lane_rate, attempts=1)
            for name in self._providers:
                self.lanes[name] = LaneStats()
            self._heartbeat("recording")
            await self._control_tick(first=True)
            tasks = [asyncio.create_task(self._book_lane(name, i))
                     for i, name in enumerate(n for n in ("book-A", "book-B") if n in self._providers)]
            tasks += [
                asyncio.create_task(self._tape_lane()),
                asyncio.create_task(self._status_lane()),
                asyncio.create_task(self._control_lane()),
                asyncio.create_task(self._stopper()),
            ]
            await asyncio.gather(*tasks)
            self._heartbeat("final_phase")
            remaining = (self.window.hard_stop - self.clock.now()).total_seconds() * self._time_scale()
            try:
                await asyncio.wait_for(self._final_phase(), timeout=max(1.0, remaining))
            except asyncio.TimeoutError:
                self._write("lanes", {"kind": "final_phase_cut", "at": self.clock.now().timestamp()})
            self._flush_requests()
            health = self._write_health()
            self._closed = True
            return health
        finally:
            # synchronous cleanup first, so nothing that suspends can be cancelled before
            # the ends are checkpointed, the segment closed and the lock released
            if hasattr(self, "writer"):
                self._checkpoint()  # an exception or a cancel still leaves exact ends
                self.writer.close()
                self._writer_closed = True
            if hasattr(self, "window") and self._state != "skipped":
                self._heartbeat("closed" if self._closed else "aborted")
            try:
                lock.rmdir()
            except OSError:
                pass
            for p in self._providers.values():
                try:
                    await p.aclose()
                except BaseException:  # noqa: BLE001 - a second cancel must not undo the above
                    pass

    async def _wait_for_maker(self) -> bool:
        while self.is_maker_busy():
            self._heartbeat("waiting_for_maker")
            t = self.clock.now()
            if t.time() >= self.params.give_up_waiting and not refused(t.time(), self.params):
                return False
            await self.clock.sleep(30)
        return True

    def _time_scale(self) -> float:
        """Real seconds per clock second (1 for the real clock; tests run a faster one)."""
        return float(getattr(self.clock, "scale", 1.0))

    async def _stopper(self) -> None:
        # never past the pre-registered drain time, whatever --until asked for
        drain = min(self.stop_at or self.window.drain_at, self.window.drain_at)
        last = beat = wall = self.clock.now().timestamp()
        mono = asyncio.get_running_loop().time()
        while self.clock.now() < drain:
            await self.clock.sleep(min(5.0, max(0.05, (drain - self.clock.now()).total_seconds())))
            now, now_mono = self.clock.now().timestamp(), asyncio.get_running_loop().time()
            self._note_wall_jump(wall, now, (now_mono - mono) / self._time_scale())
            wall, mono = now, now_mono
            if now - last >= self.params.checkpoint_every_s:
                self._checkpoint()
                last = now
            if now - beat >= self.params.heartbeat_every_s:
                self._heartbeat("recording")
                beat = now
        self._stop.set()

    def _note_wall_jump(self, wall_before: float, wall_after: float, elapsed: float) -> None:
        """The wall clock ran ahead of the event loop's monotonic clock: on macOS that is
        the machine asleep. Logged, never acted on - the book lane already treats the
        slept ticks as misses."""
        jump = (wall_after - wall_before) - elapsed
        if jump > self.params.wall_jump_s:
            event = {"from": wall_before, "to": wall_after, "jump_s": round(jump, 3)}
            self._wall_jumps.append(event)
            self._write("lanes", {"kind": "wall_jump", **event})

    def _checkpoint(self) -> None:
        """Every stream's end so far, so a day whose process dies is read up to here
        (Amendment 3) instead of being unreadable for want of health.json."""
        at = self.clock.now().timestamp()
        try:
            write_checkpoint(self.out_dir, dict(self.writer._tails), at)
        except OSError:
            return  # best effort: the next minute tries again, and a failure here must never
            # keep a segment open, a lock held, or the recording from going on
        self._last_checkpoint = at

    def _heartbeat(self, state: str) -> None:
        """heartbeat.json, rewritten atomically: liveness and the state of every lane, from
        counters the recorder keeps anyway. It makes no request and changes nothing."""
        self._state = state
        try:
            mono = asyncio.get_running_loop().time()
        except RuntimeError:
            mono = None
        if self._started_mono is None and mono is not None:
            self._started_mono = mono
        now = self.clock.now()
        cadence = self.params.book_cadence_s
        body = {
            "v": LIP_RECORDER_VERSION, "run_id": self.run_id, "day": self.window.day.isoformat(),
            "pid": os.getpid(), "state": state, "at": _iso(now), "at_ts": now.timestamp(),
            "monotonic_elapsed_s": None if mono is None or self._started_mono is None
            else round((mono - self._started_mono) / self._time_scale(), 3),
            "lanes": {k: dict(v.__dict__) for k, v in self.lanes.items()},
            "books": {k: {"due_ticks": v.due_ticks_in_window, "ok_ticks": v.ok_ticks_in_window,
                          "gap_seconds": max(0, v.due_ticks_in_window - v.ok_ticks_in_window) * cadence,
                          "slept_ticks": v.late_ticks}
                      for k, v in self.lanes.items() if k.startswith("book")},
            "tape": {**self._tape_stats, "watermark": self.watermark},
            "wall_jumps": len(self._wall_jumps),
            "slept_seconds": round(sum(j["jump_s"] for j in self._wall_jumps), 3),
            "checkpoint_at": self._last_checkpoint,
        }
        target = self.out_dir / "heartbeat.json"
        try:  # best effort: a heartbeat that cannot be written must never stop a recording
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            with tmp.open("w") as fh:
                fh.write(json.dumps(body) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError:
            pass

    def _write_skipped(self, reason: str) -> dict[str, Any]:
        """A day that did not record still exists: its admitted hours enter as zeros."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        body = {"tag": "shakedown" if self.shakedown else "study", "skipped": reason,
                "recorder_version": LIP_RECORDER_VERSION, "sim_version": LIP_SIM_VERSION,
                "day": self.window.day.isoformat(), "book_cadence_s": self.params.book_cadence_s,
                "window": {k: _iso(getattr(self.window, k)) for k in
                           ("admission_from", "window_start", "window_end", "drain_at", "hard_stop")}}
        (self.out_dir / "study.json").write_text(json.dumps(body, indent=2) + "\n")
        health = {"v": LIP_RECORDER_VERSION, "skipped": reason, "counted_day": False,
                  "day": self.window.day.isoformat()}
        (self.out_dir / "health.json").write_text(json.dumps(health, indent=2) + "\n")
        self._heartbeat("skipped")
        return {"skipped": True, "reason": reason}

    def _write_study_json(self) -> None:
        body = {
            "tag": "shakedown" if self.shakedown else "study",
            "recorder_version": LIP_RECORDER_VERSION, "sim_version": LIP_SIM_VERSION,
            "prereg_commit": self.prereg_commit, "code_commit": self.code_commit,
            "cadence_sha256": self.cadence_sha256, "freeze_sha256": self.freeze_sha256,
            "run_id": self.run_id, "pid": os.getpid(),
            "day": self.window.day.isoformat(), "started_at": _iso(self.clock.now()),
            "book_cadence_s": self.params.book_cadence_s, "tape_mode": self.params.tape_mode,
            "min_ts_honoured": self.params.min_ts_honoured, "total_cap": self.params.total_cap,
            "window": {k: _iso(getattr(self.window, k)) for k in
                       ("admission_from", "window_start", "window_end", "drain_at", "hard_stop")},
        }
        (self.out_dir / "study.json").write_text(json.dumps(body, indent=2) + "\n")

    def _write(self, stream: str, body: dict[str, Any]) -> None:
        if getattr(self, "_writer_closed", False):
            return  # a read still in flight at shutdown lands after the closing checkpoint:
            # it is not part of the day, and must not reopen a closed segment
        body = {"v": LIP_RECORDER_VERSION, **body}
        self.writer.append(stream, body, at=self.clock.now())

    def _client(self, lane: str) -> LaneClient | Any:
        return self._providers[lane]._client

    def _guard(self) -> None:
        """No request inside the maker study's window or past the hard stop - ever."""
        now = self.clock.now()
        if refused(now.time(), self.params) or now >= self.window.hard_stop:
            self._stop.set()
            raise WindowRefused(f"{now.time()} UTC: outside the LIP recording window")

    def _log_request(self, lane: str, t: float, was_429: bool = False) -> None:
        minute = int(t // 60)
        cur = self._req_minute.get(lane)
        if cur is not None and cur[0] != minute:
            self._write("lanes", {"kind": "requests", "lane": lane, "minute": int(cur[0]), "n": int(cur[1]),
                                  "first": cur[2], "last": cur[3]})
            cur = None
        if cur is None:
            self._req_minute[lane] = [minute, 1, t, t]
        else:
            cur[1] += 1
            cur[3] = t
        recent = self._recent.setdefault(lane, [])
        recent.append((t, was_429))
        while recent and recent[0][0] < t - self.params.backoff_s:
            recent.pop(0)

    def _flush_requests(self) -> None:
        for lane, cur in self._req_minute.items():
            self._write("lanes", {"kind": "requests", "lane": lane, "minute": int(cur[0]), "n": int(cur[1]),
                                  "first": cur[2], "last": cur[3]})
        self._req_minute = {}

    async def _get(self, lane: str, path: str, params: Any = None) -> Any:
        self._guard()
        stats = self.lanes[lane]
        stats.requests += 1
        client = self._client(lane)
        before = len(getattr(client, "rate_limited", []))
        sent = self.clock.now().timestamp()
        try:
            data = await client.get_json(path, params=params, use_cache=False, allow_404=True)
            stats.ok += 1
            self._note(stats, True, sent)
            return data
        except Exception as exc:
            stats.failed += 1
            self._note(stats, False, sent, str(exc))
            raise
        finally:
            hits = len(getattr(client, "rate_limited", [])) - before
            self._log_request(lane, sent, was_429=hits > 0)
            if hits > 0:
                stats.rate_limited += hits
                self._on_429(lane)

    @staticmethod
    def _note(stats: LaneStats, ok: bool, at: float, error: str = "") -> None:
        if ok:
            stats.consecutive_failures, stats.last_ok_at = 0, at
        else:
            stats.consecutive_failures += 1
            stats.last_failure_at, stats.last_error = at, error[:200]

    def _on_429(self, lane: str) -> None:
        """Section 8: halve the lane's rate for ten minutes; pause books if 429s pile up."""
        client = self._client(lane)
        if hasattr(client, "set_rate"):
            client.set_rate(max(0.1, client.rate / 2))
            asyncio.get_event_loop().call_later(
                self.params.backoff_s, lambda: client.set_rate(self._base_rate(lane)))
        recent = self._recent.get(lane, [])
        share = sum(1 for _, hit in recent if hit) / max(1, len(recent))
        if lane.startswith("book") and share > 0.01:
            now = self.clock.now()
            self._pause_books_until = (now.replace(minute=0, second=0, microsecond=0)
                                       + timedelta(hours=1)).timestamp()
        self._write("lanes", {"kind": "rate_limited", "lane": lane, "at": _iso(self.clock.now())})

    def _base_rate(self, lane: str) -> float:
        return {"tape": self.params.tape_rate, "status": self.params.status_rate,
                "control": self.params.control_rate}.get(lane, self.params.book_lane_rate)

    # ------------------------------------------------------------------ markets
    def _participation_end(self, pair: AdmittedPair) -> float:
        close = self.meta.get(pair.market, {}).get("close_time")
        close_ts = _ts(close).timestamp() - 1800 if close else float("inf")
        return min(pair.end, self.window.window_end.timestamp(), close_ts)

    def recorded_markets(self, t: float) -> list[str]:
        """Admitted markets still inside participation plus the one-hour mark tail."""
        out: list[str] = []
        for pair in sorted(self.admission.admitted.values(), key=lambda v: (v.admitted_at, v.program_id)):
            if pair.admitted_at <= t < self._participation_end(pair) + self.params.mark_tail_s:
                if pair.market not in out:
                    out.append(pair.market)
        return out[: self.params.book_lanes * self.params.book_lane_size]

    # ------------------------------------------------------------------ book lanes
    async def _book_lane(self, lane: str, index: int) -> None:
        """One read per tick of the window's own grid (window_start + i x cadence), each
        sent within the jitter of its tick. A read is credited to the tick it was
        scheduled for, so the counted-day test does not depend on the sub-second phase
        the process started at. A lane that wakes more than a tick late - the machine
        slept - rejoins the grid at the next tick; the ticks in between are misses."""
        rng = random.Random(f"{SAMPLE_SALT}|{self.window.day}|{lane}")
        in_flight: asyncio.Task | None = None
        lo, cadence = self.window.window_start.timestamp(), self.params.book_cadence_s
        i = math.ceil((self.clock.now().timestamp() - lo) / cadence)
        while not self._stop.is_set():
            tick = lo + i * cadence
            jitter = rng.uniform(-self.params.book_jitter_s, self.params.book_jitter_s)
            await self.clock.sleep(tick + jitter - self.clock.now().timestamp())
            if self._stop.is_set():
                break
            now = self.clock.now().timestamp()
            if now - tick > cadence:
                rejoin = math.ceil((now - lo) / cadence)
                self.lanes[lane].late_ticks += rejoin - i
                i = rejoin
                continue
            this, i = i, i + 1
            markets = self.recorded_markets(now)
            size = self.params.book_lane_size
            tickers = markets[index * size:(index + 1) * size]
            if tickers and 0 <= this < self._window_ticks():
                self.lanes[lane].due_ticks_in_window += 1
            if now < self._pause_books_until:
                continue
            if not tickers:
                continue
            if in_flight is not None and not in_flight.done():
                self.lanes[lane].skipped_in_flight += 1
                continue
            in_flight = asyncio.create_task(self._read_books(lane, tickers, this))
        if in_flight is not None:
            await asyncio.gather(in_flight, return_exceptions=True)

    async def _read_books(self, lane: str, tickers: list[str], tick: int | None = None) -> None:
        try:
            self._guard()
        except WindowRefused:
            return
        stats = self.lanes[lane]
        stats.requests += 1
        provider = self._providers[lane]
        client = provider._client
        before = len(getattr(client, "rate_limited", []))
        issued = self.clock.now().timestamp()
        result = await provider.get_orderbooks_synchronized(tickers, depth=self.params.book_depth)
        hits = len(getattr(client, "rate_limited", [])) - before
        self._log_request(lane, issued, was_429=hits > 0)
        if hits > 0:
            stats.rate_limited += hits
            self._on_429(lane)
        sent, received = result.sent_at.timestamp(), result.received_at.timestamp()
        if result.ok:
            stats.ok += 1
            self._note(stats, True, sent)
            if tick is not None and 0 <= tick < self._window_ticks():
                stats.ok_ticks_in_window += 1
                self._ok_ticks.setdefault(lane, set()).add(tick)
        else:
            stats.failed += 1
            self._note(stats, False, sent, str(result.detail))
        targets = {p.market: float(p.target) for p in self.admission.admitted.values()}
        self.recorded_ever.update(tickers)
        body = self.encoder.encode(lane=lane, sent_at=sent, received_at=received, ok=result.ok,
                                   detail=result.detail, requested=list(tickers),
                                   books=result.books, targets=targets)
        body["tick"] = tick  # the grid tick this read was scheduled for (Amendment 3.1)
        self._write("books", body)

    # ------------------------------------------------------------------ tape lane
    async def _tape_lane(self) -> None:
        while not self._stop.is_set():
            if self.params.tape_mode == "per_ticker":
                await self._tape_per_ticker_round()
                await self.clock.sleep(1.0)
            else:
                await self._tape_poll()
                await self.clock.sleep(self.params.tape_every_s)

    async def _tape_per_ticker_round(self) -> None:
        """TAPE_PT: each recorded market's own tape, each every tape_per_ticker_every_s."""
        now = self.clock.now().timestamp()
        for ticker in self.recorded_markets(now):
            if self._stop.is_set():
                return
            if now - self._tape_last_poll.get(ticker, -1e18) >= self.params.tape_per_ticker_every_s:
                self._tape_last_poll[ticker] = now
                await self._tape_poll(ticker=ticker)

    async def _tape_poll(self, ticker: str | None = None) -> dict[str, Any]:
        """One poll from the watermark - of the global feed, or of one market's - logged whole."""
        sent = self.clock.now().timestamp()
        key = ticker or "*"
        mark = self._tape_marks.get(key) if ticker else self.watermark
        if mark is None:
            mark = sent - 60
        min_ts = int(mark) - int(self.params.tape_overlap_s)
        cursor, pages, rows, error, cap_hit, reached = None, 0, [], "", False, False
        while True:
            params: dict[str, Any] = {"min_ts": min_ts, "limit": self.params.tape_limit}
            if ticker:
                params["ticker"] = ticker
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self._get("tape", "/markets/trades", params)
            except Exception as exc:  # the poll is logged incomplete, never dropped
                error = str(exc)[:300]
                break
            pages += 1
            batch = (data or {}).get("trades") or []
            rows.extend(batch)
            cursor = (data or {}).get("cursor")
            if not self.params.min_ts_honoured and any(
                    r.get("created_time") and _ts(r["created_time"]).timestamp() < min_ts for r in batch):
                reached = True  # min_ts ignored: this page reaches back past the watermark
                break
            if not cursor or not batch:
                break
            if pages >= self.params.tape_page_cap:
                cap_hit = True
                break
        received = self.clock.now().timestamp()
        kept, newest = [], None
        for r in rows:
            created = r.get("created_time")
            if created:
                ts = _ts(created).timestamp()
                newest = ts if newest is None or ts > newest else newest
            tid = str(r.get("trade_id") or "")
            if not tid or tid in self.seen_trades:
                continue
            if r.get("ticker") in self.recorded_ever or r.get("ticker") in self._admitted_tickers():
                self.seen_trades.add(tid)
                kept.append({k: r.get(k) for k in ("ticker", "trade_id", "created_time", "yes_price_dollars",
                                                   "count_fp", "taker_side", "is_block_trade")})
        complete = not error and not cap_hit and (self.params.min_ts_honoured or reached or not cursor)
        poll = {"kind": "tape_poll", "sent_at": sent, "received_at": received, "min_ts": min_ts,
                "pages": pages, "rows": len(rows), "kept": len(kept), "newest": newest,
                "complete": complete, "page_cap_hit": cap_hit, "error": error}
        if ticker:
            poll["ticker"] = ticker
        if kept:
            self._write("trades", {"kind": "prints", "poll_sent_at": sent, "prints": kept})
        self._write("tape_polls", poll)
        ts = self._tape_stats
        ts["polls"] += 1
        ts["pages"] += pages
        ts["page_cap_hits"] += int(cap_hit)
        if complete:
            ts["complete"] += 1
            ts["consecutive_incomplete"], ts["last_complete_at"] = 0, sent
        else:
            ts["incomplete"] += 1
            ts["consecutive_incomplete"] += 1
        if complete:
            # never let the next poll start after this one's start: continuity is what
            # makes consecutive complete polls one covered interval
            new_mark = max(mark, newest or mark, sent - 30)
            if ticker:
                self._tape_marks[key] = new_mark
            else:
                self.watermark = new_mark
        return poll

    def _admitted_tickers(self) -> set[str]:
        return {p.market for p in self.admission.admitted.values()}

    # ------------------------------------------------------------------ status lane
    async def _status_lane(self) -> None:
        while not self._stop.is_set():
            await self._status_poll()
            await self.clock.sleep(self.params.status_every_s)

    async def _status_poll(self) -> None:
        at = self.clock.now()
        tickers = self.recorded_markets(at.timestamp())
        markets: dict[str, Any] = {}
        chunks: list[dict[str, Any]] = []
        error = ""
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            try:
                data = await self._get("status", "/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            except Exception as exc:
                error = str(exc)[:300]
                chunks.append({"requested": chunk, "received_at": self.clock.now().timestamp(), "error": error})
                continue
            chunks.append({"requested": chunk, "received_at": self.clock.now().timestamp(), "error": ""})
            for m in (data or {}).get("markets") or []:
                markets[m["ticker"]] = {"status": m.get("status"), "close_time": m.get("close_time")}
                if m["ticker"] in self.meta:
                    self.meta[m["ticker"]].update(status=m.get("status"), close_time=m.get("close_time"))
        exchange: dict[str, Any] = {}
        exchange_error = ""
        try:
            exchange = await self._get("status", "/exchange/status") or {}
        except Exception as exc:
            exchange_error = str(exc)[:300]
        # every requested market is named, with the time its answer (or failure) arrived, so a
        # market the poll did not answer reads as unknown and nothing is acted on before it existed
        self._write("status", {"kind": "status", "at": at.timestamp(), "requested": tickers, "markets": markets,
                               "chunks": chunks, "exchange_received_at": self.clock.now().timestamp(),
                               "exchange": {"trading_active": exchange.get("trading_active"),
                                            "exchange_active": exchange.get("exchange_active")},
                               "exchange_error": exchange_error, "error": error})

    # ------------------------------------------------------------------ control lane
    async def _control_lane(self) -> None:
        while not self._stop.is_set():
            await self.clock.sleep(5.0)
            if not self._stop.is_set():
                await self._control_tick()

    async def _list_programs(self, status: str) -> list[dict[str, Any]]:
        rows, cursor = [], None
        while True:
            params: dict[str, Any] = {"status": status, "type": "liquidity", "limit": 10000}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("control", "/incentive_programs", params)
            batch = (data or {}).get("incentive_programs") or []
            rows.extend(batch)
            cursor = (data or {}).get("next_cursor") or (data or {}).get("cursor")
            if not cursor or not batch:
                return rows

    async def _control_tick(self, first: bool = False) -> None:
        now = self.clock.now()
        t = now.timestamp()
        due = getattr(self, "_due", {"active": 0.0, "upcoming": 0.0, "audit": t + 60, "clock": 0.0, "hourly": 0.0})
        self._due = due
        try:
            for listing, every in (("active", self.params.programs_active_every_s),
                                   ("upcoming", self.params.programs_upcoming_every_s)):
                if t >= due[listing]:
                    rows = await self._list_programs(listing)
                    diff = self.registry.observe(rows, t, listing=listing)
                    full = t >= due["hourly"] or first
                    #: every row whose content matters to the payout check is written
                    #: when it changes, not only on the hourly full listing
                    changed = set(diff.added) | set(diff.terms_changed) | set(diff.newly_paid)
                    self._write("programs", {"kind": "listing", "listing": listing, "at": t, "full": full,
                                             "added": diff.added, "withdrawn": diff.withdrawn,
                                             "terms_changed": diff.terms_changed, "newly_paid": diff.newly_paid,
                                             "rows": rows if full else [r for r in rows if r.get("id") in changed]})
                    due[listing] = t + every
            if t >= due["hourly"]:
                due["hourly"] = t + 3600
            if now < self.window.window_end:
                await self._fetch_candidate_metadata(now)
                events = self.admission.refresh(self.registry.programs.values(), self.meta, now,
                                                 window_end=self.window.window_end)
                for e in events:
                    if e.decision != "deferred":
                        self._write("universe", {"kind": "admission", **e.__dict__})
                Path(self.state_path).write_text(json.dumps(self.admission.to_json(), indent=1))
            await self._fetch_fee_terms()
            if t >= due["audit"]:
                await self._audit(now)
                due["audit"] = t + self.params.audit_every_s
            if t >= due["clock"]:
                self._sample_clock(now)
                due["clock"] = t + self.params.clock_every_s
        except Exception as exc:  # a failed control step is logged and retried next tick
            self._write("lanes", {"kind": "control_error", "at": t, "error": str(exc)[:300]})

    async def _fetch_candidate_metadata(self, now: datetime) -> None:
        window_end = self.window.window_end
        wanted = {p.market_ticker for p in self.registry.programs.values()
                  if p.id not in self.admission.decided and p.end > now and p.start <= window_end
                  and program_eligible(p, self.params)[0] and p.market_ticker not in self.meta}
        # pairs admitted on earlier days need today's close time too
        wanted |= {v.market for v in self.admission.admitted.values()
                   if v.end > now.timestamp() and v.market not in self.meta}
        wanted = sorted(wanted)
        for i in range(0, len(wanted), 100):
            chunk = wanted[i:i + 100]
            data = await self._get("control", "/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            for m in (data or {}).get("markets") or []:
                keep = {k: m.get(k) for k in ("ticker", "event_ticker", "status", "close_time", "price_ranges",
                                              "price_level_structure", "open_time", "expected_expiration_time")}
                self.meta[m["ticker"]] = keep
                self._write("markets", {"kind": "market", "at": now.timestamp(), **keep})

    async def _fetch_fee_terms(self) -> None:
        for pair in list(self.admission.admitted.values()):
            series = pair.market.split("-")[0]
            if series in self.fees:
                continue
            data = await self._get("control", f"/series/{series}")
            s = (data or {}).get("series") or {}
            self.fees[series] = {"fee_type": s.get("fee_type"), "fee_multiplier": s.get("fee_multiplier")}
            self._write("series", {"kind": "series", "series": series, **self.fees[series]})

    async def _audit(self, now: datetime) -> None:
        """Per-market tape reads for two hash-chosen markets, to check the global feed."""
        live = self.recorded_markets(now.timestamp())
        if not live:
            return
        slot = int(now.timestamp() // self.params.audit_every_s)
        chosen = sorted(live, key=lambda m: hashlib.sha256(f"{SAMPLE_SALT}|{m}|{slot}".encode()).hexdigest())
        for ticker in chosen[: self.params.audit_tickers]:
            lo = int(now.timestamp()) - 600
            data = await self._get("control", "/markets/trades",
                                   {"ticker": ticker, "min_ts": lo, "limit": 1000})
            #: ids *with* their timestamps: a print newer than the global feed's last
            #: complete poll is not a gap, it is a print the feed has not reached yet
            prints = [[str(r.get("trade_id")), r.get("created_time")]
                      for r in (data or {}).get("trades") or []]
            self._write("audit", {"kind": "audit", "at": now.timestamp(), "ticker": ticker,
                                  "min_ts": lo, "prints": prints})

    def _sample_clock(self, now: datetime) -> None:
        offset = None
        if self.run_sntp is not None:
            offset = self.run_sntp()
        else:
            try:
                out = subprocess.run(["sntp", "-t", "5", "time.apple.com"], capture_output=True,
                                     text=True, timeout=12).stdout
                token = out.strip().split()[0] if out.strip() else ""
                offset = float(token) if token else None
            except (OSError, subprocess.SubprocessError, ValueError):
                offset = None
        self._write("clock", {"kind": "clock", "at": now.timestamp(), "offset_s": offset})

    # ------------------------------------------------------------------ final phase
    async def _final_phase(self) -> None:
        """Drain the tape, read program payouts and settlements, verify, close."""
        if self.params.tape_mode == "per_ticker":
            for ticker in sorted(self.recorded_ever):
                await self._tape_poll(ticker=ticker)
        else:
            for _ in range(3):
                poll = await self._tape_poll()
                if poll["complete"]:
                    break
        try:
            for listing in ("paid_out", "closed"):
                rows = await self._list_programs(listing)
                mine = {p for p in self.admission.admitted}
                picked = [r for r in rows if r.get("id") in mine]
                diff = self.registry.observe(picked, self.clock.now().timestamp(), listing=listing)
                self._write("programs", {"kind": "listing", "listing": listing, "at": self.clock.now().timestamp(),
                                         "newly_paid": diff.newly_paid, "rows": picked})
        except Exception as exc:
            self._write("lanes", {"kind": "final_error", "error": str(exc)[:300]})
        log = AppendOnlyLog(self.settlements_path)
        tickers = sorted(self.recorded_ever)
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            try:
                data = await self._get("control", "/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            except Exception:
                continue
            for m in (data or {}).get("markets") or []:
                log.append({"v": LIP_RECORDER_VERSION, "ticker": m["ticker"], "status": m.get("status"),
                            "result": m.get("result"), "settlement_value_dollars": m.get("settlement_value_dollars"),
                            "fetched_at": _iso(self.clock.now())})
        (self.out_dir / "registry.json").write_text(json.dumps(self.registry.to_json(), indent=1))

    def _window_ticks(self) -> int:
        return max(1, int((self.window.window_end - self.window.window_start).total_seconds()
                          / self.params.book_cadence_s))

    def _write_health(self) -> dict[str, Any]:
        book_lanes = {k: v for k, v in self.lanes.items() if k.startswith("book")}
        # ticks in the window at the lane's own cadence; a tick with nothing due is not
        # a failure, a due tick without a successful read is
        lo = self.window.window_start.timestamp()
        ticks = self._window_ticks()
        # section 7 as written: a day counts if successful reads cover 80% of the window's
        # ticks. A tick is successful only if every lane with markets due at it read them -
        # a tick the recorder slept through, paused over, or had nothing to read is not
        size = self.params.book_lane_size
        ok_ticks = due_ticks = 0
        lane_names = [n for n in ("book-A", "book-B") if n in book_lanes]
        # how many markets were due at each tick, by one sweep over the pairs' intervals
        spans: dict[str, tuple[float, float]] = {}
        for pair in self.admission.admitted.values():
            a, b = pair.admitted_at, self._participation_end(pair) + self.params.mark_tail_s
            old_span = spans.get(pair.market)
            spans[pair.market] = (min(a, old_span[0]), max(b, old_span[1])) if old_span else (a, b)
        edges = sorted([(a, 1) for a, _ in spans.values()] + [(b, -1) for _, b in spans.values()])
        live, k = 0, 0
        cap = len(lane_names) * size
        #: runs of due ticks without a successful read, [first, last] - for diagnosis only;
        #: the counted-day test is the share below
        gaps: list[list[int]] = []
        for i in range(ticks):
            t = lo + i * self.params.book_cadence_s
            while k < len(edges) and edges[k][0] <= t:
                live += edges[k][1]
                k += 1
            n = min(live, cap)
            due = [name for j, name in enumerate(lane_names) if n > j * size]
            if not due:
                continue
            due_ticks += 1
            if all(i in self._ok_ticks.get(name, set()) for name in due):
                ok_ticks += 1
            elif gaps and gaps[-1][1] == i - 1:
                gaps[-1][1] = i
            else:
                gaps.append([i, i])
        share = ok_ticks / ticks
        body = {
            "v": LIP_RECORDER_VERSION, "tag": "shakedown" if self.shakedown else "study",
            "day": self.window.day.isoformat(),
            "lanes": {k: v.__dict__ for k, v in self.lanes.items()},
            "admitted_pairs": len(self.admission.admitted),
            "markets_recorded": len(self.recorded_ever),
            "trades_kept": len(self.seen_trades),
            "book_success_share_of_window": share,
            "window_ticks": ticks, "due_ticks": due_ticks, "ok_ticks": ok_ticks,
            "counted_day": (not self.shakedown) and share >= self.params.counted_day_share,
            "book_gaps": gaps, "run_id": self.run_id, "wall_jumps": self._wall_jumps,
            "tape": dict(self._tape_stats),
            "streams": {k: list(v) for k, v in self.writer._tails.items()},
            "finished_at": _iso(self.clock.now()),
        }
        (self.out_dir / "health.json").write_text(json.dumps(body, indent=2) + "\n")
        return body
