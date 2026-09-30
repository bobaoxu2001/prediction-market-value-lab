"""The fourth check's three semantic findings (docs/lip-prefreeze-audit.md), one section each.
Every test here fails on the code at ce24618 and passes on the correction.

G1 - a day whose recorder died is judged by the registered counted-day rule (Amendment 2
     item 6, applied to its verified books - Amendment 3.3), in both directions.
G2 - a day-end exit across a book gap has no observed mid (section 5.1), so the no-mid rule
     applies (Amendment 2 item 11) and LOW never books a gain from a stale price.
G3 - a checkpoint certifies exactly the streams it lists; an empty one certifies nothing,
     and a pair of which nothing was verified enters at its admitted hours and $0.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmvl_markets.lip.fills import FillScenario
from pmvl_markets.lip.health import day_health
from pmvl_markets.lip.model import LipSide
from pmvl_markets.lip.policy import POLICIES
from pmvl_markets.lip.reader import load_day
from pmvl_markets.lip.replay import replay_day, skipped_day_results
from pmvl_markets.lip.sim_params import SIM
from pmvl_markets.lip.simulator import REG_HIGH, REG_LOW, _BookIndex, simulate
from pmvl_markets.lip.store import SegmentWriter, read_checked, read_stream, stream_ends
from pmvl_markets.lip.universe import AdmittedPair
from pmvl_markets.lip.valuation import _exit_charge, mark_1h, side_mid
from pmvl_markets.providers.kalshi import SynchronizedBooks
from pmvl_markets.recording.writer import LogIntegrityError

import tests.test_lip_recorder as TR
from tests.test_lip_recorder import FakeVenue, VirtualClock, make_recorder, prow, run_virtual

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from lip_synthetic import T0, market_day  # noqa: E402

UTC = timezone.utc
J5 = POLICIES[2]
START = TR.START


def observed_5_1(md, k, params=SIM) -> bool:
    """The reference: section 5.1 as the credit computation implements it."""
    return _BookIndex(md).bracket(k, params) is not None


def item6_reference(rec, bound: int) -> int:
    """The reference: Amendment 2 item 6 from the recorder's own state, written out here
    independently of the recorder's helpers - the markets live at each tick fill the lanes
    in order; a tick succeeds only if every due lane read it."""
    lo, c = rec.window.window_start.timestamp(), rec.params.book_cadence_s
    size = rec.params.book_lane_size
    names = [n for n in ("book-A", "book-B") if n in rec.lanes]
    spans: dict[str, tuple[float, float]] = {}
    for pair in rec.admission.admitted.values():
        a, b = pair.admitted_at, rec._participation_end(pair) + rec.params.mark_tail_s
        o = spans.get(pair.market)
        spans[pair.market] = (min(a, o[0]), max(b, o[1])) if o else (a, b)
    ok = 0
    for i in range(bound):
        t = lo + i * c
        n = min(sum(1 for a, b in spans.values() if a <= t < b), len(names) * size)
        due = [nm for j, nm in enumerate(names) if n > j * size]
        if due and all(i in rec._ok_ticks.get(nm, set()) for nm in due):
            ok += 1
    return ok


# ================================================================== G1
def _synthetic_crashed_day(tmp: Path, ok_ticks: dict[str, set[int]], due: dict | None, n_ticks: int = 100) -> Path:
    """A study day whose recorder died: study.json, verified book lines, and a checkpoint -
    no health.json. The window is n_ticks seconds long so the day is small."""
    day = tmp / "day"
    lo = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    (day).mkdir(parents=True)
    (day / "study.json").write_text(json.dumps({
        "tag": "study", "day": "2026-10-01", "book_cadence_s": 1.0,
        "window": {"window_start": lo.isoformat(), "window_end": (lo + timedelta(seconds=n_ticks)).isoformat()}}))
    w = SegmentWriter(day)
    for lane in sorted(ok_ticks):
        for i in range(n_ticks):
            t = lo.timestamp() + i
            w.append("books", {"kind": "books", "lane": lane, "sent_at": t, "received_at": t + 0.2,
                               "ok": i in ok_ticks[lane], "tick": i, "detail": "", "requested": ["X"],
                               "missing": [], "markets": {}, "unchanged": []},
                     at=datetime.fromtimestamp(t, UTC))
    body = {"at": lo.timestamp() + n_ticks, "streams": {k: list(v) for k, v in w._tails.items()}}
    if due is not None:
        body["due"] = due
    w.close()
    (day / "ends.json").write_text(json.dumps(body))
    return day


def _due(runs, lanes=("book-A", "book-B"), n_ticks=100, bound=100):
    return {"lanes": list(lanes), "cadence_s": 1.0, "window_ticks": n_ticks, "bound": bound, "runs": runs}


def test_g1_lanes_missing_different_ticks_are_not_each_counted_on_their_own(tmp_path):
    """Too high before: min over lanes counted A's 88 and B's 88 successes as 0.88 - but on 24
    ticks one due lane had no read, so only 76 ticks were successful (item 6)."""
    all_ticks = set(range(100))
    day = _synthetic_crashed_day(tmp_path, {"book-A": all_ticks - set(range(0, 12)),
                                            "book-B": all_ticks - set(range(12, 24))}, _due([[0, 99, 2]]))
    h = day_health(day)
    assert h["book_success_share_of_window"] == pytest.approx(0.76) and h["counted_day"] is False
    assert h["crashed"] and h["ok_ticks"] == 76


def test_g1_a_lane_due_for_part_of_the_window_does_not_cap_the_day(tmp_path):
    """Too low before: lane B was due only for ticks 0-39, and min over lanes capped the day
    at its 40 reads (0.40) - but every due lane read every tick, so all 100 succeed."""
    day = _synthetic_crashed_day(tmp_path, {"book-A": set(range(100)), "book-B": set(range(40))},
                                 _due([[0, 39, 2], [40, 99, 1]]))
    h = day_health(day)
    assert h["book_success_share_of_window"] == pytest.approx(1.0) and h["counted_day"] is True
    assert h["ok_ticks"] == 100


@pytest.mark.parametrize("ok_through,counted", [(79, False), (80, True)])
def test_g1_the_threshold_is_the_registered_eighty_percent_of_window_ticks(tmp_path, ok_through, counted):
    day = _synthetic_crashed_day(tmp_path, {"book-A": set(range(ok_through))}, _due([[0, 99, 1]], lanes=["book-A"]))
    assert day_health(day)["counted_day"] is counted


def test_g1_ticks_after_the_checkpoint_and_ticks_with_nothing_due_are_misses(tmp_path):
    day = _synthetic_crashed_day(tmp_path, {"book-A": set(range(100))},
                                 _due([[0, 29, 1], [40, 59, 1]], lanes=["book-A"], bound=60))
    h = day_health(day)
    assert h["book_success_share_of_window"] == pytest.approx(0.50)
    assert h["ok_ticks"] == 50 and h["due_ticks"] == 50, "ticks 30-39 had nothing due, 60+ are past the checkpoint"


def test_g1_without_the_due_record_the_rule_cannot_be_applied_and_the_day_does_not_count(tmp_path):
    day = _synthetic_crashed_day(tmp_path, {"book-A": set(range(100))}, None)
    h = day_health(day)
    assert h["counted_day"] is False and h["book_success_share_of_window"] == 0.0
    assert h["rule_inputs_missing"] is True


def _flaky(p: float, seed: str):
    orig = TR.FakeProvider.get_orderbooks_synchronized
    rngs: dict[str, random.Random] = {}

    async def g(self, tickers, depth=None):
        r = rngs.setdefault(self.name, random.Random(f"{seed}|{self.name}"))
        if r.random() < p:
            s = self.clock.now()
            return SynchronizedBooks(requested=tuple(tickers), books={}, sent_at=s, received_at=s, ok=False,
                                     detail="HTTP 503")
        return await orig(self, tickers, depth)
    return g


def _two_lane_recorder(tmp_path, *, b_hours, kill_after_min=None, stop_minutes=None):
    start = START + timedelta(minutes=4, seconds=50)

    async def main():
        clock = VirtualClock(start, scale=1.0)
        venue = FakeVenue(clock)
        venue.programs.append(prow("p4", "KXFOO-D", hours=b_hours))
        rec = make_recorder(tmp_path, clock, venue, stop_minutes=0)
        rec.shakedown = False
        rec.stop_at = None if stop_minutes is None else start + timedelta(minutes=stop_minutes)
        rec.params = replace(rec.params, book_lane_size=1)
        if kill_after_min is None:
            await rec.run()
            return rec
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(kill_after_min * 60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return rec

    return run_virtual(main())


def test_g1_a_recorder_that_died_is_judged_exactly_as_its_own_rule_would_have(tmp_path, monkeypatch):
    """A real recorder, two lanes failing independently and lane B's program ending early,
    killed eight hours in: the health report's figure equals the recorder's own item-6
    count over the ticks its verified books cover."""
    monkeypatch.setattr(TR.FakeProvider, "get_orderbooks_synchronized", _flaky(0.12, "g1"))
    rec = _two_lane_recorder(tmp_path, b_hours=6, kill_after_min=8 * 60)
    day = tmp_path / "day"
    assert not (day / "health.json").exists()
    ends = stream_ends(day)
    lo = rec.window.window_start.timestamp()
    bound = min(44100, math.floor((ends.at + rec.params.book_jitter_s - lo) / rec.params.book_cadence_s) + 1)
    expected = item6_reference(rec, bound)
    assert sorted(rec.lanes) >= ["book-A", "book-B"]
    h = day_health(day)
    assert h["book_success_share_of_window"] == pytest.approx(expected / 44100)
    assert h["ok_ticks"] == expected


def test_g1_the_checkpoint_rule_reproduces_the_closing_figure_on_a_closed_day(tmp_path, monkeypatch):
    """The final checkpoint of a day that closed normally, judged by the crashed-day path,
    gives exactly the recorder's closing ok_ticks - one definition of due, one rule."""
    monkeypatch.setattr(TR.FakeProvider, "get_orderbooks_synchronized", _flaky(0.15, "close"))
    _two_lane_recorder(tmp_path, b_hours=6, stop_minutes=20)
    day = tmp_path / "day"
    closing = json.loads((day / "health.json").read_text())
    (day / "health.json").rename(day / "health.closed.json")
    h = day_health(day)
    assert h["book_success_share_of_window"] == pytest.approx(closing["book_success_share_of_window"])
    assert h["crashed"] and h["ok_ticks"] == closing["ok_ticks"]
    # due ticks after the checkpoint are unknown to it, and are misses either way
    assert h["due_ticks"] <= closing["due_ticks"]


