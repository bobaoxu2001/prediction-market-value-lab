"""Every way a maker experiment can flatter itself, pinned as a test.

The failure modes are specific and each one has produced a published strategy at some
point: counting a print at your price as your fill, dividing PnL by fills instead of
attempts, marking the position at the price you were filled at, and treating a hundred
overlapping quotes on one market as a hundred observations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_markets.arbitrage.maker_experiment import (
    MARKOUT_HORIZONS,
    FillEstimator,
    MakerQuote,
    QuotePolicy,
    evaluate_quote,
    generate_quotes,
    run_experiment,
)
from pmvl_markets.recording.schema import CaptureMode, EventSnapshot, TradeRecord
from pmvl_shared.enums import Platform, Side
from pmvl_shared.schemas import BookLevel, OrderBook

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def _lv(price: str, size: str) -> BookLevel:
    return BookLevel(price=Decimal(price), size=Decimal(size))


def _book(
    ticker: str = "M",
    *,
    yes_bid: str = "0.40",
    yes_bid_size: str = "300",
    yes_ask: str = "0.45",
) -> OrderBook:
    """A book quoted from the YES side; the NO side is its mirror."""
    return OrderBook(
        platform=Platform.KALSHI,
        platform_market_id=ticker,
        observed_at=T0,
        yes_bids=[_lv(yes_bid, yes_bid_size)],
        yes_asks=[_lv(yes_ask, "300")],
        no_bids=[_lv(str(1 - Decimal(yes_ask)), "300")],
        no_asks=[_lv(str(1 - Decimal(yes_bid)), "300")],
    )


def _snap(seconds: int, books: dict[str, OrderBook], event: str = "EV") -> EventSnapshot:
    when = T0 + timedelta(seconds=seconds)
    return EventSnapshot(
        event_ticker=event,
        platform=Platform.KALSHI,
        request_sent_at=when,
        response_received_at=when + timedelta(milliseconds=200),
        capture_mode=CaptureMode.SINGLE_REQUEST,
        books=books,
        requested_tickers=tuple(books),
        sequence=seconds,
    )


def _trade(seconds: int, yes_price: str, size: str, taker_side: str, tid: str = "") -> TradeRecord:
    return TradeRecord(
        platform_market_id="M",
        trade_id=tid or f"t{seconds}-{yes_price}-{size}",
        traded_at=T0 + timedelta(seconds=seconds),
        yes_price=Decimal(yes_price),
        size=Decimal(size),
        taker_side=taker_side,
    )


def _quote(**kw) -> MakerQuote:
    defaults = dict(
        market="M",
        event_ticker="EV",
        side=Side.YES,
        price=Decimal("0.40"),
        size=Decimal("50"),
        placed_at=T0,
        expires_at=T0 + timedelta(seconds=60),
        queue_ahead=Decimal("300"),
        spread_at_placement=Decimal("0.05"),
        snapshot_sequence=0,
    )
    defaults.update(kw)
    return MakerQuote(**defaults)


POLICY = QuotePolicy()


# ------------------------------------------------- a print is not a fill
def test_a_print_at_the_quoted_price_behind_the_queue_is_not_a_fill():
    """300 resting ahead, 50 printed. Somebody was filled; it was not us."""
    quote = _quote(queue_ahead=Decimal("300"))
    trades = [_trade(10, "0.40", "50", "no")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)

    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("0")
    assert out.filled_size[FillEstimator.TRADED_THROUGH] == Decimal("0")
    #: and the inadmissible estimator is exactly the one that says otherwise
    assert out.filled_size[FillEstimator.TOUCHED] == Decimal("50")
    assert out.first_fill_at is None


def test_the_queue_must_be_cleared_before_a_single_contract_is_ours():
    quote = _quote(queue_ahead=Decimal("300"))
    trades = [_trade(10, "0.40", "310", "no")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("10")
    assert out.first_fill_at == T0 + timedelta(seconds=10)


def test_a_print_past_the_quote_fills_only_what_actually_traded():
    """Through-volume is a fill, bounded by what traded - not by what was asked for.

    A taker who sold 5 contracts through a 300-deep queue tells you 5 would have been
    yours. Reading "traded through" as "filled in full" credits the quote with 50 it
    never got, which on a thin book is most of the position.
    """
    quote = _quote(queue_ahead=Decimal("300"), size=Decimal("50"))
    trades = [_trade(10, "0.39", "5", "no")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.filled_size[FillEstimator.TRADED_THROUGH] == Decimal("5")
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("5")


def test_through_volume_beyond_the_quote_size_is_capped_at_it():
    quote = _quote(queue_ahead=Decimal("0"), size=Decimal("50"))
    trades = [_trade(10, "0.39", "900", "no")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.filled_size[FillEstimator.TRADED_THROUGH] == Decimal("50")
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("50")


def test_the_lower_bound_never_exceeds_the_queue_model():
    """Two estimators that can cross are not a bracket."""
    for at_level, through, queue in [
        ("0", "10", "100"), ("50", "0", "10"), ("5", "5", "0"), ("0", "0", "0"),
    ]:
        quote = _quote(queue_ahead=Decimal(queue), size=Decimal("50"))
        trades = []
        if Decimal(at_level) > 0:
            trades.append(_trade(5, "0.40", at_level, "no", tid="at"))
        if Decimal(through) > 0:
            trades.append(_trade(6, "0.39", through, "no", tid="thr"))
        out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
        assert (
            out.filled_size[FillEstimator.TRADED_THROUGH]
            <= out.filled_size[FillEstimator.QUEUE_MODEL]
        ), (at_level, through, queue)


def test_a_taker_on_our_own_side_cannot_fill_us():
    """A buyer of YES lifts the ask. Our YES bid is untouched by it."""
    quote = _quote(queue_ahead=Decimal("0"))
    trades = [_trade(10, "0.40", "500", "yes")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("0")
    assert out.filled_size[FillEstimator.TOUCHED] == Decimal("0")


def test_a_no_side_quote_is_matched_against_the_tape_in_yes_coordinates():
    """A NO bid at 0.62 is the same line as a YES ask at 0.38.

    Comparing 0.62 against ``yes_price_dollars`` would match nothing, and a fill rate
    of zero for the wrong reason is worse than a wrong fill rate.
    """
    quote = _quote(side=Side.NO, price=Decimal("0.62"), queue_ahead=Decimal("0"))
    assert quote.yes_price == Decimal("0.38")
    trades = [_trade(10, "0.38", "60", "yes")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("50")


def test_prints_outside_the_quote_lifetime_do_not_count():
    quote = _quote(queue_ahead=Decimal("0"))
    trades = [_trade(-5, "0.40", "500", "no"), _trade(120, "0.40", "500", "no")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("0")


def test_block_trades_are_excluded_because_they_do_not_touch_the_book():
    quote = _quote(queue_ahead=Decimal("0"))
    block = TradeRecord(
        platform_market_id="M",
        trade_id="block",
        traded_at=T0 + timedelta(seconds=5),
        yes_price=Decimal("0.40"),
        size=Decimal("5000"),
        taker_side="no",
        is_block_trade=True,
    )
    out = evaluate_quote(quote, [block], [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("0")


# ------------------------------------------------------- adverse selection
def test_a_fill_followed_by_the_market_moving_away_is_a_loss():
    """Filled at 0.40, and 30s later the market is 0.30/0.35. The spread was not income."""
    quote = _quote(queue_ahead=Decimal("0"))
    trades = [_trade(5, "0.40", "60", "no")]
    snaps = [
        _snap(0, {"M": _book()}),
        _snap(35, {"M": _book(yes_bid="0.30", yes_ask="0.35")}),
        _snap(65, {"M": _book(yes_bid="0.30", yes_ask="0.35")}),
        _snap(310, {"M": _book(yes_bid="0.30", yes_ask="0.35")}),
    ]
    out = evaluate_quote(quote, trades, snaps, policy=POLICY)
    assert out.markout[30] is not None and out.markout[30] < 0
    assert out.pnl(FillEstimator.QUEUE_MODEL, horizon=30) < 0


def test_markout_uses_the_book_after_the_fill_never_the_fill_price():
    """A flat market pays the half-spread; it does not pay zero and it does not pay the spread."""
    quote = _quote(queue_ahead=Decimal("0"))
    trades = [_trade(5, "0.40", "60", "no")]
    snaps = [_snap(0, {"M": _book()}), _snap(35, {"M": _book()})]
    out = evaluate_quote(quote, trades, snaps, policy=POLICY)
    # mid is 0.425, filled at 0.40
    assert out.markout[30] == Decimal("0.0250")


def test_markout_is_none_when_no_book_was_recorded_at_the_horizon():
    """A missing measurement is not a zero measurement."""
    quote = _quote(queue_ahead=Decimal("0"))
    trades = [_trade(5, "0.40", "60", "no")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()})], policy=POLICY)
    assert out.markout[300] is None
    assert out.pnl(FillEstimator.QUEUE_MODEL, horizon=300) == Decimal("0")


def test_liquidation_charges_the_spread_and_the_taker_fee():
    """Flattening is not free, so it must be worse than the mid markout."""
    quote = _quote(queue_ahead=Decimal("0"))
    trades = [_trade(5, "0.40", "60", "no")]
    snaps = [_snap(0, {"M": _book()}), _snap(310, {"M": _book()})]
    out = evaluate_quote(quote, trades, snaps, policy=POLICY)
    # buying NO at 0.60 flattens: proceeds 0.40, cost 0.40, minus fee -> negative
    assert out.liquidation_pnl is not None
    assert out.liquidation_pnl < 0


# ---------------------------------------------------------- the denominator
def test_expectancy_divides_by_quotes_placed_not_by_fills():
    """One winner out of a hundred attempts is not a one-winner strategy."""
    books = {"M": _book()}
    snaps = [_snap(i * 60, books) for i in range(10)]
    trades = [_trade(5, "0.39", "5", "no")]  # only the first quote can fill
    report = run_experiment(snaps, trades, QuotePolicy(size=Decimal("50")), strategy_version="t")

    assert report.quotes_placed == 10
    assert report.fills(FillEstimator.QUEUE_MODEL) == 1

    total = report.unconditional_pnl(FillEstimator.QUEUE_MODEL, horizon=30)
    per_quote = report.expectancy_per_quote(FillEstimator.QUEUE_MODEL, horizon=30)
    assert per_quote == (total / Decimal("10")).quantize(per_quote)
    # the conditional mean is ten times the unconditional expectancy here
    assert report.mean_markout_given_fill(FillEstimator.QUEUE_MODEL, horizon=30) is not None


def test_quotes_on_one_market_never_overlap():
    """Overlapping quotes share their fills and their markout; they are one observation."""
    books = {"M": _book()}
    snaps = [_snap(i * 5, books) for i in range(24)]  # every 5s for two minutes
    quotes = generate_quotes(snaps, QuotePolicy(lifetime_seconds=60))
    assert len(quotes) == 2
    assert quotes[1].placed_at >= quotes[0].expires_at


def test_a_degraded_snapshot_never_generates_a_quote():
    degraded = EventSnapshot(
        event_ticker="EV",
        platform=Platform.KALSHI,
        request_sent_at=T0,
        response_received_at=T0 + timedelta(milliseconds=200),
        capture_mode=CaptureMode.MULTI_REQUEST,
        books={"M": _book()},
        requested_tickers=("M",),
    )
    assert generate_quotes([degraded], QuotePolicy()) == []


def test_a_one_tick_spread_is_not_quoted_under_the_declared_policy():
    tight = _book(yes_bid="0.44", yes_ask="0.45")
    assert generate_quotes([_snap(0, {"M": tight})], QuotePolicy(min_spread=Decimal("0.02"))) == []


def test_improving_the_price_means_an_empty_queue():
    quotes = generate_quotes([_snap(0, {"M": _book()})], QuotePolicy(improve_ticks=1))
    assert len(quotes) == 1
    assert quotes[0].price == Decimal("0.41")
    assert quotes[0].queue_ahead == Decimal("0")


def test_joining_the_touch_inherits_the_resting_size_as_queue():
    quotes = generate_quotes([_snap(0, {"M": _book(yes_bid_size="750")})], QuotePolicy())
    assert quotes[0].price == Decimal("0.40")
    assert quotes[0].queue_ahead == Decimal("750")


def test_report_names_the_touched_estimator_as_inadmissible():
    report = run_experiment([_snap(0, {"M": _book()})], [], QuotePolicy(), strategy_version="t")
    body = report.as_dict()
    assert body["estimators"][FillEstimator.TOUCHED.value]["admissible"] is False
    assert body["estimators"][FillEstimator.QUEUE_MODEL.value]["admissible"] is True
    assert body["independent_event_days"] == 1


def test_an_empty_recording_yields_zero_expectancy_not_an_error():
    report = run_experiment([], [], QuotePolicy(), strategy_version="t")
    assert report.quotes_placed == 0
    assert report.expectancy_per_quote(FillEstimator.QUEUE_MODEL, horizon=30) == Decimal("0")
    assert report.fill_rate(FillEstimator.QUEUE_MODEL) == Decimal("0")


def test_completion_rate_separates_a_partial_fill_from_a_whole_one():
    quote = _quote(queue_ahead=Decimal("300"), size=Decimal("50"))
    partial = evaluate_quote(quote, [_trade(10, "0.40", "310", "no")],
                             [_snap(0, {"M": _book()})], policy=POLICY)
    whole = evaluate_quote(quote, [_trade(10, "0.40", "400", "no")],
                           [_snap(0, {"M": _book()})], policy=POLICY)
    assert partial.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("10")
    assert whole.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("50")
    assert partial.filled_size[FillEstimator.QUEUE_MODEL] < quote.size


# ------------------------------------------------------------- hedge and CI
def test_the_hedge_is_the_other_side_of_the_same_market_by_default():
    """A YES fill is closed by buying NO: the pair pays exactly $1 whatever settles."""
    quote = _quote(queue_ahead=Decimal("0"))
    trades = [_trade(5, "0.40", "60", "no")]
    out = evaluate_quote(quote, trades, [_snap(0, {"M": _book()}), _snap(10, {"M": _book()})],
                         policy=POLICY)
    assert out.hedge_available
    # NO ask is 1 - yes_bid = 0.60, plus the taker fee
    assert out.hedge_cost is not None and out.hedge_cost > Decimal("0.60")


def test_a_hedged_pair_costing_more_than_a_dollar_is_a_loss_however_good_the_markout():
    """Crossing the spread to close costs more than the spread earned resting."""
    books = {"M": _book()}
    snaps = [_snap(i * 60, books) for i in range(4)]
    trades = [_trade(5, "0.39", "5", "no")]
    report = run_experiment(snaps, trades, QuotePolicy(), strategy_version="t")
    pair = report.hedged_pair_pnl(FillEstimator.QUEUE_MODEL)
    assert pair is not None and pair < 0


def test_the_confidence_interval_resamples_markets_not_quotes():
    """One market's quotes share a book and a session; they are one cluster, not many."""
    books = {f"M{i}": _book() for i in range(4)}
    snaps = [_snap(i * 60, books) for i in range(6)]
    report = run_experiment(snaps, [], QuotePolicy(), strategy_version="t")
    interval = report.cluster_bootstrap_ci(FillEstimator.QUEUE_MODEL, horizon=60)
    assert interval is not None
    low, high = interval
    assert low <= high


