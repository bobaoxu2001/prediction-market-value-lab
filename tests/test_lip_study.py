"""Credit over whole days, valuation, and the one-look decision table.

Each test is a way the pooled number could overstate the strategy: an envelope that
crosses over, a starved market that pays, a pair that does not net, a verdict from 11
readings of 12, a family that carries everything, or dollars leaking into an interim.
"""

from __future__ import annotations

import math
import random
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from scipy import stats

from pmvl_markets.lip.fills import Fill, FillScenario
from pmvl_markets.lip.model import BookSide, LipSide
from pmvl_markets.lip.policy import POLICIES
from pmvl_markets.lip.simulator import REG_HIGH, REG_LOW, SimPath, score_path, simulate
from pmvl_markets.lip.study import COMBOS, Cell, decide, lofo, pigeonhole_bootstrap, point, t_interval
from pmvl_markets.lip.valuation import day_exit, fill_fees, mark_1h, walk_bids

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from lip_synthetic import T0, market_day  # noqa: E402

J1, I1, J5, I5 = POLICIES
NAMES = [p.name for p in POLICIES]


# ------------------------------------------------------------------ credit over a day
def test_low_never_exceeds_high_over_a_market_day():
    rng = random.Random(11)
    prints = [(T0 + rng.uniform(15, 280), rng.choice([39, 40, 45, 46]), float(rng.randint(10, 600)),
               rng.random() < 0.5) for _ in range(30)]
    md = market_day(300, prints=prints)
    for policy in POLICIES:
        for sc in FillScenario:
            path = simulate(md, policy, sc, REG_LOW)
            lo = score_path(path, md, low=True)["v_sum"]
            hi = score_path(path, md, low=False)["v_sum"]
            assert lo <= hi + 1e-9, (policy.name, sc)


def test_a_starved_side_pays_nothing_in_either_envelope():
    md = market_day(200, yes=lambda t: [])  # the KXTEMPMIAH shape: nobody bids YES
    for policy in POLICIES:
        path = simulate(md, policy, FillScenario.FIFO, REG_HIGH)
        assert score_path(path, md, low=False)["v_sum"] == 0.0
        assert score_path(path, md, low=True)["v_sum"] == 0.0


def test_a_quoting_policy_earns_credit_on_a_healthy_book():
    md = market_day(300)
    path = simulate(md, I1, FillScenario.FIFO, REG_HIGH)
    hi = score_path(path, md, low=False)
    assert hi["v_sum"] > 0 and hi["credited"] > 0
    assert hi["seconds"] == 300


def test_an_order_placed_mid_second_earns_nothing_for_that_second_in_low():
    md = market_day(60)
    path = simulate(md, I1, FillScenario.FIFO, REG_LOW)
    first = min(o.live_from for o in path.orders)
    k = math.floor(first)
    assert first > k, "the order landed inside second k"
    per_second_low = score_path(path, md, low=True)
    assert per_second_low["v_sum"] <= score_path(path, md, low=False)["v_sum"]


# ------------------------------------------------------------------ valuation
def test_walking_the_bids_pays_the_taker_fee_and_reports_the_unfilled_rest():
    side = BookSide.of([(40, 100.0), (39, 50.0)])
    proceeds, filled, fee = walk_bids(side, 200.0, Decimal(1))
    assert filled == 150.0 and proceeds == pytest.approx(0.40 * 100 + 0.39 * 50) and fee > 0


def _path_with(fills: list[Fill], exits=()) -> SimPath:
    from pmvl_markets.lip.fills import OrderState

    p = SimPath("KXSYN-1", J1, FillScenario.FIFO, REG_HIGH, 1000.0)
    for i, f in enumerate(fills):
        o = OrderState(oid=i, side=f.side, price=f.price, size=f.qty, remaining=0.0, queue=0.0,
                       decided_at=f.at, live_from=f.at)
        o.fills.append(f)
        p.orders.append(o)
    p.exits = list(exits)
    return p


