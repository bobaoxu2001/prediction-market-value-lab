"""Capture Polymarket books and prints in the shapes the maker experiment already eats.

Polymarket is worth a separate measurement rather than an assumption. The Kalshi maker
route died on the hedged pair - buy the fill, cross the spread to close it, pay a taker
fee - which cost 2.7c to 4.1c a contract. Polymarket charges **zero** maker and zero
taker fee on 999 of 1,000 markets and ticks at 0.001 instead of 0.01, so the dominant
term in that loss largely does not exist there. Whether what is left is positive is a
question, not a deduction.

The representation is the trick that makes the existing experiment work unchanged.
Polymarket's YES and NO are separate ERC-1155 tokens with separate order books, so each
**token** is modelled as its own single-sided market: its bids become ``yes_bids``, its
asks ``yes_asks``, and buying it is a ``Side.YES`` quote. A taker who sells that token
is the counterparty to a resting buy, which is exactly what ``taker_side == "no"`` means
in the Kalshi tape, so the mapping is ``SELL -> "no"`` and ``BUY -> "yes"``.

One happy consequence: the YES/NO coordinate transform that Kalshi needs - where a NO
bid at 0.62 is a print at 0.38 and the "through" comparison flips direction - has no
analogue here. Each token's prints are in that token's own prices. The sign-convention
bug that this project already hit once cannot occur.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from pmvl_shared.enums import Platform
from pmvl_shared.schemas import BookLevel, OrderBook

from .schema import CaptureMode, EventSnapshot, TradeRecord

__all__ = [
    "POLYMARKET_BOOKS_PATH",
    "books_to_snapshot",
    "trades_to_records",
    "with_mint_fills",
]

#: The one POST path the market-data client is allowed to use.
POLYMARKET_BOOKS_PATH = "/books"


def _levels(raw: Iterable[dict[str, Any]] | None, *, reverse: bool) -> list[BookLevel]:
    out = [
        BookLevel(price=Decimal(str(l["price"])), size=Decimal(str(l["size"])))
        for l in (raw or [])
        if l.get("price") is not None and l.get("size") is not None
    ]
    out.sort(key=lambda l: l.price, reverse=reverse)
    return out


def books_to_snapshot(
    entries: Sequence[dict[str, Any]],
    *,
    event_ticker: str,
    sent_at: datetime,
    received_at: datetime,
    sequence: int = 0,
    requested: Sequence[str] = (),
) -> EventSnapshot:
    """One batch response becomes one snapshot, each token its own market.

    The whole batch came back from a single request, so the tokens were read together
    and the skew bound is that request's round trip - the same standard applied to
    Kalshi, and the reason this can support a multi-leg claim at all.
    """
    books: dict[str, OrderBook] = {}
    for entry in entries:
        token = entry.get("asset_id")
        if not token:
            continue
        books[str(token)] = OrderBook(
            platform=Platform.POLYMARKET,
            platform_market_id=str(token),
            observed_at=received_at,
            #: bids descend, asks ascend - the convention the pricing code expects
            yes_bids=_levels(entry.get("bids"), reverse=True),
            yes_asks=_levels(entry.get("asks"), reverse=False),
            no_bids=[],
            no_asks=[],
        )
    return EventSnapshot(
        event_ticker=event_ticker,
        platform=Platform.POLYMARKET,
        request_sent_at=sent_at,
        response_received_at=received_at,
        capture_mode=CaptureMode.SINGLE_REQUEST,
        books=books,
        requested_tickers=tuple(requested or books),
        missing_tickers=tuple(t for t in (requested or ()) if t not in books),
        sequence=sequence,
    )


def trades_to_records(rows: Sequence[dict[str, Any]]) -> list[TradeRecord]:
    """Public prints to the tape shape, with the taker's side mapped.

    ``side`` on this feed is the **taker's** action because the query asks for taker
    fills only. A taker who SELLs is the counterparty to a resting buy, so it maps to
    the tape value that the fill model treats as "could have filled my bid".
    """
    out: list[TradeRecord] = []
    for row in rows:
        token = row.get("asset")
        price = row.get("price")
        size = row.get("size")
        stamp = row.get("timestamp")
        if not token or price is None or size is None or stamp is None:
            continue
        side = str(row.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        out.append(
            TradeRecord(
                platform_market_id=str(token),
                #: the hash alone is not unique - one transaction can carry several
                #: fills - so it is qualified by the token and the price.
                trade_id=f"{row.get('transactionHash','')}-{token}-{price}-{size}",
                traded_at=datetime.fromtimestamp(int(stamp), tz=timezone.utc),
                yes_price=Decimal(str(price)),
                size=Decimal(str(size)),
                taker_side="no" if side == "SELL" else "yes",
                is_block_trade=False,
            )
        )
    return out


def with_mint_fills(
    trades: Sequence[TradeRecord], complement: dict[str, str]
) -> list[TradeRecord]:
    """Add the fills that minting produced on the other token, which the tape omits.

    Polymarket's matching engine does not only cross a buyer with a seller of the same
    token. It also crosses a buyer of YES with a buyer of NO whose prices sum to at
    least a dollar, minting a fresh pair - and on the markets measured, **most**
    transactions are mints or merges rather than direct trades (116 of 159 in one,
    146 of 184 in another). A resting bid on token T is therefore filled, most often,
    by a taker *buying the complement*.

    The taker-only tape shows that as a BUY on the complement token and shows nothing
    on T, because the maker's side is not a taker print. Read naively, a bid on T
    never fills. So each taker BUY on complement(T) at price ``p`` is also expressed as
    what it is from T's side: a sale of T at ``1 - p``, which is exactly the coordinate
    transform Kalshi needs between a YES ask and a NO bid.

    Done at replay rather than at recording. The log holds what the venue published;
    this is an inference about what that implies, and it belongs to the analysis
    version, not to the evidence.

    Merges - two sellers - fill resting *offers*, not bids, and the experiment only
    rests bids, so they add nothing here.
    """
    out = list(trades)
    for trade in trades:
        if trade.taker_side != "yes":
            continue
        other = complement.get(trade.platform_market_id)
        if other is None:
            continue
        out.append(
            TradeRecord(
                platform_market_id=other,
                trade_id=f"{trade.trade_id}-mint",
                traded_at=trade.traded_at,
                yes_price=Decimal("1") - trade.yes_price,
                size=trade.size,
                taker_side="no",
                is_block_trade=False,
            )
        )
    return out
