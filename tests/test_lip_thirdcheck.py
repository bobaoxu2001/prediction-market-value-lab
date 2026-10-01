"""The third check of the LIP code before the freeze: one test per finding it reproduced.

F1 (a perfect day scored below 80%) and F4 (a crashed day could never be read) are
recorder findings and are tested in test_lip_recorder.py beside the recorder's fixtures.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from pmvl_markets.lip.fills import FillScenario
from pmvl_markets.lip.model import LipSide, StatusPoint, StatusSeries
from pmvl_markets.lip.policy import POLICIES
from pmvl_markets.lip.sim_params import SIM
from pmvl_markets.lip.simulator import REG_HIGH, REG_LOW, _known_open, simulate
from pmvl_markets.lip.valuation import _mark, mark_1h, side_mid

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from lip_synthetic import T0, market_day  # noqa: E402

J1, I1, J5, I5 = POLICIES
SCENARIOS = (FillScenario.THROUGH, FillScenario.FIFO, FillScenario.HEAVY)


# ------------------------------------------------------------------ F2
def _one_sided_then_rising():
    """YES mid 42.5; then NO empties and YES's bid rises to 60 (no mid) past the mark time."""
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 3000 else [(60, 1500.0), (59, 1000.0)]

    def no(t):
        if t < T0 + 3000:
            return [(55, 1500.0), (54, 1000.0)]
        if t < T0 + 3950:
            return []
        return [(35, 1500.0), (34, 1000.0)]

    return market_day(4000, yes=yes, no=no, close_after=4000 + 1800, prints=[(T0 + 30, 39, 200.0, False)])


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("regime", [REG_LOW, REG_HIGH])
def test_f2_high_s_fallback_mark_is_never_below_low_s(scenario, regime):
    """HIGH fell back to the last two-sided mid (42.5) while LOW took the held side's bid
    (60) - LOW's MARK_1H was $30 above HIGH's, breaking LOW <= HIGH per market-day."""
    md = _one_sided_then_rising()
    path = simulate(md, J5, scenario, regime)
    for f in path.fills:
        at = f.at + SIM.mark_horizon_s
        assert _mark(md, f.side, at, low=True, params=SIM) <= _mark(md, f.side, at, low=False, params=SIM)
    assert mark_1h(path, md, low=True) <= mark_1h(path, md, low=False) + 1e-9


def test_f2_low_never_exceeds_high_on_random_one_sided_stretches():
    rng = random.Random(3)
    for trial in range(40):
        cut, back = 200 + rng.randint(0, 2000), 3000 + rng.randint(0, 2000)
        lo_bid, hi_bid = rng.randint(20, 50), rng.randint(20, 90)

        def yes(t, cut=cut, lo_bid=lo_bid, hi_bid=hi_bid):
            b = lo_bid if t < cut else hi_bid
            return [(b, 1500.0), (b - 1, 1000.0)]

        def no(t, cut=cut, back=back, lo_bid=lo_bid):
            if cut <= t - T0 < back:
                return []
            return [(min(98, 100 - lo_bid - 3), 1500.0)]

        md = market_day(5000, yes=yes, no=no, close_after=5000 + 1800,
                        prints=[(T0 + 30 + 60 * k, lo_bid - 1, 150.0, bool(k % 2)) for k in range(6)])
        for scenario in SCENARIOS:
            for regime in (REG_LOW, REG_HIGH):
                path = simulate(md, J5, scenario, regime)
                assert mark_1h(path, md, low=True) <= mark_1h(path, md, low=False) + 1e-9, (trial, scenario)


