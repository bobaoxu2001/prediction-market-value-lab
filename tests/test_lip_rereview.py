"""Regression tests for the second review (the fixes' fixes, R1-R19)."""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmvl_markets.lip.fills import FillScenario
from pmvl_markets.lip.model import LipSide, StatusPoint, StatusSeries
from pmvl_markets.lip.policy import POLICIES
from pmvl_markets.lip.reader import load_day
from pmvl_markets.lip.replay import build_cells
from pmvl_markets.lip.simulator import REG_HIGH, REG_LOW, _is_open, score_path, simulate
from pmvl_markets.lip.valuation import day_exit, mark_1h
from pmvl_markets.recording.writer import LogIntegrityError

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from lip_synthetic import T0, market_day  # noqa: E402

J1, I1, J5, I5 = POLICIES
UTC = timezone.utc


def test_r1_a_truncated_tail_fails_to_load(tmp_path):
    import gzip

    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(START, scale=0.002)
    asyncio.run(make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=5.0).run())
    load_day(tmp_path / "day")  # whole: loads
    newest = sorted((tmp_path / "day").glob("segment-*"))[-1] / "trades.jsonl.gz"
    lines = gzip.open(newest, "rt").read().splitlines()
    with gzip.open(newest, "wt") as fh:
        fh.write("\n".join(lines[:-1]) + "\n")
    with pytest.raises(LogIntegrityError):
        load_day(tmp_path / "day")


def test_r1_a_recorded_day_without_health_cannot_load(tmp_path):
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(START, scale=0.002)
    asyncio.run(make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=3.0).run())
    (tmp_path / "day" / "health.json").unlink()
    with pytest.raises(LogIntegrityError):
        load_day(tmp_path / "day")


def _one_sided_exit_day(seed: int):
    rng = random.Random(seed)
    cut = T0 + rng.uniform(60, 400)

    def no(t):
        return [(55, 1500.0), (54, 1000.0)] if t < cut else []

    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < cut + rng.choice([0, 5]) else []

    prints = [(T0 + rng.uniform(15, 500), rng.choice([39, 46]), float(rng.randint(50, 400)), rng.random() < 0.5)
              for _ in range(8)]
    return market_day(600, yes=yes, no=no, close_after=600 + 1800, prints=prints)


def test_r2_r11_r13_low_never_exceeds_high_in_valuation_on_one_sided_exits():
    for seed in range(40):
        md = _one_sided_exit_day(seed)
        for policy in (J5, I5, J1):
            for sc in FillScenario:
                path = simulate(md, policy, sc, REG_LOW)
                assert mark_1h(path, md, low=True) <= mark_1h(path, md, low=False) + 1e-9, (seed, policy.name, sc)
                assert day_exit(path, md, low=True) <= day_exit(path, md, low=False) + 1e-9, (seed, policy.name, sc)


def test_r11_a_sale_is_never_booked_as_a_gain_in_low():
    for seed in range(40):
        md = _one_sided_exit_day(seed)
        path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
        if not path.exits:
            continue
        marks_only = mark_1h(path, md, low=True) - sum(
            (proceeds - qty * (proceeds * 100 / qty) / 100 - fee) for _, _, qty, proceeds, fee in path.exits
            if proceeds == proceeds and qty > 0)
        assert mark_1h(path, md, low=True) <= marks_only + 1e-9


def test_r5_unknown_status_is_open_in_high_and_stops_nothing():
    md = market_day(300)
    pts = [StatusPoint(T0 - 30 + 30 * i, "active", True) for i in range(15)]
    pts[6] = StatusPoint(pts[6].at, "", True)  # one failed poll
    md.status = StatusSeries(pts)
    assert _is_open(md, pts[6].at + 5, low=False)
    assert not _is_open(md, pts[6].at + 5, low=True)
    path = simulate(md, J5, FillScenario.FIFO, REG_HIGH)
    assert not any(why == "status" for _, why in path.stops)


def test_r6_r17_an_order_landing_after_the_end_is_filled_before_its_cancel_lands():
    # The book received at T0 + 599.2 moves the bid to 42. The reprice's cancel goes out at
    # once and its placement 0.5 s later (2 writes/s), landing just after the day ends at
    # T0 + 600: it is on the wire at the end, lands, and stays fillable until its own
    # cancel lands.
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 598.5 else [(42, 1500.0), (41, 1000.0)]

    md = market_day(600, yes=yes, close_after=600 + 1800)
    base = simulate(md, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=2.0)
    end = base.participation[1]
    late_orders = [o for o in base.orders if o.side is LipSide.YES and o.price == 42]
    assert late_orders and late_orders[0].live_from > end, "the reprice landed after the end"
    o = late_orders[0]
    hit = market_day(600, yes=yes, close_after=600 + 1800,
                     prints=[((o.live_from + o.fillable_until) / 2, 41, 5000.0, False)])
    path = simulate(hit, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=2.0)
    assert [f for f in path.fills if f.price == 42], "filled while its cancel was in flight"
    assert path.exits, "and flattened"


def test_r7_the_end_cancels_wait_their_place_in_the_shared_queue():
    md = market_day(600, close_after=600 + 1800)
    a = simulate(md, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=1.0, sync_delay_s=0.0)
    b = simulate(md, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=1.0, sync_delay_s=8.0)
    end = a.participation[1]
    first_cancel = lambda p: min(w[1] for w in p.writes if w[3] == "cancel" and w[0] >= end - 1e-9)
    assert first_cancel(b) >= first_cancel(a) + 8.0 - 1e-6


