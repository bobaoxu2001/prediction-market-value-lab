"""The fifth check's two findings in the G1 correction itself (07fdf8c), one test each pair.
Both fail at 07fdf8c and pass on the follow-up.

G1-1 - a day whose recorder died is not "a day with no admitted market due at any tick"
       (Amendment 2 item 8's exogenous stop): its due ticks cover only its verified prefix,
       so like a day never recorded it resets the run of empty days.
G1-3 - the checkpoint's due record is computed from close times; a failure there must not
       escape the recorder's cleanup (segments closed, lock released) - the guarantee G4 gave.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmvl_markets.lip.store import SegmentWriter

from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder, run_virtual

UTC = timezone.utc
DAY1 = date(2026, 8, 1)


def _closed_empty_day(root: Path, d: date) -> None:
    day = root / d.isoformat()
    day.mkdir(parents=True)
    (day / "study.json").write_text(json.dumps({"tag": "study", "day": d.isoformat()}))
    (day / "health.json").write_text(json.dumps({"streams": {}, "due_ticks": 0, "ok_ticks": 0,
                                                 "counted_day": False}))


def _crashed_day_before_the_window(root: Path, d: date) -> None:
    """Killed at 12:29: its checkpoint covers no window tick, so its verified due count is 0 -
    although, for all anyone knows, markets were due all day."""
    day = root / d.isoformat()
    day.mkdir(parents=True)
    lo = datetime(d.year, d.month, d.day, 12, 30, tzinfo=UTC)
    (day / "study.json").write_text(json.dumps({
        "tag": "study", "day": d.isoformat(), "book_cadence_s": 1.0,
        "window": {"window_start": lo.isoformat(), "window_end": (lo + timedelta(hours=12, minutes=15)).isoformat()}}))
    w = SegmentWriter(day)
    t = lo.timestamp() - 60
    w.append("books", {"kind": "books", "lane": "book-A", "sent_at": t, "received_at": t + 0.2, "ok": True,
                       "tick": -60, "detail": "", "requested": ["X"], "missing": [], "markets": {}, "unchanged": []},
             at=datetime.fromtimestamp(t, UTC))
    body = {"at": t + 1, "streams": {k: list(v) for k, v in w._tails.items()},
            "due": {"lanes": ["book-A"], "cadence_s": 1.0, "window_ticks": 44100, "bound": 0, "runs": []}}
    w.close()
    (day / "ends.json").write_text(json.dumps(body))


@pytest.fixture
def calendar_root(tmp_path, monkeypatch):
    from pmvl_worker import lip_cli

    monkeypatch.setattr(lip_cli, "FORWARD", tmp_path)
    (tmp_path / "lip-gates.json").write_text(json.dumps({"day1": DAY1.isoformat()}))
    root = tmp_path / "lip-daily"
    root.mkdir()
    return lip_cli, root


def test_g1_1_a_crashed_day_does_not_count_toward_the_empty_day_stop(calendar_root):
    lip_cli, root = calendar_root
    for k in range(6):
        _closed_empty_day(root, DAY1 + timedelta(days=k))
    _crashed_day_before_the_window(root, DAY1 + timedelta(days=6))
    status = lip_cli._day_status(root / (DAY1 + timedelta(days=6)).isoformat())
    assert status["crashed"] is True and status["due_ticks"] == 0
    assert lip_cli._calendar() == (DAY1, DAY1 + timedelta(days=29)), "the run of empty days was reset"


def test_g1_1_seven_closed_empty_days_still_stop_the_study(calendar_root):
    lip_cli, root = calendar_root
    for k in range(7):
        _closed_empty_day(root, DAY1 + timedelta(days=k))
    assert lip_cli._calendar() == (DAY1, DAY1 + timedelta(days=6))


class UnparseableCloseVenue(FakeVenue):
    """From 12:27 the venue's close time stops parsing - the failure the fifth check used."""

    def answer(self, path, params):
        out = super().answer(path, params)
        if path == "/markets" and self.clock.now() >= START + timedelta(minutes=2):
            for m in out["markets"]:
                m["close_time"] = "2026-09-27 20:00 UTC"
        return out


def test_g1_3_a_failing_due_record_cannot_skip_the_recorders_cleanup(tmp_path):
    async def main():
        clock = VirtualClock(START, scale=1.0)
        rec = make_recorder(tmp_path, clock, UnparseableCloseVenue(clock), stop_minutes=0)
        rec.stop_at = START + timedelta(minutes=10)
        with pytest.raises(ValueError):
            await rec.run()   # the day dies in both versions: the close stop cannot be computed

    run_virtual(main())
    day = tmp_path / "day"
    assert not (tmp_path / "lock").exists(), "the lock was released - tomorrow is not lost"
    assert not list(day.glob("segment-*/*.jsonl")), "every segment was closed"
    assert json.loads((day / "heartbeat.json").read_text())["state"] == "aborted"
    ends = json.loads((day / "ends.json").read_text())
    assert "due" not in ends, "no due record could be computed, so none is claimed"