def test_a_single_cluster_yields_no_interval_rather_than_a_fake_one():
    """One market is one observation; an interval from it would be an invention."""
    snaps = [_snap(i * 60, {"M": _book()}) for i in range(6)]
    report = run_experiment(snaps, [], QuotePolicy(), strategy_version="t")
    assert report.cluster_bootstrap_ci(FillEstimator.QUEUE_MODEL, horizon=60) is None


def test_the_index_never_hands_back_a_book_from_before_the_moment_asked_for():
    """A markout priced on a book from *before* the fill is the fill price again.

    The lookup is a binary search, so an off-by-one is silent: it returns a real
    book, at a real time, that simply predates the event being measured - and the
    adverse move disappears from the number.
    """
    from pmvl_markets.arbitrage.maker_experiment import ReplayIndex

    snaps = [_snap(i * 10, {"M": _book(yes_bid=f"0.{40 + i}")}) for i in range(5)]
    index = ReplayIndex([], snaps)

    for target_seconds in (-5, 0, 1, 9, 10, 11, 39, 40, 41):
        when = T0 + timedelta(seconds=target_seconds)
        book = index.book_at("M", when)
        if book is None:
            assert target_seconds > 40
            continue
        matching = [s for s in snaps if s.books["M"] is book]
        assert matching[0].response_received_at >= when


