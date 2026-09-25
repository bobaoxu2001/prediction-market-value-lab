"""The incentive rule: each test is a way the credit could be overstated."""

from __future__ import annotations

import random

import pytest

from pmvl_markets.lip.scoring import (
    OwnOrder,
    accrual,
    daily_payout,
    qualifying,
    share_high,
    share_low,
    side_ok,
)

T = 1000.0
DF = 0.5


def test_the_help_center_example_pays_sixteen_dollars():
    # a 20% share of each side, counted in 8,000 of 10,000 snapshots, of a $100 pool
    assert accrual(100.0, 0.4 * 8000, 10_000) == pytest.approx(16.0)


def test_qualification_takes_whole_levels_and_sets_the_reference_at_a_fifth_of_the_target():
    lv, ref = qualifying(((50, 150.0), (49, 100.0), (48, 900.0), (47, 50.0)), T)
    assert lv == ((50, 150.0), (49, 100.0), (48, 900.0)) and ref == 49


def test_a_side_that_never_reaches_the_target_has_no_qualifying_bids():
    assert qualifying(((50, 400.0), (49, 500.0)), T) == ((), None)
    assert not side_ok(((50, 400.0), (49, 500.0)), T)


def test_a_best_bid_at_99_empties_the_side():
    assert not side_ok(((99, 5000.0),), T)


def test_shares_on_a_side_sum_to_one():
    others = ((50, 600.0), (49, 600.0))
    ours = share_high(others, (OwnOrder(50, 400.0),), T, DF)
    assert 0 < ours < 1
    # our credit plus everyone else's is the whole side
    lv, ref = qualifying(((50, 1000.0), (49, 600.0)), T)
    assert lv == ((50, 1000.0),) and ours == pytest.approx(400 / 1000)


def test_joining_behind_the_target_at_the_best_level_earns_nothing_in_low():
    others = ((50, 1200.0), (49, 500.0))
    # LOW credits only what fits under T behind the 1,200 ahead: nothing
    assert share_low(others, (OwnOrder(50, 0.0),), T, DF) == 0.0
    assert share_high(others, (OwnOrder(50, 300.0),), T, DF) > 0, "whole-level reading credits it"


def test_improving_above_the_book_earns_full_credit_in_both():
    others = ((50, 600.0), (49, 600.0))
    lo = share_low(others, (OwnOrder(51, 200.0),), T, DF)
    hi = share_high(others, (OwnOrder(51, 200.0),), T, DF)
    assert 0 < lo <= hi


def test_our_insertion_never_lowers_others_credit_in_low():
    # Without us all three levels qualify (credit 300 + 150 + 150 = 600). With 800 of
    # ours at 51 the reference moves to 51, the 300 at 50 is discounted to 150, and the
    # 900 at 49 and 48 is pushed past the target - LOW keeps it at full credit (1,050
    # in all) instead of letting it drop, so others never count for less than alone.
    others = ((50, 300.0), (49, 300.0), (48, 600.0))
    lo = share_low(others, (OwnOrder(51, 800.0),), T, DF)
    assert lo == pytest.approx(800 / (800 + 150 + 900))
    assert lo <= 800 / (800 + 600), "O_max is at least others' credit without us"


def test_low_never_exceeds_high_on_random_books():
    rng = random.Random(7)
    for _ in range(3000):
        others = tuple(sorted({rng.randint(1, 98): float(rng.randint(1, 900)) for _ in range(rng.randint(1, 8))}.items(),
                              key=lambda x: -x[0]))
        cf = tuple(sorted(((p, q + rng.choice([0.0, 0.0, rng.randint(1, 300)])) for p, q in others), key=lambda x: -x[0]))
        px = rng.randint(1, 98)
        q = float(rng.randint(1, 1200))
        q_eff = rng.uniform(0, q)
        hi = share_high(others, (OwnOrder(px, q),), T, DF)
        lo = min(share_low(cf, (OwnOrder(min(px, rng.randint(1, px)), q_eff),), T, DF), hi)
        assert lo <= hi + 1e-12


def test_the_one_dollar_minimum_and_the_cent_floor():
    assert daily_payout(0.99, None) == 0.0
    assert daily_payout(1.005, None) == 1.00
    assert daily_payout(12.349, None) == 12.34
    assert daily_payout(50.0, cap_left=20.0) == 20.0


def test_the_per_day_floor_never_pays_more_than_the_per_period_floor():
    rng = random.Random(3)
    for _ in range(500):
        days = [rng.uniform(0, 3) for _ in range(rng.randint(1, 6))]
        assert sum(daily_payout(d, None) for d in days) <= daily_payout(sum(days), None) + 1e-9
