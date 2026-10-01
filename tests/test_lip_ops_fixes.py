"""The operational fixes approved on 2026-10-01 with the fifth check's decisions: an atomic
gzip of each segment, an atomic health.json, and the tape figures under TAPE_PT. No study
semantics change. Every test of a change fails on the code before it (ba2fbab).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmvl_markets.lip.store import SegmentWriter, gzip_file, read_stream
from pmvl_markets.recording.writer import LogIntegrityError

from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder, run_virtual

UTC = timezone.utc


# ================================================================== atomic gzip
def _segment_with_rows(tmp: Path, n: int = 5) -> tuple[Path, Path]:
    w = SegmentWriter(tmp / "day", gzip_closed=False)
    t = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    for i in range(n):
        w.append("books", {"i": i}, at=t + timedelta(seconds=i))
    seg = next((tmp / "day").glob("segment-*"))
    return tmp / "day", seg / "books.jsonl"


def test_gzip_is_complete_or_absent_never_truncated(tmp_path):
    day, jsonl = _segment_with_rows(tmp_path)
    gzip_file(jsonl)
    assert not jsonl.exists() and not list(jsonl.parent.glob("*.tmp"))
    assert [b["i"] for b in read_stream(day, "books")] == list(range(5))


def test_a_kill_between_the_gzip_rename_and_the_unlink_reads_cleanly(tmp_path):
    day, jsonl = _segment_with_rows(tmp_path)
    keep = jsonl.read_bytes()
    gzip_file(jsonl)
    jsonl.write_bytes(keep)                          # the .jsonl was not yet removed
    assert [b["i"] for b in read_stream(day, "books")] == list(range(5)), "no duplicate, chain intact"


def test_a_repeated_row_that_differs_is_still_an_integrity_failure(tmp_path):
    day, jsonl = _segment_with_rows(tmp_path)
    keep = jsonl.read_text().splitlines()
    gzip_file(jsonl)
    row = json.loads(keep[2])
    row["body"]["i"] = 99
    keep[2] = json.dumps(row)
    jsonl.write_text("\n".join(keep) + "\n")
    with pytest.raises(LogIntegrityError):
        list(read_stream(day, "books"))


def test_a_kill_during_the_gzip_leaves_the_old_files_and_a_stray_temp(tmp_path):
    day, jsonl = _segment_with_rows(tmp_path)
    (jsonl.parent / "books.jsonl.gz.tmp").write_bytes(b"\x1f\x8b\x08partial")
    assert [b["i"] for b in read_stream(day, "books")] == list(range(5))
    w = SegmentWriter(day)                           # a resumed writer ignores the stray file
    assert w._tails["books"][1] == 5


def test_a_restart_within_the_hour_keeps_the_earlier_member_first(tmp_path):
    day, jsonl = _segment_with_rows(tmp_path, n=3)
    gzip_file(jsonl)
    w = SegmentWriter(day, gzip_closed=False)
    t = datetime(2026, 10, 1, 12, 30, 10, tzinfo=UTC)
    for i in range(3, 6):
        w.append("books", {"i": i}, at=t)
    gzip_file(jsonl)
    with gzip.open(jsonl.with_suffix(".jsonl.gz"), "rt") as fh:
        assert len(fh.read().splitlines()) == 6
    assert [b["i"] for b in read_stream(day, "books")] == list(range(6))



REPO = Path(__file__).resolve().parents[1]
KILL_IN_GZIP = textwrap.dedent("""
    import shutil, sys, time
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    from pmvl_markets.lip import store
    src, half, ready = Path({jsonl!r}), {half}, Path({ready!r})

    def slow_copy(fsrc, fdst, length=0):
        written = 0
        while True:
            buf = fsrc.read(65536)
            if not buf:
                return
            fdst.write(buf)
            written += len(buf)
            if written >= half and not ready.exists():
                fdst.flush()
                ready.write_text("half")      # the parent kills us here, mid-copy
                time.sleep(60)

    store.shutil.copyfileobj = slow_copy
    store.gzip_file(src)