# ================================================================== G2
def _gap_day(gap):
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 640 else [(53, 1500.0), (52, 1000.0)]

    def no(t):
        return [(55, 1500.0), (54, 1000.0)] if t < T0 + 640 else [(42, 1500.0), (41, 1000.0)]

    return market_day(600, yes=yes, no=no, close_after=600 + 3 * 3600, gaps=[gap] if gap else [],
                      prints=[(T0 + 30, 39, 200.0, False)])


def _charge(path, md, low):
    marked = {LipSide.YES: [0.0, 0.0], LipSide.NO: [0.0, 0.0]}
    return _exit_charge(path, md, marked, low=low)   # params default to SIM (1 s cadence)


@pytest.mark.parametrize("regime", [REG_LOW, REG_HIGH])
def test_g2_low_books_no_gain_from_a_reference_across_a_gap(regime):
    """A gap from T0+596 to T0+690 spans the day-end exit; the price moves inside it. The
    exit second is UNOBSERVED, so there is no mid_held(t_e): LOW took the stale mid 42.5
    against a sale at 53 and booked +$17.51. Now LOW's reference is never below the sale."""
    md = _gap_day((T0 + 596, T0 + 690))
    path = simulate(md, J5, FillScenario.THROUGH, regime)
    (at, side, qty, proceeds, fee), = path.exits
    assert not observed_5_1(md, math.floor(at))
    assert _charge(path, md, low=True) == pytest.approx(-fee), "the sale's own price: never a gain"
    assert _charge(path, md, low=False) == pytest.approx(proceeds - qty * 42.5 / 100 - fee), "HIGH: the lowest"
    assert mark_1h(path, md, low=True) <= mark_1h(path, md, low=False)


