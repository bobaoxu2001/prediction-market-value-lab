"""Fills and the replay: each test is a way the simulated maker could look better than it would be."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pmvl_markets.lip.fills import (
    FillScenario,
    OrderState,
    apply_cross,
    apply_orphan,
    apply_print,
    apply_shrink,
    print_relation,
)
from pmvl_markets.lip.model import BookSide, BookSnapshot, LipSide
from pmvl_markets.lip.policy import POLICIES, allowed_sizes, targets
from pmvl_markets.lip.simulator import REG_HIGH, REG_LOW, WriteQueue, simulate

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from lip_synthetic import T0, market_day  # noqa: E402

J1, I1, J5, I5 = POLICIES


def order(side=LipSide.YES, price=40, size=100.0, queue=50.0):
    return OrderState(oid=1, side=side, price=price, size=size, remaining=size, queue=queue,
                      decided_at=0.0, live_from=0.0)


# ------------------------------------------------------------------ one order
def test_prints_fill_only_the_side_the_aggressor_hit():
    assert print_relation(LipSide.YES, 40, 40, taker_yes=False) == "at"
    assert print_relation(LipSide.YES, 40, 39, taker_yes=False) == "through"
    assert print_relation(LipSide.YES, 40, 40, taker_yes=True) is None
    # a NO bid at 55 is a YES ask at 45: filled by YES buyers at 45 or above
    assert print_relation(LipSide.NO, 55, 45, taker_yes=True) == "at"
    assert print_relation(LipSide.NO, 55, 46, taker_yes=True) == "through"
    assert print_relation(LipSide.NO, 55, 44, taker_yes=True) is None


def test_an_at_level_print_smaller_than_the_queue_fills_nothing():
    o = order(queue=50)
    assert apply_print(o, FillScenario.FIFO, "at", 30, 1.0) == 0 and o.queue == 20


def test_through_never_uses_the_queue_and_fills_are_ordered_through_fifo_heavy():
    fills = {}
    for sc in FillScenario:
        o = order(queue=50)
        total = apply_print(o, sc, "at", 80, 1.0)
        total += apply_shrink(o, sc, level_now=0, unexplained=40, covered=False, at=2.0)
        total += apply_cross(o, sc, 10, 3.0)
        fills[sc] = total
    assert fills[FillScenario.THROUGH] <= fills[FillScenario.FIFO] <= fills[FillScenario.HEAVY]
    assert fills[FillScenario.THROUGH] == 0


def test_heavy_alone_reads_crosses_orphans_and_gap_shrinkage_as_fills():
    for sc in (FillScenario.THROUGH, FillScenario.FIFO):
        o = order(queue=0)
        assert apply_cross(o, sc, 100, 1) == 0 and apply_orphan(o, sc, 30, 1) == 0
        assert apply_shrink(o, sc, 0, 100, covered=False, at=1) == 0
    o = order(queue=0)
    assert apply_orphan(o, FillScenario.HEAVY, others_best=38, at=1.0) == 100


# ------------------------------------------------------------------ policies
def book(yes, no, t=0.0):
    return BookSnapshot(t, t + 0.2, BookSide.of(yes), BookSide.of(no))


def test_improve_only_with_room_and_never_self_crossing():
    wide = book([(40, 1500)], [(55, 1500)])  # YES spread 45 - 40 = 5
    assert targets(wide, I1, {}) == {LipSide.YES: 41, LipSide.NO: 56}
    narrow = book([(40, 1500)], [(58, 1500)])  # spread 2: join
    assert targets(narrow, I1, {}) == {LipSide.YES: 40, LipSide.NO: 58}
    for y in range(1, 98):
        for n in range(1, 99 - y):
            t = targets(book([(y, 10)], [(n, 10)]), I1, {})
            if t[LipSide.YES] is not None and t[LipSide.NO] is not None:
                assert t[LipSide.YES] + t[LipSide.NO] <= 99


def test_the_hold_rule_when_others_join_our_improved_price():
    b = book([(41, 1500)], [(55, 1500)])
    assert targets(b, I1, {LipSide.YES: 41})[LipSide.YES] == 41


def test_inventory_limits_the_heavy_side():
    assert allowed_sizes(300.0, 1000.0) == {LipSide.YES: 700.0, LipSide.NO: 1000.0}
    assert allowed_sizes(-1200.0, 1000.0) == {LipSide.YES: 1000.0, LipSide.NO: 0.0}


# ------------------------------------------------------------------ the replay
def test_the_gate_needs_ten_books_before_anything_is_placed():
    md = market_day(60)
    path = simulate(md, J1, FillScenario.FIFO, REG_HIGH)
    first = min(o.decided_at for o in path.orders)
    assert first >= T0 + 9 * 1.0, "ten books at 1 Hz"
    assert path.gate_log and path.gate_log[0][1] is True


def test_no_quote_when_others_are_below_target_at_the_gate():
    md = market_day(120, yes=lambda t: [(40, 600.0)])
    assert simulate(md, J1, FillScenario.FIFO, REG_HIGH).orders == []


def test_a_print_before_the_order_lands_cannot_fill_it():
    # the first order is decided at T0 + ~9.2 s and lands ~0.41 s later in REG_LOW
    md = market_day(60, prints=[(T0 + 9.4, 40, 5000.0, False)])
    path = simulate(md, J1, FillScenario.HEAVY, REG_LOW)
    yes = [o for o in path.orders if o.side is LipSide.YES]
    assert yes and yes[0].live_from > T0 + 9.4 and not yes[0].fills


def test_block_trades_are_ignored():
    md = market_day(60, prints=[(T0 + 30, 39, 5000.0, False)])
    md.trades.block[:] = True
    path = simulate(md, J1, FillScenario.HEAVY, REG_HIGH)
    assert not path.fills


def test_replenishment_waits_for_the_inventory_room_and_joins_behind_the_parent():
    # A YES fill of 300 leaves N = +300, so YES may rest only 700 - exactly what is left:
    # no child yet. A NO fill of 300 nets N back to 0, and the YES side refills.
    only_yes = simulate(market_day(120, prints=[(T0 + 30, 39, 300.0, False)]), J1, FillScenario.FIFO, REG_HIGH)
    yes = [o for o in only_yes.orders if o.side is LipSide.YES]
    assert yes[0].fills and yes[0].fills[0].qty == 300
    assert not [o for o in yes if o.parent == yes[0].oid]
    md = market_day(120, prints=[(T0 + 30, 39, 300.0, False), (T0 + 40, 46, 300.0, True)])
    path = simulate(md, J1, FillScenario.FIFO, REG_HIGH)
    yes = [o for o in path.orders if o.side is LipSide.YES]
    children = [o for o in yes if o.parent == yes[0].oid]
    assert children and children[0].decided_at >= T0 + 40 and children[0].size == 300
    assert children[0].queue >= md.books.before(children[0].live_from).yes.size_at(40)


def test_a_reprice_cancels_and_starts_a_new_episode_at_the_back():
    md = market_day(120, yes=lambda t: [(40, 1500.0), (39, 1000.0)] if t < T0 + 60 else [(42, 1500.0), (41, 1000.0)])
    path = simulate(md, J1, FillScenario.FIFO, REG_HIGH)
    yes = [o for o in path.orders if o.side is LipSide.YES]
    assert [o.price for o in yes][:2] == [40, 42]
    assert yes[0].fillable_until < float("inf") and yes[1].queue == 1500.0


def test_the_inventory_limit_is_never_exceeded():
    prints = [(T0 + 20 + 3 * i, 39, 400.0, False) for i in range(20)]
    md = market_day(200, prints=prints)
    path = simulate(md, J5, FillScenario.HEAVY, REG_HIGH)
    held_yes = sum(f.qty for f in path.fills if f.side is LipSide.YES)
    held_no = sum(f.qty for f in path.fills if f.side is LipSide.NO)
    assert held_yes - held_no <= J5.size(1000.0) + 1e-9


def test_the_dead_man_cancels_after_ten_seconds_without_a_book():
    md = market_day(200, gaps=[(T0 + 60, T0 + 90)])
    path = simulate(md, J1, FillScenario.FIFO, REG_HIGH)
    assert any(why == "dead_man" for _, why in path.stops)


def test_participation_stops_thirty_minutes_before_close():
    md = market_day(3600, close_after=2400)
    path = simulate(md, J1, FillScenario.FIFO, REG_HIGH)
    assert path.participation[1] == pytest.approx(T0 + 600)
    assert all(o.decided_at < T0 + 600 for o in path.orders)


def test_the_write_budget_delays_orders_and_is_unlimited_in_high():
    q = WriteQueue(rate=0.5)
    assert [q.send_time(10.0) for _ in range(3)] == [10.0, 12.0, 14.0]
    q = WriteQueue(rate=None)
    assert [q.send_time(10.0) for _ in range(3)] == [10.0, 10.0, 10.0]


def test_no_look_ahead_perturbing_later_books_leaves_earlier_decisions_alone():
    base = market_day(200)
    later = market_day(200, yes=lambda t: [(40, 1500.0), (39, 1000.0)] if t < T0 + 100 else [(45, 99.0)])
    a = simulate(base, I1, FillScenario.FIFO, REG_LOW)
    b = simulate(later, I1, FillScenario.FIFO, REG_LOW)
    early = lambda p: [(o.side, o.price, o.size, o.decided_at) for o in p.orders if o.decided_at < T0 + 99]
    assert early(a) == early(b)
