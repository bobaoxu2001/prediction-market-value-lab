"""Pooling sessions must not turn one afternoon into twenty observations.

Every test here is a way the pooled statistic could quietly overstate what is known:
resampling quotes instead of sessions, letting a favourable interim count as a
decision, dropping the sessions that produced nothing, reporting the per-policy
interval as though it carried the multiplicity correction, or treating twenty sessions
on one date as twenty draws of the day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_markets.arbitrage.maker_experiment import (
    FillEstimator,
    QuotePolicy,
    run_experiment,
)
from pmvl_markets.arbitrage.maker_study import (
    FAMILY_SIZE,
    SessionResult,
    pool_sessions,
)
from pmvl_markets.recording.schema import CaptureMode, EventSnapshot, TradeRecord
from pmvl_shared.enums import Platform, Side
from pmvl_shared.schemas import BookLevel, OrderBook

UTC = timezone.utc
T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


def _lv(price: str, size: str) -> BookLevel:
    return BookLevel(price=Decimal(price), size=Decimal(size))


def _book(yes_bid: str = "0.40", yes_ask: str = "0.45") -> OrderBook:
    return OrderBook(
        platform=Platform.KALSHI,
        platform_market_id="M",
        observed_at=T0,
        yes_bids=[_lv(yes_bid, "300")],
        yes_asks=[_lv(yes_ask, "300")],
        no_bids=[_lv(str(1 - Decimal(yes_ask)), "300")],
        no_asks=[_lv(str(1 - Decimal(yes_bid)), "300")],
    )


def _snap(base: datetime, seconds: int, market: str = "M") -> EventSnapshot:
    when = base + timedelta(seconds=seconds)
    return EventSnapshot(
        event_ticker="EV",
        platform=Platform.KALSHI,
        request_sent_at=when,
        response_received_at=when + timedelta(milliseconds=200),
        capture_mode=CaptureMode.SINGLE_REQUEST,
        books={market: _book()},
        requested_tickers=(market,),
        sequence=seconds,
    )


def _session(index: int, date: str, *, base: datetime, fill: bool) -> SessionResult:
    snaps = [_snap(base, i * 60) for i in range(6)]
    trades = (
        [
            TradeRecord(
                platform_market_id="M",
                trade_id=f"s{index}",
                traded_at=base + timedelta(seconds=5),
                yes_price=Decimal("0.39"),
                size=Decimal("60"),
                taker_side="no",
            )
        ]
        if fill
        else []
    )
    report = run_experiment(snaps, trades, QuotePolicy(), strategy_version="t")
    return SessionResult(
        session=index, date=date, started_at=base.isoformat(), report=report
    )


# --------------------------------------------------- the decision is at the end
def test_an_interim_is_not_a_decision():
    sessions = [_session(i, "2026-09-19", base=T0 + timedelta(hours=i), fill=True)
                for i in range(1, 4)]
    study = pool_sessions(sessions, planned_sessions=20)
    verdict, why = study.verdict()
    assert verdict == "INTERIM_NOT_A_DECISION"
    assert "3 of 20" in why
    assert not study.complete


def test_a_completed_study_whose_intervals_straddle_zero_says_insufficient():
    sessions = [
        _session(i, f"2026-09-{19 + i}", base=T0 + timedelta(days=i), fill=i % 2 == 0)
        for i in range(1, 5)
    ]
    study = pool_sessions(sessions, planned_sessions=4)
    verdict, _ = study.verdict()
    assert verdict in {
        "INSUFFICIENT_EVIDENCE",
        "CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION",
        "MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY",
    }
    assert study.complete


# ---------------------------------------------------------- the resampling unit
def test_one_session_yields_no_interval_rather_than_a_narrow_one():
    """One draw of the session factor is one observation, however many quotes it holds."""
    study = pool_sessions([_session(1, "2026-09-19", base=T0, fill=True)],
                          planned_sessions=20)
    row = study.results[0]
    assert row.session_ci95 is None
    assert row.date_ci95 is None
    assert row.quotes > 0  # plenty of quotes, and still no interval


def test_many_sessions_on_one_date_give_no_date_level_interval():
    """Twenty half-hours in one afternoon are one draw of the day."""
    sessions = [
        _session(i, "2026-09-19", base=T0 + timedelta(hours=i), fill=i % 3 == 0)
        for i in range(1, 13)
    ]
    study = pool_sessions(sessions, planned_sessions=12)
    row = study.results[0]
    assert row.session_ci95 is not None  # twelve sessions to resample
    assert row.date_ci95 is None         # but only one date
    assert row.dates == 1
    assert "date-level interval binds" in study.as_dict()["independence_note"]


def test_the_family_wise_interval_is_never_narrower_than_the_per_policy_one():
    sessions = [
        _session(i, f"2026-10-{i:02d}", base=T0 + timedelta(days=i), fill=i % 2 == 0)
        for i in range(1, 13)
    ]
    study = pool_sessions(sessions, planned_sessions=12)
    row = study.results[0]
    assert row.session_ci95 is not None and row.session_ci_family is not None
    assert row.session_ci_family[0] <= row.session_ci95[0]
    assert row.session_ci_family[1] >= row.session_ci95[1]


def test_a_session_that_placed_no_quotes_is_still_a_session():
    """Dropping the empty ones would quietly raise the cluster count."""
    live = [_session(i, f"2026-09-{19 + i}", base=T0 + timedelta(days=i), fill=True)
            for i in range(1, 4)]
    empty = SessionResult(
        session=4,
        date="2026-09-23",
        started_at=T0.isoformat(),
        report=run_experiment([], [], QuotePolicy(), strategy_version="t"),
    )
    study = pool_sessions([*live, empty], planned_sessions=4)
    row = study.results[0]
    assert row.sessions == 4
    assert row.dates == 4
    assert any(q == 0 for _, _, _, q in row.per_session_mean)


def test_per_session_means_are_reported_so_one_session_cannot_hide():
    sessions = [_session(i, f"2026-09-{19 + i}", base=T0 + timedelta(days=i), fill=True)
                for i in range(1, 4)]
    study = pool_sessions(sessions, planned_sessions=3)
    rows = study.results[0].per_session_mean
    assert [s for s, _, _, _ in rows] == [1, 2, 3]
    assert all(isinstance(m, Decimal) for _, _, m, _ in rows)


# ------------------------------------------------------------- what is reported
def test_the_report_states_the_estimator_and_the_horizon_it_used():
    study = pool_sessions(
        [_session(i, f"2026-09-{19 + i}", base=T0 + timedelta(days=i), fill=True)
         for i in range(1, 3)],
        planned_sessions=2,
    )
    body = study.as_dict()
    assert body["estimator"] == FillEstimator.QUEUE_MODEL.value
    assert body["markout_horizon_seconds"] == 60
    assert body["bootstrap"]["family_size"] == FAMILY_SIZE
    assert body["bootstrap"]["unit"].startswith("session")


def test_the_binding_interval_is_named_in_the_output():
    same_day = [_session(i, "2026-09-19", base=T0 + timedelta(hours=i), fill=True)
                for i in range(1, 4)]
    body = pool_sessions(same_day, planned_sessions=3).as_dict()
    assert body["policies"][0]["binding_interval"] == "session"

    spread = [_session(i, f"2026-10-{i:02d}", base=T0 + timedelta(days=i), fill=True)
              for i in range(1, 13)]
    body = pool_sessions(spread, planned_sessions=12).as_dict()
    assert body["policies"][0]["binding_interval"] == "date"


# ------------------------------------------------- how different were the sessions
def test_sessions_recording_the_same_books_are_flagged_as_overlapping():
    """Twenty sessions on one set of books are one recording cut into twenty pieces.

    The frozen universe plus a top-by-volume rule can hand consecutive sessions the
    same events an hour apart. The session-level interval would then be measuring
    autocorrelation, not independent variation, and nothing in the design rules it
    out - so it is measured.
    """
    sessions = [_session(i, f"2026-09-{19 + i}", base=T0 + timedelta(days=i), fill=True)
                for i in range(1, 4)]
    row = pool_sessions(sessions, planned_sessions=3).results[0]
    assert row.distinct_markets == 1
    assert row.mean_consecutive_overlap == Decimal("1.0000")


def test_sessions_on_different_books_show_no_overlap():
    def one(index: int, market: str) -> SessionResult:
        base = T0 + timedelta(days=index)
        snaps = [
            EventSnapshot(
                event_ticker="EV",
                platform=Platform.KALSHI,
                request_sent_at=base + timedelta(seconds=i * 60),
                response_received_at=base + timedelta(seconds=i * 60, milliseconds=200),
                capture_mode=CaptureMode.SINGLE_REQUEST,
                books={market: _book()},
                requested_tickers=(market,),
                sequence=i,
            )
            for i in range(4)
        ]
        return SessionResult(
            session=index, date=f"2026-09-{19 + index}", started_at=base.isoformat(),
            report=run_experiment(snaps, [], QuotePolicy(), strategy_version="t"),
        )

    row = pool_sessions([one(1, "A"), one(2, "B"), one(3, "C")], planned_sessions=3).results[0]
    assert row.distinct_markets == 3
    assert row.mean_consecutive_overlap == Decimal("0.0000")


# ------------------------------------------------ is the loss broad or tail-driven?
def test_a_tail_driven_mean_is_distinguishable_from_a_broad_one():
    """The same mean, two different findings, and the trimmed means separate them."""
    from pmvl_markets.arbitrage.maker_study import _markout_shape

    broad = [Decimal("-0.01")] * 100
    tail = [Decimal("0.00")] * 99 + [Decimal("-1.00")]

    b = _markout_shape(broad)
    t = _markout_shape(tail)
    assert b["mean"] == "-0.0100"
    assert t["mean"] == "-0.0100"
    # trimming changes nothing for the broad loss and everything for the tail
    assert b["mean_of_middle_ninety_percent"] == "-0.0100"
    assert t["mean_of_middle_ninety_percent"] == "0.0000"
    assert t["mean_excluding_worst_one_percent"] == "0.0000"


def test_the_distribution_counts_winners_losers_and_flats_separately():
    from pmvl_markets.arbitrage.maker_study import _markout_shape

    shape = _markout_shape(
        [Decimal("-0.02"), Decimal("-0.01"), Decimal("0.00"), Decimal("0.03")]
    )
    assert shape["losing_fills"] == 2
    assert shape["winning_fills"] == 1
    assert shape["flat_fills"] == 1
    assert shape["fills_with_a_markout"] == 4


def test_no_fills_means_no_distribution_rather_than_a_zero_one():
    from pmvl_markets.arbitrage.maker_study import _markout_shape

    assert _markout_shape([]) == {}


def test_a_verdict_names_the_interval_that_produced_it():
    """A verdict that cannot say which interval it used reads stronger than it is."""
    same_day = [_session(i, "2026-09-19", base=T0 + timedelta(hours=i), fill=True)
                for i in range(1, 5)]
    study = pool_sessions(same_day, planned_sessions=4)
    body = study.as_dict()
    assert body["date_factor_sampled"] is False
    assert "one calendar date" in body["generalisation_limit"]
    verdict, why = study.verdict()
    if verdict == "CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION":
        assert "session-level interval" in why
        assert "day factor is unsampled" in why


def test_a_multi_date_study_says_so_instead():
    spread = [_session(i, f"2026-10-{i:02d}", base=T0 + timedelta(days=i), fill=True)
              for i in range(1, 13)]
    study = pool_sessions(spread, planned_sessions=12)
    body = study.as_dict()
    assert body["date_factor_sampled"] is True
    assert body["policies"][0]["binding_interval"] == "date"


# -------------------------------------------------------------- replication
def _study(means: dict[str, float], *, sessions: int = 6, spread: float = 0.002):
    """A study whose per-policy means are what the caller asked for.

    Built by hand rather than by running the experiment, because these tests are
    about the comparison rule and not about the fill model.
    """
    from pmvl_markets.arbitrage.maker_study import PooledPolicyResult, StudyReport

    report = StudyReport(
        planned_sessions=sessions, completed_sessions=sessions,
        dates=["2026-09-19"], estimator="QUEUE_MODEL", horizon=60,
    )
    for name, mean in means.items():
        side = Side.NO if name.endswith("-no") else Side.YES
        report.results.append(
            PooledPolicyResult(
                policy=QuotePolicy(side=side, improve_ticks=0, name=name),
                sessions=sessions, dates=1, quotes=1000,
                fills={"QUEUE_MODEL": 100},
                mean_pnl_per_quote=Decimal(str(mean)),
                session_ci95=(Decimal(str(mean - spread)), Decimal(str(mean + spread))),
                session_ci_family=(Decimal(str(mean - spread * 1.3)),
                                   Decimal(str(mean + spread * 1.3))),
                date_ci95=None, date_ci_family=None,
                per_session_mean=[], hedged_pair_pnl=None, mean_markout=None,
            )
        )
    return report


FOUR = ["join-yes", "join-no", "improve-yes", "improve-no"]


def test_a_second_day_that_agrees_replicates():
    from pmvl_markets.arbitrage.maker_study import compare_days

    day1 = _study({n: -0.04 for n in FOUR}, spread=0.02)
    day2 = _study({n: -0.045 for n in FOUR}, spread=0.02)
    rep = compare_days(day1, day2, day_one_label="d1", day_two_label="d2")
    verdict, why = rep.verdict()
    assert verdict == "REPLICATED"
    assert "4 of 4" in why


def test_a_second_day_that_comes_out_positive_fails_to_replicate():
    """The only outcome that overturns the first study gets its own flag."""
    from pmvl_markets.arbitrage.maker_study import compare_days

    day1 = _study({n: -0.04 for n in FOUR}, spread=0.02)
    day2 = _study({n: 0.05 for n in FOUR}, spread=0.01)
    rep = compare_days(day1, day2, day_one_label="d1", day_two_label="d2")
    verdict, why = rep.verdict()
    assert verdict == "FAILED_TO_REPLICATE"
    assert len(rep.contradicting) == 4
    assert "entirely above zero" in why


def test_one_policy_going_positive_is_enough_to_fail():
    """A rule that needed all four to flip would defend the first result."""
    from pmvl_markets.arbitrage.maker_study import compare_days

    day1 = _study({n: -0.04 for n in FOUR}, spread=0.02)
    means = {n: -0.045 for n in FOUR}
    means["join-no"] = 0.06
    day2 = _study(means, spread=0.01)
    assert compare_days(day1, day2, day_one_label="d1", day_two_label="d2").verdict()[0] \
        == "FAILED_TO_REPLICATE"


def test_negative_but_far_outside_the_first_interval_is_inconclusive():
    """Same sign is not the same finding: a ten-fold larger loss did not replicate."""
    from pmvl_markets.arbitrage.maker_study import compare_days

    day1 = _study({n: -0.04 for n in FOUR}, spread=0.005)
    day2 = _study({n: -0.40 for n in FOUR}, spread=0.005)
    verdict, why = compare_days(day1, day2, day_one_label="d1", day_two_label="d2").verdict()
    assert verdict == "INCONCLUSIVE"
    assert "0 of 4 means inside" in why


def test_the_report_says_why_the_days_were_not_pooled():
    from pmvl_markets.arbitrage.maker_study import compare_days

    day1 = _study({n: -0.04 for n in FOUR})
    day2 = _study({n: -0.04 for n in FOUR})
    body = compare_days(day1, day2, day_one_label="d1", day_two_label="d2").as_dict()
    assert "different recorder versions" in body["not_pooled_because"]
    assert "REPLICATED needs all four" in body["rule"]


def test_nothing_to_compare_is_inconclusive_not_replicated():
    from pmvl_markets.arbitrage.maker_study import compare_days

    empty = _study({})
    assert compare_days(empty, empty, day_one_label="d1", day_two_label="d2").verdict()[0] \
        == "INCONCLUSIVE"


def test_the_same_session_number_on_two_days_is_two_sessions():
    """Each day numbers its sessions from 1; "session 3" twice is not one session."""
    a = _session(3, "2026-09-20", base=T0, fill=True)
    b = _session(3, "2026-09-23", base=T0 + timedelta(days=3), fill=True)
    study = pool_sessions([a, b], planned_sessions=40)
    assert study.completed_sessions == 2
    assert study.results[0].dates == 2



def test_two_dates_do_not_make_a_confidence_interval():
    """The flaw found on the first two-day pooling, pinned.

    Resampling two clusters can only produce three averages, so the percentile
    "interval" comes out as exactly the range of the two day means - measured to four
    decimals on every policy. It is not too wide to decide; it is spuriously narrow,
    because it can never exceed the two observations it was built from. Below the
    cluster minimum the interval is withheld, and the verdict falls back to a level
    that has enough clusters to support one.
    """
    from pmvl_markets.arbitrage.maker_study import MIN_CLUSTERS_FOR_INTERVAL

    two_days = [
        _session(i, "2026-09-20" if i <= 12 else "2026-09-23",
                 base=T0 + timedelta(hours=i), fill=i % 2 == 0)
        for i in range(1, 25)
    ]
    row = pool_sessions(two_days, planned_sessions=24).results[0]
    assert row.dates == 2 < MIN_CLUSTERS_FOR_INTERVAL
    assert row.date_ci95 is None
    assert row.session_ci95 is not None   # 24 sessions is enough
    assert row.binding_level == "session"


def test_below_the_cluster_minimum_no_interval_is_reported_at_all():
    from pmvl_markets.arbitrage.maker_study import MIN_CLUSTERS_FOR_INTERVAL

    few = [_session(i, f"2026-10-{i:02d}", base=T0 + timedelta(days=i), fill=True)
           for i in range(1, MIN_CLUSTERS_FOR_INTERVAL)]
    row = pool_sessions(few, planned_sessions=len(few)).results[0]
    assert row.session_ci95 is None
    assert row.date_ci95 is None