@pytest.mark.parametrize("regime", [REG_LOW, REG_HIGH])
def test_g2_an_observed_exit_is_unchanged(regime):
    md = _gap_day(None)
    path = simulate(md, J5, FillScenario.THROUGH, regime)
    (at, side, qty, proceeds, fee), = path.exits
    assert observed_5_1(md, math.floor(at))
    mid = side_mid(md.books.before(at), side)
    for low in (True, False):
        assert _charge(path, md, low) == pytest.approx(proceeds - qty * mid / 100 - fee)


@pytest.mark.parametrize("rtt,observed", [(0.5, True), (0.49, False)])
def test_g2_the_boundary_is_the_registered_span_limit(rtt, observed):
    """Books every second, received rtt later; none sent in [k, k+3). The bracket of second
    k runs from the book received at k - 1 + rtt to the one sent at k + 3: a span of
    4 - rtt s against the 3.5 s limit (2c + 1.5). Exactly 3.5 is observed; 3.51 is not."""
    k = 300

    def yes(t):
        return [(40, 1000.0)] if t < T0 + k else [(50, 1000.0)]

    def no(t):
        return [(55, 1000.0)] if t < T0 + k else [(45, 1000.0)]

    md = market_day(400, yes=yes, no=no, rtt=rtt, gaps=[(T0 + k, T0 + k + 3)])
    at = T0 + k + 0.3
    assert observed_5_1(md, math.floor(at)) is observed
    qty, proceeds, fee = 100.0, 50.0, 1.0   # sold 100 at 50 through the book after the gap
    path = SimpleNamespace(exits=[(at, LipSide.YES, qty, proceeds, fee)])
    ref_mid = side_mid(md.books.before(at), LipSide.YES)
    assert ref_mid == 42.5
    low, high = _charge(path, md, True), _charge(path, md, False)
    if observed:   # section 6: against the mid at the exit, in both envelopes
        assert low == high == pytest.approx(proceeds - qty * ref_mid / 100 - fee)
    else:          # item 11: LOW the highest reference, never below the sale; HIGH the lowest
        assert low == pytest.approx(-fee)
        assert high == pytest.approx(proceeds - qty * 42.5 / 100 - fee)