def test_the_index_returns_nothing_for_a_market_it_never_saw():
    from pmvl_markets.arbitrage.maker_experiment import ReplayIndex

    index = ReplayIndex([], [_snap(0, {"M": _book()})])
    assert index.book_at("OTHER", T0) is None
    assert index.trades_for("OTHER") == []


def test_the_report_states_what_it_did_not_measure():
    """A shadow experiment cannot see its own order's effect, and must say so."""
    report = run_experiment([_snap(0, {"M": _book()})], [], QuotePolicy(), strategy_version="t")
    missing = report.as_dict()["not_measured"]
    assert "own_order_impact" in missing
    assert "queue_cancellations" in missing


def test_prints_that_landed_while_the_order_was_still_in_flight_do_not_fill_it():
    """The order is not on the book until it gets there.

    Counting the prints between seeing the book and the order resting on it is the
    cheapest way to manufacture a fill rate, and on a quiet market it can be most of
    them.
    """
    quote = _quote(queue_ahead=Decimal("0"))
    in_flight = TradeRecord(
        platform_market_id="M",
        trade_id="early",
        traded_at=T0 + timedelta(milliseconds=100),
        yes_price=Decimal("0.40"),
        size=Decimal("500"),
        taker_side="no",
    )
    policy = QuotePolicy(placement_latency_ms=300)
    out = evaluate_quote(quote, [in_flight], [_snap(0, {"M": _book()})], policy=policy)
    assert out.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("0")

    instant = QuotePolicy(placement_latency_ms=0)
    optimistic = evaluate_quote(quote, [in_flight], [_snap(0, {"M": _book()})], policy=instant)
    assert optimistic.filled_size[FillEstimator.QUEUE_MODEL] == Decimal("50")
