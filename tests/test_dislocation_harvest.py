"""The harvester has to be able to report that nothing happened.

Its whole purpose is to measure how often a three-cent complete-set dislocation
appears. A measurement instrument that can only report hits is not an instrument, so
these tests pin the negative cases as hard as the positive ones: losers are kept,
one-snapshot events have zero duration, thin books have no capacity, and an edge that
the fee eats is reported as eaten.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pmvl_markets.arbitrage.dislocation import (
    THRESHOLDS_CENTS,
    harvest,
    observe_complete_set,
)
from pmvl_markets.recording.schema import CaptureMode, EventSnapshot
from pmvl_shared.enums import Platform
from pmvl_shared.schemas import BookLevel, OrderBook

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def _lv(price: str, size: str) -> BookLevel:
    return BookLevel(price=Decimal(price), size=Decimal(size))


def _book(yes_asks: list[BookLevel], no_asks: list[BookLevel], ticker: str = "M") -> OrderBook:
    return OrderBook(
        platform=Platform.KALSHI,
        platform_market_id=ticker,
        observed_at=T0,
        yes_asks=yes_asks,
        no_asks=no_asks,
        yes_bids=[_lv("0.01", "10")],
        no_bids=[_lv("0.01", "10")],
    )


def _obs(book: OrderBook, **kw):
    return observe_complete_set(
        "M", "EV", book,
        observed_at=kw.pop("observed_at", T0),
        snapshot_sequence=kw.pop("seq", 0),
        skew_bound_ms=Decimal("300"),
        **kw,
    )


def _snap(seconds: int, books: dict[str, OrderBook], mode=CaptureMode.SINGLE_REQUEST):
    when = T0 + timedelta(seconds=seconds)
    return EventSnapshot(
        event_ticker="EV",
        platform=Platform.KALSHI,
        request_sent_at=when,
        response_received_at=when + timedelta(milliseconds=300),
        capture_mode=mode,
        books=books,
        requested_tickers=tuple(books),
        sequence=seconds,
    )


# -------------------------------------------------------------- measurement
def test_a_pair_above_a_dollar_is_recorded_as_a_loss_not_dropped():
    """The losers are the denominator. Without them nothing is known about how rare a win is."""
    obs = _obs(_book([_lv("0.55", "500")], [_lv("0.52", "500")]))
    assert obs is not None
    assert obs.gross_edge_per_set == Decimal("-0.07")
    assert all(obs.capacity_sets[c] == 0 for c in THRESHOLDS_CENTS)


def test_a_six_cent_edge_clears_every_threshold_up_to_five():
    obs = _obs(_book([_lv("0.50", "400")], [_lv("0.44", "400")]))
    assert obs.gross_edge_per_set == Decimal("0.06")
    for cents in THRESHOLDS_CENTS:
        assert obs.capacity_sets[cents] == Decimal("400"), cents


def test_a_two_cent_edge_clears_two_but_not_three():
    obs = _obs(_book([_lv("0.50", "400")], [_lv("0.48", "400")]))
    assert obs.capacity_sets[2] == Decimal("400")
    assert obs.capacity_sets[3] == Decimal("0")
    assert not obs.meets(3)


def test_capacity_stops_at_the_marginal_level_not_the_average():
    """Deep levels that no longer clear the bar must not be bought on the average.

    Four hundred sets at six cents and ten thousand at minus two average out to a
    positive number, and a sizer that reads the average takes all 10,400.
    """
    book = _book(
        [_lv("0.50", "400"), _lv("0.60", "10000")],
        [_lv("0.44", "400"), _lv("0.44", "10000")],
    )
    obs = _obs(book)
    assert obs.capacity_sets[3] == Decimal("400")


def test_an_unquoted_side_is_not_an_observation():
    assert _obs(_book([_lv("0.50", "400")], [])) is None


def test_a_thin_book_caps_capacity_at_the_smaller_side():
    obs = _obs(_book([_lv("0.50", "5")], [_lv("0.44", "900")]))
    assert obs.capacity_sets[3] == Decimal("5")
    assert obs.top_of_book_sets == Decimal("5")


def test_the_fee_is_charged_against_the_edge_at_the_size_taken():
    """A one-cent gross edge on a handful of sets does not survive the cent ceiling."""
    small = _obs(_book([_lv("0.50", "3")], [_lv("0.49", "3")]))
    assert small.capacity_sets[1] == Decimal("3")
    assert small.net_after_fees[1] <= 0


def test_a_wide_edge_at_size_nets_positive_before_sequencing():
    big = _obs(_book([_lv("0.50", "1000")], [_lv("0.44", "1000")]))
    assert big.net_after_fees[3] > 0


# ------------------------------------------------------------------ episodes
def test_a_single_snapshot_dislocation_has_zero_duration():
    """Padding it to the polling interval would invent persistence never observed."""
    hit = _book([_lv("0.50", "400")], [_lv("0.44", "400")])
    miss = _book([_lv("0.55", "400")], [_lv("0.52", "400")])
    report = harvest(
        [_snap(0, {"M": hit}), _snap(5, {"M": miss}), _snap(10, {"M": miss})],
        strategy_version="t",
    )
    episodes = report.episodes_at(3)
    assert len(episodes) == 1
    assert episodes[0].duration_seconds == Decimal("0.0")
    assert episodes[0].snapshots == 1


def test_a_persistent_dislocation_is_one_episode_with_a_real_duration():
    hit = _book([_lv("0.50", "400")], [_lv("0.44", "400")])
    report = harvest([_snap(i * 5, {"M": hit}) for i in range(4)], strategy_version="t")
    episodes = report.episodes_at(3)
    assert len(episodes) == 1
    assert episodes[0].snapshots == 4
    assert episodes[0].duration_seconds == Decimal("15.0")


def test_a_dislocation_that_returns_is_two_episodes_not_one():
    hit = _book([_lv("0.50", "400")], [_lv("0.44", "400")])
    miss = _book([_lv("0.55", "400")], [_lv("0.52", "400")])
    report = harvest(
        [_snap(0, {"M": hit}), _snap(5, {"M": miss}), _snap(10, {"M": hit})],
        strategy_version="t",
    )
    assert len(report.episodes_at(3)) == 2


def test_degraded_snapshots_are_skipped_and_counted():
    hit = _book([_lv("0.50", "400")], [_lv("0.44", "400")])
    report = harvest(
        [_snap(0, {"M": hit}, mode=CaptureMode.MULTI_REQUEST)], strategy_version="t"
    )
    assert report.snapshots_read == 1
    assert report.degraded_snapshots_skipped == 1
    assert report.market_observations == 0
    assert report.episodes == []


def test_a_book_with_no_depth_past_the_structure_cannot_be_sequenced():
    """All the size at the touch and nothing behind it: the second leg has no book left.

    An unpriceable second leg is reported as no sequential floor at all, and
    ``survives_sequencing`` is False - the fail-closed reading. Treating an
    unanswerable question as a pass is how a theoretical structure gets promoted.
    """
    hit = _book([_lv("0.50", "400")], [_lv("0.44", "400")])
    report = harvest([_snap(0, {"M": hit})], strategy_version="t")
    episode = report.episodes_at(3)[0]
    assert episode.sequential_net_floor is None
    assert episode.survives_sequencing is False


def test_a_deep_book_gets_a_real_sequential_verdict():
    """Depth *past* the capacity is what makes the second leg answerable at all.

    The levels above the threshold are never bought - they are what the adverse fill
    would have to reach through, and without them the sequential question has no
    answer rather than a bad one.
    """
    deep = _book(
        [_lv("0.50", "400"), _lv("0.51", "5000"), _lv("0.60", "20000")],
        [_lv("0.44", "400"), _lv("0.45", "5000"), _lv("0.55", "20000")],
    )
    report = harvest([_snap(0, {"M": deep})], strategy_version="t")
    episode = report.episodes_at(3)[0]
    assert episode.sequential_net_floor is not None
    assert episode.survives_sequencing == (episode.sequential_net_floor > 0)


def test_report_states_the_rate_against_every_observation_made():
    hit = _book([_lv("0.50", "400")], [_lv("0.44", "400")])
    miss = _book([_lv("0.55", "400")], [_lv("0.52", "400")])
    report = harvest(
        [_snap(0, {"A": hit, "B": miss, "C": miss, "D": miss})], strategy_version="t"
    )
    body = report.as_dict()
    assert body["market_observations"] == 4
    assert body["thresholds"]["3"]["observations_clearing"] == 1
    assert Decimal(body["thresholds"]["3"]["observation_rate"]) == Decimal("0.25")


def test_percentiles_are_reported_over_losers_and_winners_together():
    books = {
        f"M{i}": _book([_lv("0.55", "100")], [_lv("0.52", "100")]) for i in range(9)
    }
    books["WIN"] = _book([_lv("0.50", "100")], [_lv("0.44", "100")])
    report = harvest([_snap(0, books)], strategy_version="t")
    pct = report.edge_percentiles()
    assert Decimal(pct["p50"]) < 0
    assert Decimal(pct["max"]) == Decimal("0.06")


def test_an_empty_recording_reports_nothing_rather_than_failing():
    report = harvest([], strategy_version="t")
    body = report.as_dict()
    assert body["market_observations"] == 0
    assert body["thresholds"]["3"]["episodes"] == 0
    assert body["gross_edge_per_set_percentiles"] == {}


# ------------------------------------------------- the cross-rung structure
def _rung(ticker: str, strike: str, comparator: str = "greater", *, subject: str = "Miami"):
    """A ladder rung. ``subject`` is what the question is about, not its strike."""
    from pmvl_shared.enums import MarketStatus
    from pmvl_shared.schemas import NormalizedMarket

    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id=ticker,
        title=f"Will the temp in {subject} be above {strike} on Sep 18, 2026?",
        subtitle=f"{strike} or above",
        status=MarketStatus.OPEN,
        series_ticker="KXTEST",
        platform_event_id="KXTEST-26SEP18",
        close_time=datetime(2026, 12, 1, tzinfo=UTC),
        settlement_source="NWS",
        strike_type=comparator,
        floor_strike=Decimal(strike) if comparator.startswith("greater") else None,
        cap_strike=Decimal(strike) if comparator.startswith("less") else None,
    )


def test_a_cross_rung_pair_is_measured_where_the_engine_cannot_cross_it():
    """Two rungs are two books, so unlike the intra-market control this can dislocate.

    Buying YES on "above 100" and NO on "above 200" covers every state: if the price
    is above 200 it is above 100.
    """
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    low = _book([_lv("0.50", "400")], [_lv("0.90", "400")], ticker="LOW")
    high = _book([_lv("0.10", "400")], [_lv("0.44", "400")], ticker="HIGH")
    markets = {"LOW": _rung("LOW", "100"), "HIGH": _rung("HIGH", "200")}

    report = harvest_implication_pairs(
        [_snap(0, {"LOW": low, "HIGH": high})], markets, strategy_version="t"
    )
    assert report.market_observations == 1
    observation = report.observations[0]
    # YES(low) at 0.50 + NO(high) at 0.44 = 0.94
    assert observation.gross_edge_per_set == Decimal("0.06")
    assert len(report.episodes_at(3)) == 1


def test_a_rung_whose_rules_were_not_recorded_is_not_paired():
    """No recorded rule, no proof, no observation - never a guess from the ticker."""
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    low = _book([_lv("0.50", "400")], [_lv("0.90", "400")], ticker="LOW")
    high = _book([_lv("0.10", "400")], [_lv("0.44", "400")], ticker="HIGH")
    report = harvest_implication_pairs(
        [_snap(0, {"LOW": low, "HIGH": high})], {"LOW": _rung("LOW", "100")},
        strategy_version="t",
    )
    assert report.market_observations == 0


def test_rungs_whose_comparators_disagree_are_refused_before_the_strikes_matter():
    """``>=`` against ``>`` is refused outright, which is stricter than the maths needs.

    ``[X > 100]`` really does imply ``[X >= 100]``, so a pair like this could be
    priced. The proof declines it anyway: a ladder whose rungs do not share a
    comparator is not a ladder the venue built, and the cheap way to be wrong here is
    to reason about a mixed-comparator pair as though someone had checked it. The
    refusal is recorded rather than silent.
    """
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    a = _book([_lv("0.50", "400")], [_lv("0.90", "400")], ticker="A")
    b = _book([_lv("0.10", "400")], [_lv("0.10", "400")], ticker="B")
    markets = {
        "A": _rung("A", "100", "greater_or_equal"),
        "B": _rung("B", "100", "greater"),
    }
    report = harvest_implication_pairs(
        [_snap(0, {"A": a, "B": b})], markets, strategy_version="t"
    )
    assert report.market_observations == 0
    assert report.pairs_refused.get("refuted") == 2


def test_the_cross_rung_harvest_skips_degraded_snapshots_too():
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    low = _book([_lv("0.50", "400")], [_lv("0.90", "400")], ticker="LOW")
    high = _book([_lv("0.10", "400")], [_lv("0.44", "400")], ticker="HIGH")
    markets = {"LOW": _rung("LOW", "100"), "HIGH": _rung("HIGH", "200")}
    report = harvest_implication_pairs(
        [_snap(0, {"LOW": low, "HIGH": high}, mode=CaptureMode.MULTI_REQUEST)],
        markets, strategy_version="t",
    )
    assert report.degraded_snapshots_skipped == 1
    assert report.market_observations == 0


def test_two_questions_that_share_a_strike_are_never_paired():
    """The bug this guard exists for, reproduced.

    Twenty-seven "will it rain in <city>" markets all carry ``greater`` and a strike
    of 0. The threshold predicate says ``[X > 0]`` implies ``[X > 0]`` for every pair
    of cities, which priced rain in Dallas as settling rain in Milwaukee and reported
    a 78-cent edge on it. Nothing about the arithmetic was wrong; the strike simply
    was not what distinguished the two contracts.
    """
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    dallas = _book([_lv("0.03", "400")], [_lv("0.80", "400")], ticker="DAL")
    milwaukee = _book([_lv("0.20", "400")], [_lv("0.19", "400")], ticker="MKE")
    markets = {
        "DAL": _rung("DAL", "0", subject="Dallas"),
        "MKE": _rung("MKE", "0", subject="Milwaukee"),
    }
    report = harvest_implication_pairs(
        [_snap(0, {"DAL": dallas, "MKE": milwaukee})], markets, strategy_version="t"
    )
    assert report.market_observations == 0
    assert report.episodes == []
    assert report.pairs_refused  # and it says why, rather than silently skipping


def test_two_cities_at_different_strikes_are_still_never_paired():
    """Distinct strikes do not make two different questions comparable."""
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    a = _book([_lv("0.03", "400")], [_lv("0.80", "400")], ticker="DAL")
    b = _book([_lv("0.20", "400")], [_lv("0.19", "400")], ticker="MKE")
    markets = {
        "DAL": _rung("DAL", "70", subject="Dallas"),
        "MKE": _rung("MKE", "80", subject="Milwaukee"),
    }
    report = harvest_implication_pairs(
        [_snap(0, {"DAL": a, "MKE": b})], markets, strategy_version="t"
    )
    assert report.market_observations == 0


def test_a_real_ladder_still_proves_after_the_guard():
    """The guard must not cost the case it was built around."""
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    low = _book([_lv("0.50", "400")], [_lv("0.90", "400")], ticker="LOW")
    high = _book([_lv("0.10", "400")], [_lv("0.44", "400")], ticker="HIGH")
    markets = {"LOW": _rung("LOW", "70"), "HIGH": _rung("HIGH", "80")}
    report = harvest_implication_pairs(
        [_snap(0, {"LOW": low, "HIGH": high})], markets, strategy_version="t"
    )
    assert report.market_observations == 1
    assert report.observations[0].gross_edge_per_set == Decimal("0.06")


def test_near_misses_are_counted_so_a_zero_can_be_read():
    """"never close" and "one tick away, repeatedly" are different zeros."""
    hit = _book([_lv("0.50", "400")], [_lv("0.44", "400")])      # +6c
    close = _book([_lv("0.50", "400")], [_lv("0.48", "400")])    # +2c
    far = _book([_lv("0.60", "400")], [_lv("0.60", "400")])      # -20c
    report = harvest([_snap(0, {"A": hit, "B": close, "C": far})], strategy_version="t")
    band = report.as_dict()["thresholds"]["3"]
    assert band["observations_clearing"] == 1
    assert band["near_misses_within_one_cent"] == 1   # the +2c one


def test_an_unpriceable_structure_is_counted_rather_than_vanishing():
    """A leg with no quote on the side you must buy is not an observation of zero edge.

    It belongs outside the denominator - the question was never asked of it - but it
    has to appear somewhere, because a structure that is usually unquoted is usually
    untradeable, and that is a fact about the market rather than a hole in the file.
    """
    half_quoted = _book([_lv("0.50", "400")], [])
    report = harvest([_snap(0, {"M": half_quoted})], strategy_version="t")
    assert report.market_observations == 0
    assert report.unpriceable_observations == 1
    assert report.as_dict()["unpriceable_observations"] == 1


def test_an_episode_in_one_event_is_not_ended_by_a_snapshot_of_another():
    """Snapshots arrive one event at a time; interleaving must not fragment episodes.

    Closing every open episode on every snapshot files a two-minute dislocation as a
    dozen instantaneous ones, which changes the headline duration from "long enough
    to act on" to "never lasted a moment".
    """
    from pmvl_markets.arbitrage.dislocation import harvest_implication_pairs

    low = _book([_lv("0.50", "400")], [_lv("0.90", "400")], ticker="LOW")
    high = _book([_lv("0.10", "400")], [_lv("0.44", "400")], ticker="HIGH")
    markets = {"LOW": _rung("LOW", "70"), "HIGH": _rung("HIGH", "80")}

    def other(seconds: int):
        snap = _snap(seconds, {"X": _book([_lv("0.50", "10")], [_lv("0.50", "10")], "X")})
        return EventSnapshot(
            event_ticker="OTHER",
            platform=snap.platform,
            request_sent_at=snap.request_sent_at,
            response_received_at=snap.response_received_at,
            capture_mode=snap.capture_mode,
            books=snap.books,
            requested_tickers=snap.requested_tickers,
            sequence=snap.sequence,
        )

    stream = []
    for i in range(4):
        stream.append(_snap(i * 10, {"LOW": low, "HIGH": high}))
        stream.append(other(i * 10 + 1))

    report = harvest_implication_pairs(stream, markets, strategy_version="t")
    episodes = report.episodes_at(3)
    assert len(episodes) == 1
    assert episodes[0].snapshots == 4
    assert episodes[0].duration_seconds == Decimal("30.0")