def test_day_exit_nets_a_yes_no_pair_to_one_dollar():
    fills = [Fill(1, LipSide.YES, T0 + 10, 40, 100.0, "through"), Fill(2, LipSide.NO, T0 + 20, 55, 100.0, "through")]
    cash = day_exit(_path_with(fills), market_day(60), low=True)
    assert cash == pytest.approx(100 * (1 - 0.40 - 0.55))


def test_an_unfillable_exit_remainder_is_worth_nothing_in_low():
    fills = [Fill(1, LipSide.YES, T0 + 10, 40, 100.0, "through")]
    exits = [(T0 + 50, LipSide.YES, 100.0, float("nan"), 0.0)]
    md = market_day(60)
    assert day_exit(_path_with(fills, exits), md, low=True) == pytest.approx(-40.0)
    assert day_exit(_path_with(fills, exits), md, low=False) > -40.0


def test_the_one_hour_mark_uses_others_mid_and_charges_the_exit():
    fills = [Fill(1, LipSide.YES, T0 + 10, 40, 100.0, "through")]
    md = market_day(4000)  # others' YES mid = (40 + 45) / 2 = 42.5
    assert mark_1h(_path_with(fills), md, low=True) == pytest.approx(100 * (42.5 - 40) / 100)


def test_maker_fees_follow_the_series_and_unknown_types_are_charged_in_low_only():
    f = [Fill(1, LipSide.YES, T0, 40, 100.0, "through")]
    assert fill_fees(f, market_day(10, fee_type="quadratic"), low=True) == 0.0
    assert fill_fees(f, market_day(10, fee_type="quadratic_with_maker_fees"), low=False) > 0
    unknown = market_day(10, fee_type="")
    assert fill_fees(f, unknown, low=True) > 0 and fill_fees(f, unknown, low=False) == 0.0


# ------------------------------------------------------------------ statistics
def cells(value_of, dates=20, families=12, hours=10.0):
    out = []
    for d in range(dates):
        for f in range(families):
            c = Cell(f"2026-10-{d + 1:02d}", f"F{f}", hours)
            for p in NAMES:
                for combo in COMBOS:
                    for env in ("LOW", "HIGH"):
                        c.value[(p, combo, env)] = value_of(d, f, p, combo, env)
            out.append(c)
    return out


def verdict(cs, days=20):
    return decide(cs, policies=NAMES, counted_days=days, final=True, versions={"lip-sim-1.0.0"},
                  gates_closed=True, isolation_ok=True)


def test_t_calibration_at_twenty_clusters():
    assert stats.t.ppf(1 - 0.0125 / 2, 19) == pytest.approx(2.759, abs=1e-3)
    assert stats.t.ppf(0.975, 19) == pytest.approx(2.093, abs=1e-3)
    lo, hi = t_interval(1.0, 0.1, 20, 0.05)
    assert lo == pytest.approx(1.0 - 0.2093, abs=1e-3)


def test_the_pigeonhole_bootstrap_matches_a_two_way_variance():
    rng = random.Random(5)
    day_fx = [rng.gauss(0, 1) for _ in range(20)]
    fam_fx = [rng.gauss(0, 1) for _ in range(12)]
    cs = cells(lambda d, f, p, c, e: 10 * (day_fx[d] + fam_fx[f]))
    boot = pigeonhole_bootstrap(cs, [(NAMES[0], COMBOS[0], "LOW")], draws=4000, seed=1)[0]
    analytic = (math.sqrt(sum((x - sum(day_fx) / 20) ** 2 for x in day_fx) / 20 / 20
                          + sum((x - sum(fam_fx) / 12) ** 2 for x in fam_fx) / 12 / 12))
    assert float(boot.std()) == pytest.approx(analytic, rel=0.25)


def test_too_few_days_or_families_is_insufficient_whatever_the_numbers():
    cs = cells(lambda *a: 5.0)
    assert verdict(cs, days=19).verdict == "INSUFFICIENT_EVIDENCE"
    few = [c for c in cells(lambda *a: 5.0, families=9)]
    assert verdict(few).verdict == "INSUFFICIENT_EVIDENCE"


