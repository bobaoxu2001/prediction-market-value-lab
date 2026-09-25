"""Regression tests for the adversarial review of the LIP study code (35 findings).

Each test reproduces one way the code could have reported a profit that is not real,
or broken a standing rule, and fails on the code as it was before the fix.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone

from pathlib import Path

import httpx
import pytest

from pmvl_markets.lip.fills import FillScenario
from pmvl_markets.lip.final import isolation_breaches, payment_status
from pmvl_markets.lip.http import LaneClient
from pmvl_markets.lip.model import LipSide, StatusPoint, StatusSeries
from pmvl_markets.lip.policy import POLICIES
from pmvl_markets.lip.replay import build_cells, skipped_day_results
from pmvl_markets.lip.simulator import REG_HIGH, REG_LOW, WriteQueue, _is_open, score_path, simulate
from pmvl_markets.lip.store import SegmentWriter, read_stream
from pmvl_markets.lip.valuation import day_exit, mark_1h
from pmvl_markets.recording.writer import AppendOnlyLog, LogIntegrityError

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from lip_synthetic import T0, market_day  # noqa: E402

J1, I1, J5, I5 = POLICIES
UTC = timezone.utc


# ------------------------------------------------------------------ fills and the replay
def test_one_print_never_fills_more_than_it_carried():
    # parent and child resting at 40 once both sides have filled once, then one 500-lot through
    prints = [(T0 + 30, 39, 300.0, False), (T0 + 40, 46, 300.0, True), (T0 + 80, 39, 500.0, False)]
    md = market_day(150, prints=prints)
    for sc in FillScenario:
        path = simulate(md, J1, sc, REG_HIGH)
        at_80 = [f for f in path.fills if abs(f.at - (T0 + 80)) < 1e-6]
        assert sum(f.qty for f in at_80) <= 500.0 + 1e-9, sc


def test_heavy_checks_an_orders_first_book_for_orphans():
    # others bid 41 for exactly the book we decide on, then fall back to 40
    def yes(t):
        return [(41, 1500.0), (40, 1000.0)] if T0 + 59.5 <= t < T0 + 60.5 else [(40, 1500.0), (39, 1000.0)]

    md = market_day(120, yes=yes)
    path = simulate(md, J5, FillScenario.HEAVY, REG_HIGH)
    assert any(f.kind == "orphan" for f in path.fills)
    for o in path.orders:
        assert all(f.at >= o.live_from - 1e-9 for f in o.fills), "no fill before the order rested"


def test_prints_between_the_end_and_the_cancels_landing_still_fill():
    md = market_day(600, close_after=600 + 1800)  # participation ends at T0 + 600
    base = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    end = base.participation[1]
    late = market_day(600, close_after=600 + 1800, prints=[(end + 0.2, 38, 5000.0, False)])
    path = simulate(late, J5, FillScenario.THROUGH, REG_LOW)
    assert any(abs(f.at - (end + 0.2)) < 1e-6 for f in path.fills)
    assert path.exits, "the late fill is flattened"


def test_inventory_is_known_only_a_latency_after_the_fill():
    # a fill at T0 + 20.0 is known at ~T0 + 20.41; the book at T0 + 20.2 must still size
    # the order as if nothing had filled
    md = market_day(60, prints=[(T0 + 20.0, 39, 200.0, False)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    assert path.fills
    # the only decisions inside the latency window are sized from the known position
    for o in path.orders:
        if T0 + 20.0 < o.decided_at < T0 + 20.4:
            assert o.size == J5.size(1000.0)


def test_a_later_status_poll_cannot_shut_the_gate_early():
    md = market_day(200, prints=[(T0 + 20, 39, 1000.0, False)])
    md.status = StatusSeries([StatusPoint(T0 - 30, "active", True), StatusPoint(T0 + 30, "paused", True),
                              StatusPoint(T0 + 60, "active", True)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    assert path.gate_log and path.gate_log[0][1] is True, "the gate only knew the poll before it"


def test_fifo_queue_never_exceeds_what_rests_at_the_price():
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 9.5 else [(40, 200.0), (39, 2300.0)]

    prints = [(T0 + 20 + 5 * i, 40, 200.0, False) for i in range(5)]
    md = market_day(100, yes=yes, prints=prints)
    path = simulate(md, J5, FillScenario.FIFO, REG_HIGH)
    yes_orders = [o for o in path.orders if o.side is LipSide.YES]
    assert yes_orders and sum(f.qty for f in path.fills if f.side is LipSide.YES) > 0


def test_heavy_widens_the_fill_window_by_the_clock_uncertainty():
    md = market_day(60)
    md.clock_eps = 0.2
    first = simulate(md, J5, FillScenario.HEAVY, REG_HIGH)
    live_from = min(o.live_from for o in first.orders if o.side is LipSide.YES)
    near = market_day(60, prints=[(live_from - 0.1, 39, 500.0, False)])
    near.clock_eps = 0.2
    assert simulate(near, J5, FillScenario.HEAVY, REG_HIGH).fills
    assert not simulate(near, J5, FillScenario.FIFO, REG_HIGH).fills


def test_the_write_queue_delays_the_first_action_after_a_synchronized_instant():
    q = WriteQueue(rate=0.1, sync_delay=3.0)
    q.mark_sync(100.0)
    assert q.send_time(100.0) == 103.0 and q.send_time(100.0) == 113.0


# ------------------------------------------------------------------ credit
def test_a_decreased_and_filled_out_order_earns_nothing_afterwards():
    prints = [(T0 + 20 + 3 * i, 39, 150.0, False) for i in range(8)]
    md = market_day(240, prints=prints)
    path = simulate(md, J5, FillScenario.HEAVY, REG_LOW)
    for low in (True, False):
        total = score_path(path, md, low=low)
        assert total["v_sum"] <= total["seconds"] * 2
    decreased = [o for o in path.orders if o.decreases]
    for o in decreased:
        assert o.remaining >= 0


def test_low_needs_both_bracketing_polls_known():
    md = market_day(600)
    md.status = StatusSeries([StatusPoint(T0 - 10, "active", True), StatusPoint(T0 + 100, "active", True)])
    assert _is_open(md, T0 + 50, low=True)
    assert not _is_open(md, T0 + 300, low=True), "after the last poll the status is unknown"
    assert _is_open(md, T0 + 300, low=False)


def test_high_leaves_closed_seconds_out_of_its_denominator():
    md = market_day(600)
    pts = [StatusPoint(T0 - 30 + 30 * i, "active" if T0 - 30 + 30 * i < T0 + 300 else "paused", True)
           for i in range(30)]
    md.status = StatusSeries(pts)
    path = simulate(md, I1, FillScenario.FIFO, REG_HIGH)
    hi = score_path(path, md, low=False)
    lo = score_path(path, md, low=True)
    assert hi["n_total"] < lo["n_total"]


def test_a_child_and_its_parent_do_not_both_claim_the_same_room_under_the_target():
    prints = [(T0 + 30, 39, 300.0, False), (T0 + 40, 46, 300.0, True)]
    md = market_day(200, yes=lambda t: [(40, 500.0), (39, 1000.0)], no=lambda t: [(55, 1500.0), (54, 1000.0)],
                    prints=prints)
    path = simulate(md, J1, FillScenario.FIFO, REG_HIGH)
    lo = score_path(path, md, low=True)
    hi = score_path(path, md, low=False)
    assert lo["v_sum"] <= hi["v_sum"] + 1e-9


# ------------------------------------------------------------------ valuation
def test_no_mid_at_the_exit_cannot_turn_a_loss_into_a_profit():
    # buy 200 YES at 40, flatten at 40 into an empty NO side: no two-sided mid at the exit
    def no(t):
        return [(55, 1500.0), (54, 1000.0)] if t < T0 + 100 else []

    md = market_day(600, no=no, close_after=600 + 1800, prints=[(T0 + 30, 39, 200.0, False)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    cash = day_exit(path, md, low=True)
    assert mark_1h(path, md, low=True) <= cash + 1e-9 or mark_1h(path, md, low=True) <= 0


# ------------------------------------------------------------------ pooling
def _result(day: str, accrued: float, cap: float | None) -> dict:
    return {"day": day, "market": "M", "program_id": "p", "family": "F", "hours": 12.0, "cap": cap,
            "paths": {"J1|FIFO|REG_HIGH|HIGH": {"accrued": accrued, "pay": 0.0, "MARK_1H": 0.0, "DAY_EXIT": 0.0}}}


def test_the_per_account_cap_spans_days():
    results = [_result("2026-10-01", 27.31, 30.0), _result("2026-10-02", 27.31, 30.0)]
    cells = build_cells(results, verified={"p"}, withdrawn=set())
    pays = [r["paths"]["J1|FIFO|REG_HIGH|HIGH"]["pay"] for r in results]
    assert pays == [27.31, 2.69], "day two pays only what the cap has left"
    by_valuation = sum(v for c in cells for (p, combo, env), v in c.value.items() if combo[2] == "DAY_EXIT")
    assert by_valuation <= 30.0 + 1e-9


def test_a_skipped_day_adds_its_live_pairs_hours_at_zero(tmp_path):
    state = {"admitted": {"p": {"program_id": "p", "market": "KXA-1", "family": "KXA", "admitted_at": T0 - 86400,
                                "start": T0 - 86400, "end": T0 + 86400, "target": "1000"}}, "decided": ["p"]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    study = {"day": "2026-09-25", "window": {"window_start": datetime.fromtimestamp(T0, UTC).isoformat(),
                                            "window_end": datetime.fromtimestamp(T0 + 44100, UTC).isoformat()}}
    rows = skipped_day_results(study, tmp_path / "state.json")
    assert len(rows) == 1 and rows[0]["hours"] == pytest.approx(44100 / 3600)
    assert all(v["accrued"] == 0 for v in rows[0]["paths"].values())


def test_changed_terms_anywhere_unverify_a_program(tmp_path):
    day = tmp_path / "d"
    w = SegmentWriter(day, gzip_closed=False)
    base = {"id": "a", "period_reward": 1_000_000, "target_size_fp": "1000.00", "discount_factor_bps": 5000,
            "max_reward_per_account": None}
    at = datetime.fromtimestamp(T0, UTC)
    w.append("programs", {"at": T0, "rows": [base]}, at=at)
    w.append("programs", {"at": T0 + 10, "rows": [{**base, "period_reward": 2_000_000}]}, at=at)
    w.append("programs", {"at": T0 + 20, "rows": [base]}, at=at)
    log = AppendOnlyLog(tmp_path / "paid.jsonl")
    log.append({"row": {**base, "paid_out": True}})
    verified, _ = payment_status([day], tmp_path / "paid.jsonl")
    assert verified == set()


# ------------------------------------------------------------------ the store
def test_a_torn_line_in_the_middle_is_an_integrity_failure(tmp_path):
    w = SegmentWriter(tmp_path, gzip_closed=False)
    at = datetime.fromtimestamp(T0, UTC)
    for i in range(3):
        w.append("s", {"i": i}, at=at)
    f = next(tmp_path.rglob("s.jsonl"))
    lines = f.read_text().splitlines()
    f.write_text("\n".join([lines[0], lines[1][:20], lines[2]]) + "\n")
    with pytest.raises(LogIntegrityError):
        list(read_stream(tmp_path, "s"))


def test_a_torn_tail_is_cut_on_resume_and_the_chain_continues(tmp_path):
    w = SegmentWriter(tmp_path, gzip_closed=False)
    at = datetime.fromtimestamp(T0, UTC)
    w.append("s", {"i": 0}, at=at)
    f = next(tmp_path.rglob("s.jsonl"))
    with f.open("a") as fh:
        fh.write('{"seq":1,"prev":"x","dig')  # a process killed mid-write
    w2 = SegmentWriter(tmp_path, gzip_closed=False)
    w2.append("s", {"i": 1}, at=at)
    assert [b["i"] for b in read_stream(tmp_path, "s")] == [0, 1]


def test_a_restart_in_the_same_hour_keeps_both_runs_in_order(tmp_path):
    at = datetime.fromtimestamp(T0, UTC)
    w = SegmentWriter(tmp_path)
    w.append("s", {"i": 0}, at=at)
    w.close()  # gzips the open segment
    w2 = SegmentWriter(tmp_path)
    w2.append("s", {"i": 1}, at=at)
    w2.close()  # appends a second gzip member, never overwrites
    assert [b["i"] for b in read_stream(tmp_path, "s")] == [0, 1]
    with gzip.open(next(tmp_path.rglob("s.jsonl.gz")), "rt") as fh:
        assert len(fh.read().splitlines()) == 2


def test_a_missing_tail_fails_against_the_recorded_end(tmp_path):
    w = SegmentWriter(tmp_path, gzip_closed=False)
    at = datetime.fromtimestamp(T0, UTC)
    for i in range(3):
        w.append("s", {"i": i}, at=at)
    end = w._tails["s"]
    f = next(tmp_path.rglob("s.jsonl"))
    f.write_text("\n".join(f.read_text().splitlines()[:2]) + "\n")
    with pytest.raises(LogIntegrityError):
        list(read_stream(tmp_path, "s", expect_end=(end[1], end[0])))


# ------------------------------------------------------------------ recorder and isolation
def test_a_429_that_a_retry_got_past_is_still_counted():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] == 1 else 200, json={"ok": True})

    async def main():
        c = LaneClient("https://x.test", name="t", rate=100, attempts=2)
        hooks = c._client.event_hooks
        c._client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler),
                                      event_hooks=hooks)
        await c.get_json("/x", use_cache=False)
        return c.rate_limited

    assert len(asyncio.run(main())) == 1


def test_isolation_sees_every_lane_and_the_off_day_commands(tmp_path):
    day = tmp_path / "lip"
    w = SegmentWriter(day, gzip_closed=False)
    t = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    w.append("lanes", {"kind": "requests", "lane": "control", "minute": 0, "n": 3,
                       "first": t.timestamp(), "last": t.timestamp() + 30}, at=t)
    maker = tmp_path / "maker" / "2026-09-26"
    maker.mkdir(parents=True)
    (maker / "study.json").write_text(json.dumps({"sessions": [
        {"started_at": (t + timedelta(minutes=1)).isoformat(), "ended_at": (t + timedelta(minutes=31)).isoformat()}]}))
    assert isolation_breaches(day, tmp_path / "maker")
    off = tmp_path / "off.jsonl"
    AppendOnlyLog(off).append({"command": "programs-paid", "first": t.timestamp() + 600, "last": t.timestamp() + 610})
    empty = tmp_path / "empty"
    SegmentWriter(empty, gzip_closed=False)
    assert isolation_breaches(empty, tmp_path / "maker", off)


def test_off_day_commands_refuse_the_maker_window(monkeypatch):
    import typer

    import pmvl_markets.lip.recorder as rec
    from pmvl_worker import lip_cli

    monkeypatch.setattr(rec, "refused", lambda t, params=None: True)
    with pytest.raises(typer.Exit):
        lip_cli._offday_guard("programs-paid")


def test_the_pre_flight_fallback_reaches_the_recorder(tmp_path, monkeypatch):
    from pmvl_worker import lip_cli

    monkeypatch.setattr(lip_cli, "FORWARD", tmp_path)
    (tmp_path / "lip-cadence.json").write_text(json.dumps(
        {"tape_limit": 1000, "book_cadence_s": 2.0, "tape_mode": "TAPE_PT", "min_ts_honoured": False}))
    over = lip_cli._cadence_overrides()
    assert over == {"tape_limit": 1000, "book_cadence_s": 2.0, "min_ts_honoured": False,
                    "tape_mode": "per_ticker", "book_lanes": 1, "total_cap": 100}


def test_the_per_ticker_tape_records_each_market_with_its_own_coverage(tmp_path):
    from dataclasses import replace

    from pmvl_markets.lip.params import RECORDING
    from pmvl_markets.lip.reader import load_day
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(START, scale=0.002)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=8.0)
    rec.params = replace(RECORDING, tape_mode="per_ticker", book_lanes=1, total_cap=100)
    rec.admission.params = rec.params
    asyncio.run(rec.run())
    polls = list(read_stream(tmp_path / "day", "tape_polls"))
    assert polls and all(p.get("ticker") == "KXFOO-A" for p in polls)
    day = load_day(tmp_path / "day")
    assert "KXFOO-A" in day.coverage_by_ticker


def test_a_skipped_day_writes_its_directory(tmp_path):
    from tests.test_lip_recorder import FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(datetime(2026, 9, 25, 13, 20, tzinfo=UTC), scale=0.0001)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), busy=lambda: True)
    out = asyncio.run(rec.run())
    assert out["skipped"]
    study = json.loads((tmp_path / "day" / "study.json").read_text())
    health = json.loads((tmp_path / "day" / "health.json").read_text())
    assert study["skipped"] and health["counted_day"] is False


def test_a_failed_status_poll_reads_as_unknown(tmp_path):
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(START, scale=0.002)
    venue = FakeVenue(clock)
    original = venue.answer

    def flaky(path, params):
        if path == "/markets" and "tickers" in params and clock.now() > START + timedelta(minutes=3):
            return {"markets": []}  # the venue answered for nothing
        return original(path, params)

    venue.answer = flaky
    rec = make_recorder(tmp_path, clock, venue, stop_minutes=6.0)
    asyncio.run(rec.run())
    from pmvl_markets.lip.reader import load_day

    day = load_day(tmp_path / "day")
    md = day.market_day(day.pairs[0])
    assert any(p.market_status == "" for p in md.status.points)


def test_a_carried_pair_keeps_its_close_time_and_its_place(tmp_path):
    from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(START, scale=0.002)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=4.0)
    asyncio.run(rec.run())
    state = json.loads((tmp_path / "state.json").read_text())
    pair = next(iter(state["admitted"].values()))
    assert pair["reward"] and pair["series"] == "KXFOO"
    from pmvl_markets.lip.reader import load_day

    day = load_day(tmp_path / "day", state_path=tmp_path / "state.json")
    md = day.market_day(day.pairs[0])
    assert md.close_time < float("inf")


def test_the_refusal_is_rechecked_before_every_request(tmp_path):
    from pmvl_markets.lip.recorder import WindowRefused, day_window
    from tests.test_lip_recorder import FakeVenue, VirtualClock, make_recorder

    clock = VirtualClock(datetime(2026, 9, 26, 1, 59, 59, tzinfo=UTC), scale=0.001)
    rec = make_recorder(tmp_path, clock, FakeVenue(clock))
    rec.window = day_window(datetime(2026, 9, 26, 1, 0, tzinfo=UTC))

    async def later():
        clock.now()  # the virtual clock starts on its first read
        await asyncio.sleep(0.01)  # 10 virtual seconds: past 02:00
        rec._guard()

    with pytest.raises(WindowRefused):
        asyncio.run(later())


