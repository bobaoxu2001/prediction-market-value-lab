"""Programs and admission: the two places the LIP study could choose its answer.

Each test is a way the universe could be steered - by an outcome-bearing input, by a
non-deterministic order, by a rejected market quietly coming back, or by programs
whose terms changed after they were counted.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from pmvl_markets.lip.programs import (
    ProgramRegistry,
    family_of,
    n_total_seconds,
    program_from_api,
    rate_per_hour,
)
from pmvl_markets.lip.universe import (
    AdmissionState,
    fast_info_class,
    market_eligible,
    program_eligible,
    rank_key,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 25, 12, 30, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]


def row(pid: str, market: str, *, hours: float = 24, reward_usd: float = 100, start: datetime = T0,
        paid: bool = False, target: str = "1000.00", bps: int = 5000) -> dict:
    return {"id": pid, "market_ticker": market, "incentive_type": "liquidity",
            "start_date": start.isoformat().replace("+00:00", "Z"),
            "end_date": (start + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "period_reward": int(reward_usd * 10_000), "target_size_fp": target,
            "discount_factor_bps": bps, "paid_out": paid}


def meta(ticker: str, *, status: str = "active", close: datetime | None = None, ranges=None) -> dict:
    return {"ticker": ticker, "status": status,
            "close_time": (close or T0 + timedelta(days=3)).isoformat().replace("+00:00", "Z"),
            "price_ranges": ranges if ranges is not None else [{"start": "0.00", "end": "1.00", "step": "0.01"}]}


# ------------------------------------------------------------------ programs
def test_period_reward_is_read_in_centi_cents():
    p = program_from_api(row("a", "KXFOO-1", reward_usd=100))
    assert p.reward == Decimal("100") and p.target == Decimal("1000.00") and p.discount == Decimal("0.5")


def test_n_total_uses_floor_of_start_and_ceil_of_end_on_microseconds():
    r = row("a", "KXFOO-1", hours=1)
    r["start_date"] = "2026-09-25T12:30:00.585230Z"
    r["end_date"] = "2026-09-25T13:30:00.100000Z"
    assert n_total_seconds(program_from_api(r)) == 3601


def test_rate_per_hour():
    assert rate_per_hour(program_from_api(row("a", "KXFOO-1", hours=24, reward_usd=100))) == Decimal(100) / 24


def test_state_ladders_fold_to_their_national_family_only_when_the_remainder_is_known():
    assert family_of("KXAAAGASDMI", {"KXAAAGASD"}) == "KXAAAGASD"
    assert family_of("KXAAAGASDMI", set()) == "KXAAAGASDMI"
    assert family_of("KXFOOCA", {"KXBAR"}) == "KXFOOCA"


def test_registry_tracks_additions_term_changes_payment_and_withdrawal():
    reg = ProgramRegistry()
    d = reg.observe([row("a", "KXFOO-1"), row("b", "KXFOO-2")], T0.timestamp(), listing="active")
    assert sorted(d.added) == ["a", "b"]
    changed = row("a", "KXFOO-1", reward_usd=50)
    d = reg.observe([changed], T0.timestamp() + 300, listing="active")
    assert d.terms_changed == ["a"] and d.withdrawn == ["b"]
    reg.observe([row("a", "KXFOO-1", reward_usd=50, paid=True)], T0.timestamp() + 600, listing="paid_out")
    assert not reg.verified("a"), "paid, but its terms changed after it was first seen"
    assert reg.withdrawn("b")


def test_a_program_seen_paid_with_unchanged_terms_is_verified():
    reg = ProgramRegistry()
    reg.observe([row("a", "KXFOO-1")], T0.timestamp(), listing="active")
    reg.observe([row("a", "KXFOO-1", paid=True)], T0.timestamp() + 99, listing="paid_out")
    assert reg.verified("a") and not reg.withdrawn("a")


def test_withdrawal_is_only_inferred_from_an_active_listing_and_before_the_end():
    reg = ProgramRegistry()
    reg.observe([row("a", "KXFOO-1", hours=1)], T0.timestamp(), listing="active")
    d = reg.observe([], T0.timestamp() + 7200, listing="active")
    assert d.withdrawn == [], "it simply ended"
    reg2 = ProgramRegistry()
    reg2.observe([row("a", "KXFOO-1")], T0.timestamp(), listing="upcoming")
    assert reg2.observe([], T0.timestamp() + 60, listing="upcoming").withdrawn == []


# ------------------------------------------------------------------ fast-information exclusions
def test_the_frozen_x1_x4_classification_still_holds_for_every_known_series():
    frozen = json.loads((FIXTURES / "lip_fast_info_series.json").read_text())
    known = json.loads((REPO / "artifacts/research/lip-1/known_series.json").read_text())
    now = {s: fast_info_class(s) for s in known}
    assert {s: c for s, c in now.items() if c} == frozen


def test_season_markets_escape_only_the_game_rule():
    assert fast_info_class("KXNFLGAME") == "X2"
    assert fast_info_class("KXNFLSEASONGAME") is None
    assert fast_info_class("KXSEASONMENTION") == "X2", "the exception belongs to one pattern only"
    assert fast_info_class("KXBTCD") == "X4" and fast_info_class("KXHIGHNY") == "X3"
    assert fast_info_class("KXETH15M") == "X1" and fast_info_class("KXTEMPMIAH") == "X1"


def test_program_eligibility_reasons():
    assert program_eligible(program_from_api(row("a", "KXFOO-1", hours=5))) == (False, "duration")
    assert program_eligible(program_from_api(row("a", "KXFOO-1", hours=24, reward_usd=40))) == (False, "rate")
    assert program_eligible(program_from_api(row("a", "KXBTCD-1"))) == (False, "X4")
    assert program_eligible(program_from_api(row("a", "KXFOO-1"))) == (True, "")


def test_market_eligibility_reasons():
    assert market_eligible(meta("M", status="initialized"), T0) == (False, "status")
    assert market_eligible(meta("M", ranges=[{"start": "0", "end": "0.1", "step": "0.001"},
                                             {"start": "0.1", "end": "1", "step": "0.01"}]), T0) == (False, "grid")
    assert market_eligible(meta("M", close=T0 + timedelta(minutes=30)), T0) == (False, "close_time")
    assert market_eligible(meta("M"), T0) == (True, "")


# ------------------------------------------------------------------ admission
def _programs(n: int, series: str = "KXFOO"):
    return [program_from_api(row(f"p{i:03d}", f"{series}-{i:03d}")) for i in range(n)]


def test_admission_takes_no_book_trade_price_volume_or_settlement_argument():
    params = set(inspect.signature(AdmissionState.refresh).parameters)
    assert params == {"self", "programs", "market_meta", "at", "window_end"}


def test_admission_order_is_the_fixed_salt_hash_and_is_deterministic():
    progs = _programs(12)
    metas = {p.market_ticker: meta(p.market_ticker) for p in progs}
    a, b = AdmissionState(), AdmissionState()
    ev_a = a.refresh(progs, metas, T0, window_end=T0 + timedelta(hours=12))
    ev_b = b.refresh(list(reversed(progs)), metas, T0, window_end=T0 + timedelta(hours=12))
    admitted_a = [e.program_id for e in ev_a if e.decision == "admitted"]
    assert admitted_a == [e.program_id for e in ev_b if e.decision == "admitted"]
    assert admitted_a == [p.id for p in sorted(progs, key=rank_key)][:5]


def test_family_and_total_caps_and_every_rejection_is_logged_with_its_reason():
    progs = _programs(8, "KXFOO") + [program_from_api(row(f"q{i:03d}", f"KXS{i:03d}-1")) for i in range(250)]
    metas = {p.market_ticker: meta(p.market_ticker) for p in progs}
    st = AdmissionState()
    events = st.refresh(progs, metas, T0, window_end=T0 + timedelta(hours=12))
    admitted = [e for e in events if e.decision == "admitted"]
    assert len(admitted) == 200
    assert sum(1 for e in admitted if e.family == "KXFOO") <= 5
    reasons = {e.reason for e in events if e.decision == "rejected"}
    assert {"cap_total"} <= reasons
    assert len(events) == len(progs), "every candidate gets a logged decision"


def test_a_missing_metadata_defers_rather_than_rejects_and_a_decision_is_never_revisited():
    progs = _programs(2)
    st = AdmissionState()
    events = st.refresh(progs, {}, T0, window_end=T0 + timedelta(hours=12))
    assert {e.decision for e in events} == {"deferred"} and not st.decided
    events = st.refresh(progs, {progs[0].market_ticker: meta(progs[0].market_ticker, status="paused"),
                                progs[1].market_ticker: meta(progs[1].market_ticker)},
                        T0, window_end=T0 + timedelta(hours=12))
    assert {e.decision for e in events} == {"rejected", "admitted"}
    again = st.refresh(progs, {p.market_ticker: meta(p.market_ticker) for p in progs},
                       T0 + timedelta(minutes=5), window_end=T0 + timedelta(hours=12))
    assert again == [], "a status that later turns active does not reopen a decided candidate"


def test_a_second_program_on_an_admitted_market_is_rejected_as_overlap():
    a = program_from_api(row("a", "KXFOO-1"))
    b = program_from_api(row("b", "KXFOO-1"))
    st = AdmissionState()
    events = st.refresh([a, b], {"KXFOO-1": meta("KXFOO-1")}, T0, window_end=T0 + timedelta(hours=12))
    assert sorted(e.decision for e in events) == ["admitted", "rejected"]
    assert [e.reason for e in events if e.decision == "rejected"] == ["overlap"]


def test_admission_state_round_trips_and_keeps_multi_day_programs_in_place():
    progs = _programs(3)
    st = AdmissionState()
    st.refresh(progs, {p.market_ticker: meta(p.market_ticker) for p in progs}, T0, window_end=T0 + timedelta(hours=12))
    back = AdmissionState.from_json(json.loads(json.dumps(st.to_json())))
    assert back.admitted.keys() == st.admitted.keys() and back.decided == st.decided
    pair = next(iter(back.admitted.values()))
    lo, hi = back.admitted_seconds(pair, (T0.timestamp(), (T0 + timedelta(hours=12, minutes=15)).timestamp()))
    assert lo >= pair.admitted_at and hi <= pair.end