def test_g2_the_observed_test_is_the_credit_computations_on_random_books():
    from pmvl_markets.lip.valuation import _observed

    rng = random.Random(11)
    for trial in range(60):
        gaps = [(T0 + a, T0 + a + rng.randint(1, 20)) for a in sorted(rng.sample(range(5, 280), 4))]
        md = market_day(300, rtt=rng.choice([0.1, 0.3, 0.5, 0.9]), cadence=rng.choice([1.0, 2.0]), gaps=gaps)
        params = replace(SIM, cadence_s=md.books.snapshots[1].sent_at - md.books.snapshots[0].sent_at)
        for k in range(int(T0), int(T0) + 300, 3):
            assert _observed(md, k, params) == observed_5_1(md, k, params), (trial, k)


def test_g2_across_random_gaps_low_never_gains_on_an_unobserved_exit():
    rng = random.Random(5)
    seen_unobserved = 0
    for trial in range(40):
        a = rng.randint(560, 600)
        gap = (T0 + a, T0 + a + rng.randint(5, 150))
        up = rng.randint(-10, 15)

        def yes(t, up=up):
            return [(40, 1500.0), (39, 1000.0)] if t < T0 + 620 else [(40 + up, 1500.0), (39 + up, 1000.0)]

        def no(t, up=up):
            return [(55, 1500.0), (54, 1000.0)] if t < T0 + 620 else [(55 - up, 1500.0), (54 - up, 1000.0)]

        md = market_day(600, yes=yes, no=no, close_after=600 + 3 * 3600, gaps=[gap],
                        prints=[(T0 + 30, 39, 200.0, False)])
        for scenario in (FillScenario.THROUGH, FillScenario.FIFO):
            for regime in (REG_LOW, REG_HIGH):
                path = simulate(md, J5, scenario, regime)
                for exit_ in path.exits:
                    at, proceeds = exit_[0], exit_[3]
                    if math.isnan(proceeds) or observed_5_1(md, math.floor(at)):
                        continue
                    seen_unobserved += 1
                    one = SimpleNamespace(exits=[exit_])
                    assert _charge(one, md, low=True) <= 1e-9, (trial, exit_)
                assert mark_1h(path, md, low=True) <= mark_1h(path, md, low=False) + 1e-9
    assert seen_unobserved > 20


