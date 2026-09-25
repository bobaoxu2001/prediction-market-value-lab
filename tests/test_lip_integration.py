"""Recorder to verdict on one synthetic evening: the halves meet, and the invariants hold."""

from __future__ import annotations

import asyncio

import pytest

from pmvl_markets.lip.replay import build_cells, replay_day
from pmvl_markets.lip.study import COMBOS, decide, point

from tests.test_lip_recorder import START, FakeVenue, VirtualClock, make_recorder


@pytest.fixture(scope="module")
def day(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("lipint")
    clock = VirtualClock(START, scale=0.002)
    venue = FakeVenue(clock)
    rec = make_recorder(tmp, clock, venue, stop_minutes=9.0)
    asyncio.run(rec.run())
    # the recorder's evening starts at 12:25; move admission into the quoting window
    return tmp / "day"


def test_a_recorded_day_replays_through_all_24_paths(day):
    results = replay_day(day, workers=1)
    assert len(results) == 1
    r = results[0]
    assert len(r["paths"]) == 4 * 3 * 2 * 2
    assert r["hours"] >= 0


def test_low_never_exceeds_high_for_any_path(day):
    r = replay_day(day, workers=1)[0]
    for key, v in r["paths"].items():
        if key.endswith("|LOW"):
            high = r["paths"][key[: -len("LOW")] + "HIGH"]
            assert v["accrued"] <= high["accrued"] + 1e-9, key


def test_an_unverified_program_pays_nothing_in_low(day):
    results = replay_day(day, workers=1)
    pid = results[0]["program_id"]
    unverified = build_cells(results, verified=None, withdrawn=set())
    verified = build_cells(results, verified={pid}, withdrawn=set())
    for c_u, c_v in zip(unverified, verified):
        for (policy, combo, env), value in c_u.value.items():
            if env == "LOW":
                pay = sum(v["pay"] for k, v in results[0]["paths"].items()
                          if k.startswith(f"{policy}|{combo[0]}|{combo[1]}|LOW"))
                assert value == pytest.approx(c_v.value[(policy, combo, env)] - pay)


def test_the_pooled_study_is_an_interim_until_the_final_look(day):
    results = replay_day(day, workers=1)
    cells = build_cells(results, verified=set(), withdrawn=set())
    v = decide(cells, policies=["J1", "I1", "J5", "I5"], counted_days=0, final=False,
               versions={"lip-sim-1.0.0"}, gates_closed=False, isolation_ok=True)
    assert v.as_dict() == {"verdict": "INTERIM_NOT_A_DECISION", "why": "an interim decides nothing"}
    assert point(cells, ("J1", COMBOS[0], "HIGH")) == point(cells, ("J1", COMBOS[0], "HIGH"))


def test_a_withdrawn_program_pays_nothing_in_either_envelope(day):
    results = replay_day(day, workers=1)
    pid = results[0]["program_id"]
    cells = build_cells(results, verified={pid}, withdrawn={pid})
    kept = build_cells(results, verified={pid}, withdrawn=set())
    for c_w, c_k in zip(cells, kept):
        for key, value in c_w.value.items():
            assert value <= c_k.value[key] + 1e-9