def test_positive_needs_all_twelve_readings_and_survives_leave_one_family_out():
    rng = random.Random(9)
    noise = {(d, f): rng.gauss(0, 0.5) for d in range(20) for f in range(12)}
    good = cells(lambda d, f, p, c, e: (10.0 + noise[(d, f)]) if p == "J1" else -5.0 + noise[(d, f)])
    assert verdict(good).verdict == "MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY"
    one_bad = cells(lambda d, f, p, c, e: (-10.0 if c == COMBOS[5] else 10.0 + noise[(d, f)]) if p == "J1"
                    else -5.0 + noise[(d, f)])
    assert verdict(one_bad).verdict != "MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY"
    carried = cells(lambda d, f, p, c, e: (500.0 if f == 0 else -1.0 + noise[(d, f)]) if p == "J1"
                    else -5.0 + noise[(d, f)])
    assert lofo(carried, ("J1", COMBOS[0], "LOW")) < 0
    assert verdict(carried).verdict != "MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY"


def test_negative_needs_every_policy_and_reading_below_zero_on_high():
    rng = random.Random(2)
    noise = {(d, f): rng.gauss(0, 0.5) for d in range(20) for f in range(12)}
    bad = cells(lambda d, f, p, c, e: -10.0 + noise[(d, f)])
    assert verdict(bad).verdict == "CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION"
    mixed = cells(lambda d, f, p, c, e: (1.0 if (p == "I5" and e == "HIGH") else -10.0) + noise[(d, f)])
    assert verdict(mixed).verdict == "INSUFFICIENT_EVIDENCE"


def test_positive_and_negative_cannot_both_hold():
    rng = random.Random(4)
    for _ in range(20):
        base = rng.uniform(-3, 3)
        cs = cells(lambda d, f, p, c, e, b=base: b + (0.5 if e == "HIGH" else 0.0) + rng.gauss(0, 1))
        v = verdict(cs).verdict
        assert v in {"MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY", "INSUFFICIENT_EVIDENCE",
                     "CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION"}


def test_an_interim_carries_no_dollar_field():
    v = decide(cells(lambda *a: 5.0), policies=NAMES, counted_days=20, final=False, versions={"x"},
               gates_closed=True, isolation_ok=True)
    body = v.as_dict()
    assert body["verdict"] == "INTERIM_NOT_A_DECISION" and set(body) == {"verdict", "why"}


def test_a_never_quoted_market_adds_hours_and_no_value():
    cs = cells(lambda *a: 5.0, dates=2, families=2)
    key = (NAMES[0], COMBOS[0], "LOW")
    before = point(cs, key)
    cs.append(Cell("2026-10-01", "F9", 10.0))
    assert point(cs, key) < before


def test_mixed_versions_are_refused():
    v = decide(cells(lambda *a: 5.0), policies=NAMES, counted_days=20, final=True,
               versions={"lip-sim-1.0.0", "lip-sim-1.0.1"}, gates_closed=True, isolation_ok=True)
    assert v.verdict == "INSUFFICIENT_EVIDENCE" and "versions" in v.why


# ------------------------------------------------------------------ canaries
def test_canary_small_pool_with_fills_drifting_against_us_never_reaches_positive():
    # every YES fill is followed by the book moving 2c against it; the pool is tiny
    def yes(t):
        return [(40, 1500.0), (39, 1000.0)] if int(t - T0) % 120 < 60 else [(38, 1500.0), (37, 1000.0)]

    prints = [(T0 + 55 + 120 * i, 39, 800.0, False) for i in range(20)]
    md = market_day(2400, yes=yes, prints=prints, reward=2.0)
    for policy in POLICIES:
        path = simulate(md, policy, FillScenario.FIFO, REG_LOW)
        credit = score_path(path, md, low=True)["accrued"]
        value = credit + mark_1h(path, md, low=True)
        assert value <= 0.0, policy.name


def test_canary_no_takers_and_a_large_pool_earns_through_low_credit_alone():
    md = market_day(1200, reward=2400.0)  # $100 an hour for a day-long period
    path = simulate(md, I1, FillScenario.FIFO, REG_LOW)
    low = score_path(path, md, low=True)
    assert path.fills == [] and low["accrued"] > 0
    assert low["accrued"] <= score_path(path, md, low=False)["accrued"] + 1e-9