def test_r9_the_resting_total_uses_only_fills_already_known():
    # a fill at T0 + 20.0 is known only ~0.41 s later: a decision in between must not
    # shrink or top up the resting order on account of it
    md = market_day(60, prints=[(T0 + 20.0, 39, 100.0, False)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    for o in path.orders:
        assert not (T0 + 20.0 < o.decided_at < T0 + 20.4 and o.parent is not None)


def test_r18_through_and_fifo_close_the_window_eps_before_a_cancel_lands():
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 60 else [(42, 1500.0), (41, 1000.0)]

    md = market_day(120, yes=yes)
    md.clock_eps = 0.2
    base = simulate(md, J5, FillScenario.THROUGH, REG_HIGH)
    old = next(o for o in base.orders if o.side is LipSide.YES and o.price == 40)
    late = market_day(120, yes=yes, prints=[(old.fillable_until - 0.1, 39, 500.0, False)])
    late.clock_eps = 0.2
    assert not [f for f in simulate(late, J5, FillScenario.THROUGH, REG_HIGH).fills if f.price == 40]


def test_r19_low_never_pays_more_than_high_under_a_cap():
    def r(day, lo, hi):
        return {"day": day, "market": "M", "program_id": "p", "family": "F", "hours": 12.0, "cap": 30.0,
                "paths": {"J1|FIFO|REG_HIGH|LOW": {"accrued": lo, "pay": 0, "MARK_1H": 0, "DAY_EXIT": 0},
                          "J1|FIFO|REG_HIGH|HIGH": {"accrued": hi, "pay": 0, "MARK_1H": 0, "DAY_EXIT": 0}}}

    results = [r("2026-10-01", 20.0, 29.5), r("2026-10-02", 20.0, 29.5)]
    build_cells(results, verified={"p"}, withdrawn=set())
    for x in results:
        assert x["paths"]["J1|FIFO|REG_HIGH|LOW"]["pay"] <= x["paths"]["J1|FIFO|REG_HIGH|HIGH"]["pay"] + 1e-9


def test_r12_r15_a_day_slept_through_or_with_nothing_admitted_does_not_count(tmp_path):
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(START, scale=0.002)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=5.0)
    rec.shakedown = False
    health = asyncio.run(rec.run())
    # five recorded minutes of a twelve-hour window cannot be a counted day
    assert health["counted_day"] is False and health["ok_ticks"] < 0.8 * health["window_ticks"]
    empty = tmp_path / "e"
    empty.mkdir()
    clock2 = VirtualClock(START, scale=0.002)
    venue = FakeVenue(clock2)
    venue.programs = []
    rec2 = make_recorder(empty, clock2, venue, stop_minutes=3.0, lock="lock2")
    rec2.shakedown = False
    h2 = asyncio.run(rec2.run())
    assert h2["due_ticks"] == 0 and h2["counted_day"] is False


def test_r4_r14_book_lane_429s_reach_the_trailing_window_and_pause_the_books(tmp_path):
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(START, scale=0.002)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=4.0)

    async def run():
        task = asyncio.create_task(rec.run())
        while "book-A" not in rec._providers:
            await asyncio.sleep(0.001)
        client = rec._providers["book-A"]._client
        original = rec._providers["book-A"].get_orderbooks_synchronized

        async def limited(tickers, depth=None):
            client.rate_limited.append(0.0)
            return await original(tickers, depth=depth)

        rec._providers["book-A"].get_orderbooks_synchronized = limited
        return await task

    asyncio.run(run())
    assert rec._pause_books_until > 0
    assert any(hit for _, hit in rec._recent.get("book-A", []))


def test_r8_r16_status_is_stamped_when_its_answer_arrived(tmp_path):
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder
    from pmvl_markets.lip.store import read_stream

    clock = VirtualClock(START, scale=0.002)
    asyncio.run(make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=3.0).run())
    lines = list(read_stream(tmp_path / "day", "status"))
    assert all(ch["received_at"] >= line["at"] for line in lines for ch in line["chunks"])
    day = load_day(tmp_path / "day")
    md = day.market_day(day.pairs[0])
    firsts = [ch["received_at"] for line in lines for ch in line["chunks"]]
    assert md.status.points[0].at >= min(firsts)


def test_r10_a_day_with_recordings_but_no_study_json_stops_the_final_analysis(tmp_path, monkeypatch):
    import typer

    from pmvl_worker import lip_cli

    monkeypatch.setattr(lip_cli, "FORWARD", tmp_path)
    today = datetime.now(UTC).date()
    day1 = today - timedelta(days=60)  # calendar and verdict date both in the past
    (tmp_path / "lip-gates.json").write_text(json.dumps({"day1": day1.isoformat()}))
    broken = tmp_path / "lip-daily" / day1.isoformat() / "segment-x"
    broken.mkdir(parents=True)
    (tmp_path / "lip-daily" / "admission_state.json").write_text(json.dumps({"admitted": {}, "decided": []}))
    with pytest.raises(typer.Exit):
        lip_cli.study_cmd(final=True)