# ------------------------------------------------------------------ F3
@pytest.mark.parametrize("regime", [REG_LOW, REG_HIGH])
def test_f3_a_two_sided_exit_is_charged_against_the_mid_in_both_envelopes(regime):
    """With a mid at the exit, HIGH took the walk book's bid as its reference - half a
    spread under the mid - and charged the flatten only its slippage and fee."""
    md = market_day(600, close_after=600 + 1800, prints=[(T0 + 30, 39, 200.0, False)])
    path = simulate(md, J5, FillScenario.THROUGH, regime)
    (at, side, qty, proceeds, fee), = path.exits
    mid = side_mid(md.books.before(path.participation[1]), side)   # mid_held(t_e), section 6
    assert mid is not None
    for low in (True, False):
        marks = sum(f.qty * (_mark(md, f.side, f.at + SIM.mark_horizon_s, low=low, params=SIM) - f.price) / 100
                    for f in path.fills)
        assert mark_1h(path, md, low=low) == pytest.approx(marks + proceeds - qty * mid / 100 - fee)
    assert side is LipSide.YES


# ------------------------------------------------------------------ F5
def test_f5_a_placement_landing_after_the_end_gets_its_fills():
    """The post-end loop tested 'settled' before draining the landings due by then, so a
    reprice still on the wire at the end never became an order and a print through it
    was dropped - fewer fills, which flatters whenever fills lose."""
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if t < T0 + 598.5 else [(42, 1500.0), (41, 1000.0)]

    def no(t):  # others' NO bids vanish: J5 quotes YES only, so nothing else keeps the loop going
        return [(55, 1500.0), (54, 1000.0)] if t < T0 + 100 else []

    base = simulate(market_day(600, yes=yes, no=no, close_after=2400), J5, FillScenario.THROUGH, REG_LOW,
                    writes_per_s=2.0)
    late = [o for o in base.orders if o.price == 42]
    assert late and late[0].live_from > base.participation[1], "the reprice lands after the end"
    o = late[0]
    for t_print in (o.live_from + 0.05, (o.live_from + o.fillable_until) / 2):
        md = market_day(600, yes=yes, no=no, close_after=2400, prints=[(t_print, 41, 5000.0, False)])
        path = simulate(md, J5, FillScenario.THROUGH, REG_LOW, writes_per_s=2.0)
        assert [(f.price, f.qty) for f in path.fills if f.price == 42] == [(42, 200.0)]
        assert [(s, q) for _, s, q, _, _ in path.exits] == [(LipSide.YES, 200.0)]


# ------------------------------------------------------------------ F6
PRINTS = [(T0 + 200, 39, 300.0, False), (T0 + 400, 39, 300.0, False)]


@pytest.mark.parametrize("regime", [REG_LOW, REG_HIGH])
def test_f6_a_failed_status_poll_does_not_close_the_gate(regime):
    """Amendment 2 item 4 - an unknown status stops nothing - was honoured by the status
    event and by HIGH's credit, but the gate read an unknown latest poll as closed and
    sat out the whole hour."""
    known = market_day(600, prints=PRINTS)
    unknown = market_day(600, prints=PRINTS)
    unknown.status = StatusSeries([StatusPoint(p.at, "", p.exchange_active) if abs(p.at - T0) < 1e-6 else p
                                   for p in unknown.status.points])
    assert _known_open(unknown, T0 + 1)
    a, b = simulate(known, J5, FillScenario.FIFO, regime), simulate(unknown, J5, FillScenario.FIFO, regime)
    assert a.gate_log[:1] == b.gate_log[:1] and b.gate_log[0][1] is True
    assert sum(f.qty for f in b.fills) == sum(f.qty for f in a.fills) > 0


def test_f6_a_mid_hour_admission_opens_before_its_first_poll():
    md = market_day(1800, prints=[(T0 + 600, 39, 300.0, False)])
    md.status = StatusSeries([p for p in md.status.points if p.at >= T0 + 25])
    path = simulate(md, J5, FillScenario.FIFO, REG_HIGH, window_anchor=T0 - 1800)
    assert path.gate_log and path.gate_log[0][1] is True and path.orders


def test_f6_a_known_closed_market_or_exchange_still_closes_the_gate():
    for point in (StatusPoint(T0, "paused", True), StatusPoint(T0, "", False)):
        md = market_day(600, prints=PRINTS)
        md.status = StatusSeries([point] + [p for p in md.status.points if p.at > T0 + 700])
        assert not _known_open(md, T0 + 1)
        assert not simulate(md, J5, FillScenario.FIFO, REG_HIGH).orders