""")


def _kill_gzip_midway(tmp_path, jsonl: Path) -> None:
    """Run gzip_file on ``jsonl`` in a child process and SIGKILL it half way through the copy -
    a real interruption of the function itself, not a hand-made picture of what it leaves."""
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c", KILL_IN_GZIP.format(
        repo=str(REPO), jsonl=str(jsonl), half=jsonl.stat().st_size // 2, ready=str(ready))])
    deadline = time.time() + 60
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "the child never reached the middle of the copy"
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=10)


def _big_segment(tmp_path, n: int, existing_gz: bool = False):
    day = tmp_path / "day"
    start_i = 0
    if existing_gz:   # an earlier run in the same hour already gzipped its rows
        w = SegmentWriter(day, gzip_closed=False)
        for i in range(50):
            w.append("books", {"i": i, "pad": "y" * 500}, at=datetime(2026, 10, 1, 12, 30, tzinfo=UTC))
        gzip_file(next(day.glob("segment-*")) / "books.jsonl")
        start_i = 50
    w = SegmentWriter(day, gzip_closed=False)
    for i in range(start_i, start_i + n):
        w.append("books", {"i": i, "pad": "x" * 2000}, at=datetime(2026, 10, 1, 12, 30, tzinfo=UTC))
    return day, next(day.glob("segment-*")) / "books.jsonl"


@pytest.mark.parametrize("existing_gz", [False, True])
def test_gzip_killed_mid_copy_leaves_every_row_readable(tmp_path, existing_gz):
    """Sixth check, T1: no test exercised gzip_file itself being interrupted. Reverting it to
    the old in-place append passed every test, yet a kill mid-copy then leaves a truncated
    .gz beside the intact .jsonl and every reader raises EOFError. Here a real child process
    is killed half way through the copy: the old .gz (if any) must be untouched, nothing
    truncated may sit under the stream's name, and every row must read back in order."""
    day, jsonl = _big_segment(tmp_path, 2000, existing_gz=existing_gz)
    gz = jsonl.with_suffix(".jsonl.gz")
    before = gz.read_bytes() if gz.exists() else None
    _kill_gzip_midway(tmp_path, jsonl)
    assert jsonl.exists(), "the source is only removed after a complete rename"
    after = gz.read_bytes() if gz.exists() else None
    assert after == before, "the .gz under the stream's name was not touched by the interrupted copy"
    expected = list(range(50 + 2000)) if existing_gz else list(range(2000))
    assert [b["i"] for b in read_stream(day, "books")] == expected
    assert SegmentWriter(day)._tails["books"][1] == len(expected), "a resumed writer continues the chain"
    gzip_file(jsonl)                                      # and a later, complete gzip is clean
    assert [b["i"] for b in read_stream(day, "books")] == expected

# ================================================================== ops: atomic health.json
def test_health_json_is_replaced_whole(tmp_path, monkeypatch):
    from pmvl_markets.lip import store

    target = tmp_path / "health.json"
    store.atomic_write_text(target, '{"v": 1}\n')

    def fail(a, b):
        raise OSError("killed before the rename")

    monkeypatch.setattr(store.os, "replace", fail)
    with pytest.raises(OSError):
        store.atomic_write_text(target, '{"v": 2, "streams": {}}\n')
    assert json.loads(target.read_text()) == {"v": 1}, "the old file stands, whole"


def test_the_recorder_writes_health_json_through_the_atomic_writer(tmp_path, monkeypatch):
    from pmvl_markets.lip import recorder as R

    calls = []
    real = R.atomic_write_text
    monkeypatch.setattr(R, "atomic_write_text", lambda p, t: (calls.append(Path(p).name), real(p, t)))
    clock = VirtualClock(START, scale=0.002)
    asyncio.run(make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=2.0).run())
    assert "health.json" in calls and json.loads((tmp_path / "day" / "health.json").read_text())["streams"]


# ================================================================== ops: TAPE_PT tape figures
def test_under_tape_pt_the_manifest_and_heartbeat_report_the_per_ticker_tape(tmp_path):
    from pmvl_markets.lip.daymanifest import build_manifest

    start = START + timedelta(minutes=4, seconds=50)

    async def main():
        clock = VirtualClock(start, scale=1.0)
        rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=0)
        rec.stop_at = start + timedelta(minutes=8)
        rec.params = replace(rec.params, tape_mode="per_ticker", book_lanes=1, total_cap=100)
        return await rec.run()

    run_virtual(main())
    tape = build_manifest(tmp_path / "day")["coverage"]["tape"]
    assert tape["mode"] == "per_ticker" and tape["polls"] > 0 and tape["complete"] == tape["polls"]
    assert tape["feeds"] == 1 and tape["covered_seconds_min"] > 0
    beat = json.loads((tmp_path / "day" / "heartbeat.json").read_text())["tape"]
    assert beat["mode"] == "per_ticker" and beat["watermark"] is not None and beat["feeds"] == 1


def test_the_global_tape_figures_are_unchanged(tmp_path):
    from pmvl_markets.lip.daymanifest import build_manifest

    start = START + timedelta(minutes=4, seconds=50)

    async def main():
        clock = VirtualClock(start, scale=1.0)
        rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=0)
        rec.stop_at = start + timedelta(minutes=5)
        return await rec.run()

    run_virtual(main())
    tape = build_manifest(tmp_path / "day")["coverage"]["tape"]
    assert tape["mode"] == "global" and tape["polls"] > 0 and tape["covered_seconds_in_window"] > 200
    assert "feeds" not in tape
