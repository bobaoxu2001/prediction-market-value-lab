"""The 2026-10-01 decisions on the fifth check's open questions
(docs/lip-prefreeze-audit.md; the dated correction note under Amendment 3).

F1   - LOW's 1-hour mark takes a bid only from a book in the mark window or at an OBSERVED
       second; a bid from before a gap covering the window, or spanning the close, is stale
       and LOW takes 0. HIGH unchanged.
F2   - the exit is charged against mid_held(t_e) at participation end, with section 5.1's
       test on t_e's second; the walk is unchanged.
G1-2 - a market is due while one of its own pairs is live: what the lanes read.

Every test of a change fails on the code before it (ba2fbab).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
import random
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmvl_markets.lip.fills import FillScenario
from pmvl_markets.lip.model import LipSide
from pmvl_markets.lip.policy import POLICIES
from pmvl_markets.lip.recorder import LaneStats, LipRecorder, day_window
from pmvl_markets.lip.sim_params import SIM
from pmvl_markets.lip.simulator import REG_HIGH, REG_LOW, _BookIndex, simulate
from pmvl_markets.lip.store import SegmentWriter, gzip_file, read_stream
from pmvl_markets.lip.universe import AdmittedPair
from pmvl_markets.lip.valuation import _exit_charge, _mark, mark_1h, side_mid
from pmvl_markets.recording.writer import LogIntegrityError

from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder, run_virtual

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from lip_synthetic import T0, market_day  # noqa: E402

UTC = timezone.utc
J5 = POLICIES[2]
H = SIM.mark_horizon_s


def observed(md, k):
    return _BookIndex(md).bracket(k, SIM) is not None


# ================================================================== F1
def _rally_and_reverse():
    """One YES fill 200 @ 40 at T0+30. The price rallies to 60/35 at T0+3000, reverses to
    20/75 at T0+3300; the mark window is [T0+3630, T0+3930]."""
    def yes(t):
        if t < T0 + 3000:
            return [(40, 1500.0), (39, 1000.0)]
        return [(60, 1500.0), (59, 1000.0)] if t < T0 + 3300 else [(20, 1500.0), (19, 1000.0)]

    def no(t):
        if t < T0 + 3000:
            return [(55, 1500.0), (54, 1000.0)]
        return [(35, 1500.0), (34, 1000.0)] if t < T0 + 3300 else [(75, 1500.0), (74, 1000.0)]

    return yes, no


def test_f1_low_takes_no_bid_from_before_a_gap_covering_the_mark_window():
    yes, no = _rally_and_reverse()
    md = market_day(4720, yes=yes, no=no, close_after=4720 + 3600, gaps=[(T0 + 3100, T0 + 4500)],
                    prints=[(T0 + 30, 39, 200.0, False)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    f = path.fills[0]
    t = f.at + H
    assert not md.books.received_between(t, t + SIM.mark_search_s), "nothing in the window"
    assert _mark(md, f.side, t, low=True, params=SIM) == 0.0, "the stale 60 bid is not to be had"
    assert _mark(md, f.side, t, low=False, params=SIM) == 62.5, "HIGH unchanged: the last mid"
    assert mark_1h(path, md, low=True) <= mark_1h(path, md, low=False)


def test_f1_a_one_sided_book_inside_the_window_still_gives_its_bid():
    """No two-sided book in the window, but books are there: the held side's bid is real."""
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 3000 else [(33, 1500.0), (32, 1000.0)]

    def no(t):
        return [(55, 1500.0), (54, 1000.0)] if t < T0 + 3000 else []

    md = market_day(4720, yes=yes, no=no, close_after=4720 + 3600, prints=[(T0 + 30, 39, 200.0, False)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    f = path.fills[0]
    assert _mark(md, f.side, f.at + H, low=True, params=SIM) == 33.0


def _close_day(gap):
    """The market closes at T0+2700 (before the fill's mark time) and has not settled."""
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 2400 else [(10, 1500.0), (9, 1000.0)]

    def no(t):
        return [(55, 1500.0), (54, 1000.0)] if t < T0 + 2400 else [(85, 1500.0), (84, 1000.0)]

    return market_day(3600, yes=yes, no=no, close_after=2700, gaps=[gap] if gap else [],
                      prints=[(T0 + 30, 39, 200.0, False)])


def test_f1_the_close_branch_takes_no_bid_across_a_gap_spanning_the_close():
    md = _close_day((T0 + 2100, T0 + 3000))
    assert md.close_time == pytest.approx(T0 + 2700) and not observed(md, math.floor(md.close_time))
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    f = path.fills[0]
    assert _mark(md, f.side, f.at + H, low=True, params=SIM) == 0.0, "the 40 bid before the gap is stale"


def test_f1_the_close_branch_takes_the_last_bid_at_an_observed_close():
    md = _close_day(None)
    assert observed(md, math.floor(md.close_time))
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
    f = path.fills[0]
    assert _mark(md, f.side, f.at + H, low=True, params=SIM) == 10.0


def test_f1_across_random_gaps_low_marks_only_bids_there_to_be_had():
    rng = random.Random(17)
    checked = 0
    for trial in range(40):
        a = rng.randint(3300, 3700)
        gap = (T0 + a, T0 + a + rng.randint(30, 900))
        up = rng.randint(-15, 25)

        def yes(t, up=up):
            return [(40, 1500.0), (39, 1000.0)] if t < T0 + 3200 else [(40 + up, 1500.0), (39 + up, 1000.0)]

        def no(t, up=up, a=a):
            if T0 + a - 200 <= t < T0 + a:
                return []                      # one-sided just before the gap
            return [(55, 1500.0), (54, 1000.0)] if t < T0 + 3200 else [(55 - up, 1500.0), (54 - up, 1000.0)]

        md = market_day(4800, yes=yes, no=no, close_after=4800 + 3600, gaps=[gap],
                        prints=[(T0 + 30, 39, 200.0, False)])
        path = simulate(md, J5, FillScenario.THROUGH, REG_LOW)
        for f in path.fills:
            t = f.at + H
            low = _mark(md, f.side, t, low=True, params=SIM)
            window = md.books.received_between(t, t + SIM.mark_search_s)
            if not window and not observed(md, math.floor(t + SIM.mark_search_s)):
                assert low == 0.0, trial
                checked += 1
            assert low <= _mark(md, f.side, t, low=False, params=SIM)
        assert mark_1h(path, md, low=True) <= mark_1h(path, md, low=False) + 1e-9
    assert checked > 5


# ================================================================== F2
def _queue_day(move_at, gaps=()):
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < move_at else [(30, 1500.0), (29, 1000.0)]

    def no(t):
        return [(55, 1500.0), (54, 1000.0)] if t < move_at else [(65, 1500.0), (64, 1000.0)]

    return market_day(700, yes=yes, no=no, close_after=700 + 3 * 3600, gaps=list(gaps),
                      prints=[(T0 + 30, 39, 200.0, False)])


def _charge(path, md, low):
    return _exit_charge(path, md, {LipSide.YES: [0.0, 0.0], LipSide.NO: [0.0, 0.0]}, low=low, params=SIM)


def test_f2_the_exit_is_charged_against_the_mid_at_participation_end():
    """REG_LOW's end-of-day queue delays the flatten 42 s; the price drops at T0+710, after
    t_e = T0+700 and before the arrival. Section 6 charges against mid_held(t_e) = 42.5 -
    the code had used the mid at the arrival, 32.5, and dropped the drift."""
    md = _queue_day(T0 + 710)
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=10 / 160, sync_delay_s=10.0)
    t_e = path.participation[1]
    (at, side, qty, proceeds, fee), = path.exits
    assert at - t_e > 30 and side_mid(md.books.before(t_e), side) == 42.5
    for low in (True, False):
        assert _charge(path, md, low) == pytest.approx(proceeds - qty * 42.5 / 100 - fee)


def test_f2_a_gap_over_t_e_is_the_no_mid_rule_even_when_the_arrival_is_observed():
    md = _queue_day(T0 + 710, gaps=[(T0 + 695, T0 + 715)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=10 / 160, sync_delay_s=10.0)
    t_e = path.participation[1]
    (at, side, qty, proceeds, fee), = path.exits
    assert not observed(md, math.floor(t_e)) and observed(md, math.floor(at))
    sale = proceeds * 100 / qty
    assert _charge(path, md, True) == pytest.approx(proceeds - qty * max(42.5, sale) / 100 - fee)
    assert _charge(path, md, True) <= -fee + 1e-9, "LOW: never a gain"
    assert _charge(path, md, False) >= _charge(path, md, True)


def test_f2_a_gap_over_the_arrival_alone_does_not_change_an_observed_t_e():
    md = _queue_day(T0 + 900, gaps=[(T0 + 735, T0 + 760)])
    path = simulate(md, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=10 / 160, sync_delay_s=10.0)
    t_e = path.participation[1]
    (at, side, qty, proceeds, fee), = path.exits
    assert observed(md, math.floor(t_e))
    mid = side_mid(md.books.before(t_e), side)
    for low in (True, False):
        assert _charge(path, md, low) == pytest.approx(proceeds - qty * mid / 100 - fee)


def test_f2_the_last_two_sided_mid_candidate_is_taken_at_or_before_t_e():
    """Across a gap over t_e, item 11's 'last two-sided mid' is the last one by t_e - not a
    later book's, which would be an observation from after the registered reference time."""
    def yes(t):
        return [(40, 1500.0)] if t < T0 + 690 else [(30, 1500.0)] if t < T0 + 720 else [(20, 1500.0)]

    def no(t):
        return [(55, 1500.0)] if t < T0 + 690 else [(65, 1500.0)] if t < T0 + 720 else [(75, 1500.0)]

    md = market_day(800, yes=yes, no=no, gaps=[(T0 + 695, T0 + 705)])
    t_e = T0 + 700.0
    path = SimpleNamespace(exits=[(T0 + 742.0, LipSide.YES, 100.0, 19.0, 1.0)], participation=(T0, t_e))
    assert not observed(md, math.floor(t_e)) and observed(md, math.floor(T0 + 742))
    # by t_e the last mid is 32.5 (the T0+690 drop); the later 22.5 is no candidate. LOW takes
    # the highest of 32.5 (reference and last mid) and 20 (the walk's bid) and the sale's 19
    assert _charge(path, md, True) == pytest.approx(19.0 - 100.0 * 32.5 / 100 - 1.0)


# ================================================================== G1-2
def _recorder_with(pairs):
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    rec = LipRecorder(out_dir=tmp / "d", state_path=tmp / "s.json", settlements_path=tmp / "x.jsonl")
    rec.window = day_window(datetime(2026, 10, 1, 13, 0, tzinfo=UTC))
    rec.params = replace(rec.params, book_lane_size=1)
    rec.lanes = {"book-A": LaneStats(), "book-B": LaneStats()}
    for pid, market, a, b in pairs:
        rec.admission.admitted[pid] = AdmittedPair(pid, market, "F", a, a, b, "1000")
    return rec


def test_g1_2_a_market_between_two_of_its_pairs_is_not_due():
    """B's first pair ends at 19:30 (+ mark tail), its second is admitted at 21:05. In
    between, the lanes read A alone: one lane due, not two."""
    lo = datetime(2026, 10, 1, 12, 30, tzinfo=UTC).timestamp()
    rec = _recorder_with([("p1", "A", lo - 300, lo + 86400), ("p2", "B", lo - 300, lo + 3 * 3600),
                          ("p3", "B", lo + 8 * 3600 + 35 * 60, lo + 86400)])
    tail = rec.params.mark_tail_s
    due = dict(rec._due_lanes(44100))
    gap_from, gap_to = int(3 * 3600 + tail), 8 * 3600 + 35 * 60
    assert due[gap_from - 1] == 2 and due[gap_from] == 1 and due[gap_to - 1] == 1 and due[gap_to] == 2
    assert all(rec.recorded_markets(lo + i) == ["A"] for i in (gap_from, (gap_from + gap_to) // 2, gap_to - 1))


def test_g1_2_overlapping_pairs_of_one_market_count_once():
    lo = datetime(2026, 10, 1, 12, 30, tzinfo=UTC).timestamp()
    rec = _recorder_with([("p1", "A", lo - 300, lo + 86400), ("p2", "A", lo + 600, lo + 86400)])
    assert {n for _, n in rec._due_lanes(3600)} == {1}, "one market: one lane, never two"