# ================================================================== G3
def _killed_day(tmp_path, kill_after_s: float, restore_after_s: float | None):
    """Run a study recorder and kill it at ``kill_after_s``; with ``restore_after_s`` put back
    the ends.json that was on disk at that moment - what a SIGKILL there would leave."""
    snap = {}

    async def main():
        clock = VirtualClock(START, scale=1.0)
        rec = make_recorder(tmp_path, clock, FakeVenue(clock), stop_minutes=0)
        rec.stop_at, rec.shakedown = None, False
        task = asyncio.create_task(rec.run())
        if restore_after_s is not None:
            await asyncio.sleep(restore_after_s)
            snap["ends"] = (tmp_path / "day" / "ends.json").read_text()
            await asyncio.sleep(kill_after_s - restore_after_s)
        else:
            await asyncio.sleep(kill_after_s)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_virtual(main())
    day = tmp_path / "day"
    if "ends" in snap:
        (day / "ends.json").write_text(snap["ends"])
    return day


STREAMS = ("books", "tape_polls", "trades", "universe", "programs", "status", "markets", "lanes")


def test_g3_an_empty_first_checkpoint_certifies_nothing(tmp_path):
    day = _killed_day(tmp_path, 45, restore_after_s=45)
    ends = stream_ends(day)
    assert ends.prefix and dict(ends) == {}
    for s in STREAMS:
        assert list(read_checked(day, s, ends)) == [], s
    assert len(list(read_stream(day, "books"))) > 0, "lines are on disk, unverified"
    h = day_health(day)
    assert h["crashed"] and h["counted_day"] is False and h["ok_ticks"] == 0 and h["admitted_pairs"] == 0


def test_g3_a_day_killed_before_its_first_valid_checkpoint_contributes_only_admitted_hours_at_zero(tmp_path):
    day = _killed_day(tmp_path, 45, restore_after_s=45)
    results = replay_day(day, state_path=tmp_path / "state.json", workers=1)
    study = json.loads((day / "study.json").read_text())
    assert results == skipped_day_results(study, tmp_path / "state.json"), "as a day never recorded (item 8)"
    assert results and all(v["accrued"] == v["MARK_1H"] == v["DAY_EXIT"] == v["fills"] == 0.0
                           for r in results for v in r["paths"].values())


