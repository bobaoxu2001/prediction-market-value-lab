"""Operations before the freeze: can every study day be reconstructed and diagnosed after
the fact? Heartbeat, missed-run sentinel, day manifest and qualification, the freeze
manifest, the failure modes that can corrupt a day, and the shakedown's labelling.

Nothing here tests economics; the modules under test are outcome-blind.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from pmvl_markets.lip.daymanifest import build_manifest, classify_error, qualify, record_final_statuses, run_status
from pmvl_markets.lip.http import LaneClient
from pmvl_markets.lip.reader import load_day
from pmvl_markets.lip.recorder import LockHeld
from pmvl_markets.lip.store import read_stream, stream_ends
from pmvl_markets.providers.http import ProviderError

from tests.test_lip_recorder import (START, FakeVenue, SleepingClock, VirtualClock, make_recorder, prow,
                                     run_virtual)

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]
D = date(2026, 10, 1)


def at(h: int, m: int, s: int = 0, day: date = D) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=UTC)


# ================================================================== C. heartbeat
IN_WINDOW = START + timedelta(minutes=4, seconds=50)   # 12:29:50, so the run spans 12:30


def _record_with_beats(tmp_path, venue_factory=FakeVenue, clock=None, minutes=6.0, start=IN_WINDOW):
    """Run a recorder in virtual time, reading heartbeat.json every 20 s as it goes."""
    beats: list[dict] = []

    async def main():
        c = clock or VirtualClock(start, scale=1.0)
        venue = venue_factory(c)
        rec = make_recorder(tmp_path, c, venue, stop_minutes=0)
        rec.stop_at, rec.run_id = c.start + timedelta(minutes=minutes), "test-run"

        async def watch():
            while True:
                await asyncio.sleep(20)
                f = tmp_path / "day" / "heartbeat.json"
                if f.exists():
                    beat = json.loads(f.read_text())
                    if not beats or beat["at_ts"] != beats[-1]["at_ts"]:
                        beats.append(beat)

        w = asyncio.create_task(watch())
        health = await rec.run()
        w.cancel()
        return rec, venue, health

    rec, venue, health = run_virtual(main())
    final = json.loads((tmp_path / "day" / "heartbeat.json").read_text())
    return rec, venue, health, beats, final


def test_the_heartbeat_progresses_while_recording_and_closes(tmp_path):
    _, _, _, beats, final = _record_with_beats(tmp_path)
    recording = [b for b in beats if b["state"] == "recording"]
    assert len(recording) >= 8, "a beat every 30 s of a 6-minute run"
    ts = [b["at_ts"] for b in recording]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    ok = [b["books"]["book-A"]["ok_ticks"] for b in recording]
    assert ok == sorted(ok) and ok[-1] > ok[0], "successful ticks accumulate"
    assert recording[-1]["monotonic_elapsed_s"] > recording[0]["monotonic_elapsed_s"]
    assert recording[-1]["run_id"] == "test-run" and recording[-1]["pid"] == os.getpid()
    assert recording[-1]["tape"]["complete"] > 0 and recording[-1]["checkpoint_at"] is not None
    assert final["state"] == "closed"


def test_the_heartbeat_shows_a_failure_and_its_recovery(tmp_path):
    down = (IN_WINDOW + timedelta(seconds=90), IN_WINDOW + timedelta(seconds=180))

    def venue(clock):
        v = FakeVenue(clock)
        v.outage = down
        return v

    _, _, _, beats, _ = _record_with_beats(tmp_path, venue)
    lo, hi = down[0].timestamp(), down[1].timestamp()
    during = [b for b in beats if lo + 30 < b["at_ts"] < hi]
    after = [b for b in beats if b["at_ts"] > hi + 40]
    assert during and all(b["lanes"]["book-A"]["consecutive_failures"] > 0 for b in during)
    assert all("nodename" in b["lanes"]["book-A"]["last_error"] for b in during)
    assert any(b["tape"]["consecutive_incomplete"] > 0 for b in during)
    assert during[-1]["books"]["book-A"]["gap_seconds"] > 0
    assert after and after[-1]["lanes"]["book-A"]["consecutive_failures"] == 0
    assert after[-1]["lanes"]["book-A"]["last_ok_at"] > hi
    assert after[-1]["tape"]["consecutive_incomplete"] == 0
    assert 85 <= after[-1]["books"]["book-A"]["gap_seconds"] <= 95, "the 90-second outage, and no more"


def test_a_sleep_is_logged_as_a_wall_clock_jump(tmp_path):
    clock = SleepingClock(IN_WINDOW, scale=1.0, sleep_at_s=120, sleep_for_s=900)
    _, _, health, _, final = _record_with_beats(tmp_path, clock=clock, minutes=20.0)
    assert final["wall_jumps"] == 1 and 890 <= final["slept_seconds"] <= 910
    jumps = [b for b in read_stream(tmp_path / "day", "lanes") if b.get("kind") == "wall_jump"]
    assert len(jumps) == 1 and 890 <= jumps[0]["jump_s"] <= 910
    assert health["wall_jumps"] and health["lanes"]["book-A"]["late_ticks"] >= 890


# ================================================================== D. missed-run sentinel
def _runs(root: Path, *rows: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "runs.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.mark.parametrize("now,expected", [
    (at(12, 24, 59), "NOT_YET_DUE"),
    (at(12, 29, 59), "NOT_YET_DUE"),
    (at(12, 30, 0), "NEVER_LAUNCHED"),
    (at(23, 0) + timedelta(days=3), "NEVER_LAUNCHED"),
])
def test_a_day_with_no_trace_is_not_due_until_12_30_then_never_launched(tmp_path, now, expected):
    s = run_status(D, now, tmp_path)
    assert s["status"] == expected and s["launch"] == "NONE"
    assert not (tmp_path / D.isoformat()).exists(), "the sentinel never creates a day"


def test_a_started_run_is_running_until_its_heartbeat_goes_stale(tmp_path):
    _runs(tmp_path, {"event": "start", "day": D.isoformat(), "started_at": "2026-10-01T12:25:01Z"})
    assert run_status(D, at(12, 26), tmp_path)["status"] == "RUNNING"   # no heartbeat yet, young
    assert run_status(D, at(12, 40), tmp_path)["status"] == "DIED"      # no heartbeat, old
    day = tmp_path / D.isoformat()
    day.mkdir()
    (day / "heartbeat.json").write_text(json.dumps({"state": "recording", "at_ts": at(15, 0).timestamp()}))
    assert run_status(D, at(15, 2), tmp_path)["status"] == "RUNNING"
    assert run_status(D, at(15, 4), tmp_path)["status"] == "DIED", "stale beyond 180 s"
    s = run_status(D, at(15, 2), tmp_path)
    assert s["launch"] == "ON_TIME" and s["startup_delay_s"] == 1.0


@pytest.mark.parametrize("start,launch", [("2026-10-01T12:30:00Z", "ON_TIME"), ("2026-10-01T12:30:01Z", "LATE")])
def test_launch_is_late_after_the_window_opens(tmp_path, start, launch):
    _runs(tmp_path, {"event": "start", "day": D.isoformat(), "started_at": start})
    assert run_status(D, at(12, 31), tmp_path)["launch"] == launch


@pytest.mark.parametrize("end,expected", [
    ({"status": "SUCCESS", "exit_code": 0}, "COMPLETED"),
    ({"status": "INTENTIONAL_SKIP", "exit_code": 6}, "INTENTIONAL_SKIP"),
    ({"status": "SIGNALLED", "exit_code": 143}, "FAILED"),
    ({"status": "RECORDER_FAILURE", "exit_code": 4}, "FAILED"),
])
def test_the_terminal_record_decides_a_finished_run(tmp_path, end, expected):
    _runs(tmp_path, {"event": "start", "day": D.isoformat(), "started_at": "2026-10-01T12:25:01Z"},
          {"event": "end", "day": D.isoformat(), "ended_at": "2026-10-02T02:00:00Z", **end})
    s = run_status(D, at(3, 0, day=D + timedelta(days=1)), tmp_path)
    assert s["status"] == expected and s["terminal"]["exit_code"] == end["exit_code"]


def test_final_statuses_are_recorded_once_for_past_days_only(tmp_path):
    _runs(tmp_path, {"event": "start", "day": D.isoformat(), "started_at": "2026-10-01T12:25:01Z"},
          {"event": "end", "day": D.isoformat(), "status": "SUCCESS", "exit_code": 0})
    now = at(3, 0, day=D + timedelta(days=2))   # day 1 and day 2 are over; day 3 has not begun
    written = record_final_statuses(tmp_path, D, now)
    assert [(w["day"], w["status"]) for w in written] == [("2026-10-01", "COMPLETED"),
                                                         ("2026-10-02", "NEVER_LAUNCHED")]
    assert record_final_statuses(tmp_path, D, now) == [], "idempotent"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run-status.jsonl", "runs.jsonl"]


# ================================================================== E. day manifest and qualification
@pytest.fixture(scope="module")
def outage_day(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("outage")

    async def main():
        clock = VirtualClock(START, scale=1.0)
        venue = FakeVenue(clock)
        venue.outage = (START + timedelta(seconds=300), START + timedelta(seconds=360))
        rec = make_recorder(tmp, clock, venue, stop_minutes=0)
        rec.stop_at = START + timedelta(minutes=12)
        return await rec.run()

    run_virtual(main())
    return tmp / "day"


def test_the_manifest_reports_a_recovered_outage(outage_day):
    m = build_manifest(outage_day)
    assert m["integrity"]["verified"] and m["integrity"]["read_to"] == "health.json"
    assert m["integrity"]["duplicate_trade_ids"] == 0 and m["integrity"]["duplicate_book_reads"] == 0
    assert m["integrity"]["tape_continuity_breaks"] == 0 and not m["integrity"]["burst_lanes"]
    assert m["faults"]["books"].get("dns", 0) > 0 and m["faults"]["tape"].get("dns", 0) > 0
    gaps = m["coverage"]["book_gaps"]
    # the outage (12:30-12:31) recovered; the shakedown then stopped at 12:37, so the
    # rest of the window is one gap that never recovered
    assert gaps["count"] == 2 and gaps["recovered"] == 1
    first, last = gaps["runs"]
    assert first[0] in (0, 1), "tick 0 may be read up to 250 ms early, before the outage"
    assert 55 <= first[1] - first[0] + 1 <= 65
    assert gaps["unrecovered_seconds"] == (last[1] - last[0] + 1) * 1.0 > 40000
    assert m["coverage"]["ok_ticks_by_lane_from_books"]["book-A"] == m["coverage"]["ok_ticks_by_lane_recorded"]["book-A"]
    assert m["lifecycle"]["termination"] == "closed"
    assert m["qualification"]["status"] == "NOT_COUNTED" and m["identity"]["tag"] == "shakedown"


def test_the_manifest_is_deterministic(outage_day):
    assert json.dumps(build_manifest(outage_day), sort_keys=True) == json.dumps(build_manifest(outage_day),
                                                                               sort_keys=True)


def _day_with_health(tmp_path: Path, **health) -> Path:
    day = tmp_path / "2026-10-01"
    day.mkdir()
    (day / "study.json").write_text(json.dumps({"tag": "study", "day": "2026-10-01"}))
    (day / "health.json").write_text(json.dumps({"streams": {}, **health}))
    return day


@pytest.mark.parametrize("counted,status", [(True, "COUNTED"), (False, "NOT_COUNTED")])
def test_qualification_is_the_recorders_own_pre_registered_figure(tmp_path, counted, status):
    day = _day_with_health(tmp_path, counted_day=counted, ok_ticks=35280, window_ticks=44100)
    q = qualify(day)
    assert q["status"] == status and q["source"] == "health.json" and "0.80" in q["rule"]


def test_qualification_of_days_that_did_not_record(tmp_path):
    assert qualify(tmp_path / "2026-10-01")["status"] == "NOT_RECORDED"
    day = tmp_path / "2026-10-02"
    day.mkdir()
    (day / "study.json").write_text(json.dumps({"tag": "study", "skipped": "maker study still running"}))
    assert qualify(day)["status"] == "SKIPPED"


def test_the_recorders_rule_counts_exactly_at_eighty_percent():
    """The threshold the qualification reports is the one the recorder applies: share >= 0.80."""
    src = (REPO / "packages/market-normalization/src/pmvl_markets/lip/recorder.py").read_text()
    assert "share >= self.params.counted_day_share" in src
    from pmvl_markets.lip.params import RECORDING

    assert RECORDING.counted_day_share == 0.80
    assert 35280 / 44100 >= RECORDING.counted_day_share > 35279 / 44100


def test_the_manifest_and_sentinel_cannot_import_the_modules_that_compute_money():
    tree = ast.parse((REPO / "packages/market-normalization/src/pmvl_markets/lip/daymanifest.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[-1])
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name.split(".")[-1] for a in node.names)
    assert not names & {"scoring", "fills", "simulator", "valuation", "study", "replay"}


# ================================================================== F. freeze manifest
def test_the_freeze_manifest_is_reproducible_and_detects_drift():
    from pmvl_worker.lip_cli import _freeze_drift, build_freeze

    a, b = build_freeze(None), build_freeze(None)
    assert a["semantic_digest"] == b["semantic_digest"], "unchanged files, same manifest"
    assert _freeze_drift(a) == []
    lip = "packages/market-normalization/src/pmvl_markets/lip/recorder.py"
    assert lip in a["code"]["modules"] and "scripts/record_lip_day.sh" in a["code"]["files"]
    assert "docs/lip-preregistration.md" in a["code"]["files"]
    tampered = json.loads(json.dumps(a))
    tampered["code"]["modules"][lip] = "0" * 64
    assert lip in _freeze_drift(tampered)


def test_the_suite_result_is_parsed_from_the_pytest_summary(tmp_path):
    from pmvl_worker.lip_cli import _suite_result

    log = tmp_path / "suite.log"
    log.write_text("....\n3001 passed, 1 skipped in 460.2s (0:07:40)\n")
    r = _suite_result(log)
    assert (r["passed"], r["skipped"], r["green"]) == (3001, 1, True)
    log.write_text("1 failed, 3000 passed, 1 skipped in 460s\n")
    assert _suite_result(log)["green"] is False


# ================================================================== G. failure modes
def _client(handler) -> LaneClient:
    c = LaneClient("https://x.test", name="kalshi", rate=1000.0, attempts=1)
    c._client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler))
    return c


@pytest.mark.parametrize("handler,cls", [
    (lambda r: httpx.Response(429), "http_429"),
    (lambda r: httpx.Response(503), "http_5xx"),
    (lambda r: httpx.Response(599), "http_5xx"),
    (lambda r: httpx.Response(403, text="forbidden"), "http_4xx"),
    (lambda r: httpx.Response(200, content=b'{"trades": [tru'), "malformed"),
])
def test_each_http_failure_is_raised_and_classified(handler, cls):
    async def go():
        c = _client(handler)
        try:
            with pytest.raises(ProviderError) as exc:
                await c.get_json("/markets/trades", params={"limit": 1}, use_cache=False)
            return str(exc.value)
        finally:
            await c.aclose()

    assert classify_error(asyncio.run(go())) == cls


@pytest.mark.parametrize("error,cls", [
    (httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"), "dns"),
    (httpx.ReadError("[Errno 54] Connection reset by peer"), "connection"),
    (httpx.ReadTimeout("timed out"), "timeout"),
])
def test_each_transport_failure_is_raised_and_classified(error, cls):
    def handler(request):
        raise error

    async def go():
        c = _client(handler)
        try:
            with pytest.raises(ProviderError) as exc:
                await c.get_json("/markets/trades", params={"limit": 1}, use_cache=False)
            return str(exc.value)
        finally:
            await c.aclose()

    assert classify_error(asyncio.run(go())) == cls


class PagingVenue(FakeVenue):
    """The tape answers in two pages; ``mode`` makes the second a duplicate of the first,
    or makes the cursor repeat forever."""

    def __init__(self, clock, mode):
        super().__init__(clock)
        self.mode = mode

    def answer(self, path, params):
        if path != "/markets/trades":
            return super().answer(path, params)
        now = self.clock.now()
        t = (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        rows = [{"ticker": "KXFOO-A", "trade_id": f"t{int(now.timestamp())}-{k}", "created_time": t,
                 "yes_price_dollars": "0.40", "count_fp": "1.00", "taker_side": "no"} for k in range(3)]
        if self.mode == "repeat_cursor":
            return {"trades": rows, "cursor": "same"}
        return {"trades": rows, "cursor": "" if params.get("cursor") else "page2"}


def _run_paging(tmp_path, mode):
    async def main():
        clock = VirtualClock(START, scale=1.0)
        rec = make_recorder(tmp_path, clock, PagingVenue(clock, mode), stop_minutes=0)
        rec.stop_at = START + timedelta(minutes=3)
        return await rec.run()

    run_virtual(main())
    return tmp_path / "day"


def test_a_duplicated_tape_page_is_written_once(tmp_path):
    day = _run_paging(tmp_path, "duplicate_page")
    ids = [p["trade_id"] for b in read_stream(day, "trades") for p in b["prints"]]
    assert ids and len(ids) == len(set(ids))
    assert build_manifest(day)["integrity"]["duplicate_trade_ids"] == 0


def test_a_repeating_cursor_ends_at_the_page_cap_as_a_recorded_gap(tmp_path):
    day = _run_paging(tmp_path, "repeat_cursor")
    polls = list(read_stream(day, "tape_polls"))
    assert polls and all(p["page_cap_hit"] and not p["complete"] for p in polls)
    m = build_manifest(day)
    assert m["coverage"]["tape"]["complete"] == 0 and m["coverage"]["tape"]["page_cap_hits"] == len(polls)


def test_sigterm_during_recording_checkpoints_and_leaves_a_readable_day(tmp_path):
    """The recorder's CLI path: a SIGTERM cancels the run, its finally-block writes the
    stream ends and releases the lock, and the process would exit 128 + 15."""
    from pmvl_worker.lip_cli import Signalled, run_until_signalled

    clock = VirtualClock(START, scale=0.002)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=0)
    rec.stop_at = START + timedelta(hours=2)

    async def run_then_signal():
        asyncio.get_running_loop().call_later(0.4, os.kill, os.getpid(), signal.SIGTERM)
        return await rec.run()

    with pytest.raises(Signalled) as exc:
        run_until_signalled(run_then_signal())
    assert exc.value.signum == signal.SIGTERM
    day = tmp_path / "day"
    assert not (tmp_path / "lock").exists(), "the lock was released"
    assert not (day / "health.json").exists() and (day / "ends.json").exists()
    assert json.loads((day / "heartbeat.json").read_text())["state"] == "aborted"
    ends = stream_ends(day)
    assert ends.prefix and ends["books"][0] > 0
    assert load_day(day).pairs, "the interrupted day still parses and loads"
    m = build_manifest(day)
    assert m["lifecycle"]["termination"] == "crashed" and m["integrity"]["verified"]


def test_partial_writes_at_death_do_not_corrupt_what_was_recorded(outage_day, tmp_path):
    import shutil

    day = tmp_path / "day"
    shutil.copytree(outage_day, day)
    (day / "health.json").unlink()
    (day / "ends.json.tmp").write_text('{"at": 1, "streams": {"books": ["x", 99')   # torn temp checkpoint
    (day / "heartbeat.tmp").write_text("{\"state\": \"rec")                           # torn temp heartbeat
    live = sorted(day.glob("segment-*"))[-1] / "status.jsonl"
    with live.open("a") as fh:
        fh.write('{"seq": 99999, "body": {"torn')                                     # torn last line
    assert load_day(day).pairs
    assert build_manifest(day)["integrity"]["verified"]


KILL_SCRIPT = textwrap.dedent('''
    import asyncio, sys
    from datetime import timedelta
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder
    tmp = Path({tmp!r})
    clock = VirtualClock(START, scale=0.01)
    rec = make_recorder(tmp, clock, FakeVenue(clock), stop_minutes=0)
    rec.stop_at = START + timedelta(hours=3)
    asyncio.run(rec.run())
''')


def test_after_sigkill_the_checkpoint_the_fsynced_lines_and_the_stale_lock_remain(tmp_path):
    """SIGKILL cannot be caught. What persists: every line up to the kill (each is fsynced),
    the last minute's checkpoint, the heartbeat as last written, and the lock - so the
    next invocation refuses (exit 4, recorded by the wrapper) and the sentinel reports
    the day as DIED rather than running."""
    proc = subprocess.Popen([sys.executable, "-c", KILL_SCRIPT.format(repo=str(REPO), tmp=str(tmp_path))])
    ends_file = tmp_path / "day" / "ends.json"
    deadline = time.time() + 60
    while time.time() < deadline:
        if ends_file.exists() and json.loads(ends_file.read_text())["streams"].get("books", [0, 0])[1] > 0:
            break
        time.sleep(0.1)
    time.sleep(1.0)
    proc.kill()
    proc.wait(timeout=10)
    day = tmp_path / "day"
    assert (tmp_path / "lock").exists(), "the stale lock survives a SIGKILL"
    assert not (day / "health.json").exists()
    ends = stream_ends(day)
    assert ends.prefix and ends["books"][0] > 0
    assert len(list(read_stream(day, "books"))) >= ends["books"][0], "the disk holds at least the checkpoint"
    assert load_day(day).pairs
    assert json.loads((day / "heartbeat.json").read_text())["state"] == "recording"
    clock = VirtualClock(START, scale=0.01)
    with pytest.raises(LockHeld):
        asyncio.run(make_recorder(tmp_path, clock, FakeVenue(clock)).run())
    m = build_manifest(day)
    assert m["lifecycle"]["termination"] == "crashed" and m["integrity"]["read_to"] == "checkpoint"


class HistoryVenue(FakeVenue):
    """A tape that keeps its history - every print since min_ts - like the real endpoint."""

    def answer(self, path, params):
        if path != "/markets/trades":
            return super().answer(path, params)
        now = self.clock.now()
        lo = max(int(params.get("min_ts", 0)), int(START.timestamp()) - 120)
        rows = [{"ticker": "KXFOO-A", "trade_id": f"t{s}", "yes_price_dollars": "0.40", "count_fp": "10.00",
                 "created_time": datetime.fromtimestamp(s, UTC).isoformat().replace("+00:00", "Z"),
                 "taker_side": "no", "is_block_trade": False} for s in range(lo, int(now.timestamp()))]
        return {"trades": rows[-int(params.get("limit", 1000)):], "cursor": ""}


@pytest.fixture(scope="module")
def restarted_day(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("restart")

    async def one(start, minutes):
        clock = VirtualClock(start, scale=1.0)
        rec = make_recorder(tmp, clock, HistoryVenue(clock), stop_minutes=0)
        rec.stop_at = start + timedelta(minutes=minutes)
        await rec.run()

    run_virtual(one(START, 5))
    run_virtual(one(START + timedelta(minutes=5, seconds=20), 5))
    root = tmp / "day"
    with (tmp / "runs.jsonl").open("w") as fh:   # as the wrapper would have written them
        for _ in range(2):
            fh.write(json.dumps({"event": "start", "day": "2026-09-25", "started_at": START.isoformat()}) + "\n")
    return root, tmp / "runs.jsonl"


def test_a_same_day_restart_is_flagged_with_its_duplicates(restarted_day):
    day, runs = restarted_day
    m = build_manifest(day, runs_log=runs)
    assert m["lifecycle"]["restarts"] == 1
    assert m["integrity"]["duplicate_trade_ids"] > 0, "detected, so an investigator cannot miss it"


@pytest.mark.xfail(strict=True, reason="KNOWN, NOT FIXED BEFORE THE FREEZE (see the pre-freeze audit): "
                                       "a same-day restart re-fetches the tape and the reader does not "
                                       "de-duplicate by trade_id. The runbook forbids same-day restarts.")
def test_a_same_day_restart_does_not_duplicate_prints_in_the_replay(restarted_day):
    day, _ = restarted_day
    data = load_day(day)
    md = data.market_day(data.pairs[0])
    ids = [p["trade_id"] for b in read_stream(day, "trades") for p in b["prints"]]
    assert md.trades.ts.size == len(set(ids))


def test_a_restart_never_manufactures_book_ticks(restarted_day):
    """After a restart the closing health.json knows only the second process's ticks: an
    undercount, never an overcount. The manifest shows both figures side by side."""
    day, runs = restarted_day
    m = build_manifest(day, runs_log=runs)
    recorded = m["coverage"]["ok_ticks_by_lane_recorded"]["book-A"]
    from_books = m["coverage"]["ok_ticks_by_lane_from_books"]["book-A"]
    assert recorded <= from_books
    ticks = [b["tick"] for b in read_stream(day, "books") if b.get("ok") and b.get("tick") is not None
             and b["tick"] >= 0]
    assert len(ticks) == len(set(ticks)), "no tick credited twice"


def test_after_a_wake_no_lane_bursts(tmp_path):
    clock = SleepingClock(IN_WINDOW, scale=1.0, sleep_at_s=120, sleep_for_s=900)
    _record_with_beats(tmp_path, clock=clock, minutes=20.0)
    m = build_manifest(tmp_path / "day")
    assert not m["integrity"]["burst_lanes"], m["integrity"]["max_requests_per_minute"]
    assert m["faults"]["slept_seconds"] >= 890


# ================================================================== H. shakedown presentation
def test_a_shakedown_replay_is_labelled_not_a_performance_result(outage_day, tmp_path):
    from pmvl_markets.lip.replay import write_results
    from pmvl_worker.lip_cli import replay_presentation

    p = replay_presentation(outage_day)
    assert p["label"] == "INCOMPLETE_COVERAGE_NOT_PERFORMANCE_RESULT" and p["not_performance_result"]
    assert 0 < p["recording_coverage_share"] < 0.02, "7 of 735 window minutes"
    results = [{"day": "d", "paths": {"J1|THROUGH|REG_LOW|LOW": {"MARK_1H": -1.5}}}]
    out = tmp_path / "replay.json"
    write_results(results, out, presentation=p)
    body = json.loads(out.read_text())
    assert body["presentation"]["label"] == p["label"] and body["results"] == results, "numbers untouched"


def test_a_study_day_is_labelled_pooled_only(tmp_path, outage_day):
    import shutil

    from pmvl_worker.lip_cli import replay_presentation

    day = tmp_path / "day"
    shutil.copytree(outage_day, day)
    study = json.loads((day / "study.json").read_text())
    (day / "study.json").write_text(json.dumps({**study, "tag": "study"}))
    p = replay_presentation(day)
    assert p["label"] == "STUDY_DAY_POOLED_ONLY" and not p["not_performance_result"]


def test_two_lanes_share_one_heartbeat_file_with_both_lanes(tmp_path):
    from dataclasses import replace

    async def main():
        clock = VirtualClock(IN_WINDOW, scale=1.0)
        venue = FakeVenue(clock)
        venue.programs.append(prow("p4", "KXFOO-D"))
        rec = make_recorder(tmp_path, clock, venue, stop_minutes=0)
        rec.stop_at = IN_WINDOW + timedelta(minutes=3)
        rec.params = replace(rec.params, book_lane_size=1)
        await rec.run()

    run_virtual(main())
    beat = json.loads((tmp_path / "day" / "heartbeat.json").read_text())
    assert set(beat["books"]) == {"book-A", "book-B"} and all(v["ok_ticks"] > 0 for v in beat["books"].values())


def test_a_heartbeat_or_checkpoint_that_cannot_be_written_never_stops_the_recording(tmp_path, monkeypatch):
    import pmvl_markets.lip.recorder as R

    def broken(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(R, "write_checkpoint", broken)
    real_replace = os.replace
    monkeypatch.setattr(R.os, "replace", lambda a, b: broken() if str(b).endswith("heartbeat.json")
                        else real_replace(a, b))

    async def main():
        clock = VirtualClock(IN_WINDOW, scale=1.0)
        rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=0)
        rec.stop_at = IN_WINDOW + timedelta(minutes=4)
        return await rec.run()

    health = run_virtual(main())
    assert health["ok_ticks"] > 200, "four minutes recorded despite both failing"
    assert not (tmp_path / "lock").exists()


def test_a_second_signal_does_not_cut_the_cleanup_short(tmp_path, monkeypatch):
    """Fourth check, G4: the wrapper forwards a TERM on top of the signal the process group
    already got, so two arrive a millisecond apart; the second cancel used to land inside
    the finally-block's suspending aclose and leave the lock, the segment and the
    checkpoint behind - and the next day lost to a stale lock."""
    import tests.test_lip_recorder as T
    from pmvl_worker.lip_cli import Signalled, run_until_signalled

    async def slow_aclose(self):
        await asyncio.sleep(0.05)   # httpx's aclose suspends on a live connection

    monkeypatch.setattr(T.FakeProvider, "aclose", slow_aclose)
    clock = VirtualClock(START, scale=0.002)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=0)
    rec.stop_at = START + timedelta(hours=2)

    async def run_then_two_signals():
        loop = asyncio.get_running_loop()
        loop.call_later(0.4, os.kill, os.getpid(), signal.SIGINT)
        loop.call_later(0.401, os.kill, os.getpid(), signal.SIGTERM)
        return await rec.run()

    with pytest.raises(Signalled) as exc:
        run_until_signalled(run_then_two_signals())
    assert exc.value.signum == signal.SIGINT, "the first signal decides the exit code"
    day = tmp_path / "day"
    assert not (tmp_path / "lock").exists(), "the lock was released"
    assert json.loads((day / "heartbeat.json").read_text())["state"] == "aborted"
    assert not list(day.glob("segment-*/*.jsonl")), "every segment closed"
    assert stream_ends(day)["books"] == (len(list(read_stream(day, "books"))),
                                         stream_ends(day)["books"][1]), "the checkpoint is the exact end"
