"""Polymarket's tape and books, mapped into the shapes the experiment already eats.

The mapping is where a quiet error would live: a taker's side read backwards makes
every fill land on the wrong quote, and a bid list sorted the wrong way makes the best
price the worst one. Both are silent - the numbers still come out, just wrong.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pmvl_markets.recording.polymarket_capture import (
    books_to_snapshot,
    trades_to_records,
)
from pmvl_markets.recording.schema import CaptureMode
from pmvl_shared.enums import Platform, Side

UTC = timezone.utc
SENT = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
GOT = datetime(2026, 9, 23, 12, 0, 0, 400000, tzinfo=UTC)

ENTRY = {
    "asset_id": "TOKEN_YES",
    "bids": [{"price": "0.40", "size": "100"}, {"price": "0.42", "size": "50"}],
    "asks": [{"price": "0.46", "size": "80"}, {"price": "0.44", "size": "30"}],
}


def test_bids_descend_and_asks_ascend_whatever_order_the_venue_sent():
    """Best-first is what every pricing helper assumes; the feed does not promise it."""
    snap = books_to_snapshot([ENTRY], event_ticker="EV", sent_at=SENT, received_at=GOT)
    book = snap.books["TOKEN_YES"]
    assert [l.price for l in book.yes_bids] == [Decimal("0.42"), Decimal("0.40")]
    assert [l.price for l in book.yes_asks] == [Decimal("0.44"), Decimal("0.46")]
    assert book.best_bid(Side.YES) == Decimal("0.42")
    assert book.best_ask(Side.YES) == Decimal("0.44")


def test_one_batch_response_is_one_synchronized_snapshot():
    snap = books_to_snapshot(
        [ENTRY, {"asset_id": "TOKEN_NO", "bids": [], "asks": []}],
        event_ticker="EV", sent_at=SENT, received_at=GOT,
    )
    assert snap.capture_mode is CaptureMode.SINGLE_REQUEST
    assert snap.is_synchronized
    assert snap.skew_bound_ms == Decimal("400.0")
    assert set(snap.books) == {"TOKEN_YES", "TOKEN_NO"}
    assert snap.platform is Platform.POLYMARKET


def test_a_requested_token_that_came_back_empty_is_recorded_as_missing():
    snap = books_to_snapshot(
        [ENTRY], event_ticker="EV", sent_at=SENT, received_at=GOT,
        requested=["TOKEN_YES", "TOKEN_GONE"],
    )
    assert snap.missing_tickers == ("TOKEN_GONE",)


# ------------------------------------------------------------------- the tape
def _row(side: str, price="0.41", size="25", tx="0xabc"):
    return {"asset": "TOKEN_YES", "side": side, "price": price, "size": size,
            "timestamp": 1790162575, "transactionHash": tx}


def test_a_taker_selling_is_the_counterparty_to_a_resting_buy():
    """The mapping that decides whether any fill is ever detected."""
    rec = trades_to_records([_row("SELL")])[0]
    assert rec.taker_side == "no"
    assert rec.platform_market_id == "TOKEN_YES"
    assert rec.yes_price == Decimal("0.41")
    assert rec.size == Decimal("25")


def test_a_taker_buying_cannot_fill_a_resting_buy():
    assert trades_to_records([_row("BUY")])[0].taker_side == "yes"


def test_rows_without_a_usable_side_are_dropped_rather_than_guessed():
    assert trades_to_records([_row(""), _row("MAKER"), _row("sell")]) != []
    assert len(trades_to_records([_row(""), _row("MAKER")])) == 0
    #: lowercase is the same instruction, so it is accepted
    assert trades_to_records([_row("sell")])[0].taker_side == "no"


def test_two_fills_in_one_transaction_are_two_records():
    """A hash is not a fill id. One transaction can settle several fills, and
    deduplicating on the hash alone would silently discard all but the first."""
    rows = [_row("SELL", price="0.41", size="25", tx="0xsame"),
            _row("SELL", price="0.40", size="10", tx="0xsame")]
    recs = trades_to_records(rows)
    assert len({r.trade_id for r in recs}) == 2


def test_a_malformed_row_does_not_take_the_batch_down_with_it():
    rows = [{"asset": "T", "side": "SELL"}, _row("SELL")]
    assert len(trades_to_records(rows)) == 1


def test_timestamps_are_seconds_not_milliseconds():
    """Reading this feed's seconds as milliseconds puts every print in 1970."""
    rec = trades_to_records([_row("SELL")])[0]
    assert rec.traded_at.year == 2026