def test_g3_a_valid_first_checkpoint_is_read_to_exactly_its_ends(tmp_path):
    day = _killed_day(tmp_path, 100, restore_after_s=75)
    ends = stream_ends(day)
    assert ends.prefix and ends["books"][0] > 0
    for s in ends:
        assert len(list(read_checked(day, s, ends))) == ends[s][0], s
    assert len(list(read_stream(day, "books"))) > ends["books"][0], "later lines exist and are left out"
    assert load_day(day, state_path=tmp_path / "state.json").pairs


def test_g3_a_day_with_neither_health_nor_checkpoint_still_fails_to_load(tmp_path):
    day = _killed_day(tmp_path, 45, restore_after_s=45)
    (day / "ends.json").unlink()
    with pytest.raises(LogIntegrityError):
        load_day(day)
    with pytest.raises(LogIntegrityError):
        day_health(day)


def test_g3_health_json_wins_over_a_checkpoint_and_is_read_whole(tmp_path):
    _two_lane_recorder(tmp_path, b_hours=24, stop_minutes=3)
    day = tmp_path / "day"
    assert (day / "health.json").exists() and (day / "ends.json").exists()
    ends = stream_ends(day)
    assert not ends.prefix
    assert len(list(read_checked(day, "books", ends))) == len(list(read_stream(day, "books")))


def test_g3_empty_closing_ends_do_not_certify_a_stream_either(tmp_path):
    """The same defect on the other source of ends: a health.json that lists no streams
    while stream files exist must not let them be read unverified."""
    _two_lane_recorder(tmp_path, b_hours=24, stop_minutes=3)
    day = tmp_path / "day"
    health = json.loads((day / "health.json").read_text())
    (day / "health.json").write_text(json.dumps({**health, "streams": {}}))
    with pytest.raises(LogIntegrityError):
        list(read_checked(day, "books", stream_ends(day)))


def test_g3_without_any_ends_the_optional_readers_behave_as_before(tmp_path):
    """required=False with neither file (isolation and payment checks on a partial tree):
    unchanged - nothing is certified, and the stream is read as it stands."""
    day = _killed_day(tmp_path, 45, restore_after_s=45)
    (day / "ends.json").unlink()
    ends = stream_ends(day, required=False)
    assert not ends.prefix and dict(ends) == {}
    assert len(list(read_checked(day, "lanes", ends))) == len(list(read_stream(day, "lanes")))


def _carry(state_path: Path, market: str) -> None:
    """A pair admitted on an earlier day, still live, whose market the day never recorded."""
    body = json.loads(state_path.read_text())
    lo = START.timestamp()
    body["admitted"]["p-carried"] = asdict(AdmittedPair(
        "p-carried", market, "KXCARRY", lo - 86400, lo - 86400, lo + 86400, "1000.00",
        series="KXCARRY", reward="100", discount="0.5", cap=None))
    state_path.write_text(json.dumps(body))


def test_g3_on_a_crashed_day_a_pair_with_nothing_verified_enters_at_zero(tmp_path):
    day = _killed_day(tmp_path, 100, restore_after_s=75)
    _carry(tmp_path / "state.json", "KXCARRY-Z")
    results = {r["market"]: r for r in replay_day(day, state_path=tmp_path / "state.json", workers=1)}
    assert "KXFOO-A" in results and "KXCARRY-Z" in results
    z = results["KXCARRY-Z"]
    assert z["hours"] == pytest.approx(12.25) and all(v["MARK_1H"] == 0.0 for v in z["paths"].values())


def test_g3_on_a_closed_day_a_missing_close_time_is_still_an_error(tmp_path):
    _two_lane_recorder(tmp_path, b_hours=24, stop_minutes=3)
    _carry(tmp_path / "state.json", "KXCARRY-Z")
    with pytest.raises(ValueError, match="no close time"):
        replay_day(tmp_path / "day", state_path=tmp_path / "state.json", workers=1)
