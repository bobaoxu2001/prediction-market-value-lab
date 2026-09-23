"""What one synchronized observation is, and what disqualifies it.

The unit of record is an *event snapshot*: every tradeable leg of one Kalshi event,
read by the venue during the handling of a single request. Two properties make it
usable as evidence and both are recorded rather than assumed:

``capture_mode``
    ``SINGLE_REQUEST`` is the only mode that may support a multi-leg claim. The other
    modes exist so that a degraded capture is *recorded as degraded* instead of being
    dropped, because silently dropping them would bias the record toward the conditions
    under which the venue happens to answer quickly.
``skew_bound_ms``
    The round trip of that request. The venue does not stamp its books, so the tightest
    honest statement is "every leg was read somewhere inside this window". It is an
    upper bound on skew, never a measurement of zero.

A snapshot also records the legs it *could not* include. An event with more legs than
the batch endpoint accepts cannot be captured synchronously at all, and the recorder
says so rather than quietly capturing a subset that a later scan would treat as whole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pmvl_shared.enums import Platform
from pmvl_shared.schemas import BookLevel, OrderBook
from pmvl_shared.timeutil import iso

__all__ = [
    "ANALYSIS_VERSION",
    "STRATEGY_VERSION",
    "CaptureMode",
    "EventSnapshot",
    "RejectionReason",
    "SnapshotRejection",
    "TradeRecord",
]

#: What the recorder wrote, and how. Bumped when the *shape or meaning of a recorded
#: line* changes, because that is what makes two files un-poolable. Stamped on every
#: line, so pooling across a bump would require deliberately ignoring the field.
#:
#: 1.0.0 - first synchronized forward recorder. Single-request capture, per-leg books
#:         and prints, market rules captured at the start of the run.
#: 1.1.0 - `skew_bound_ms` is measured around the socket instead of around the rate
#:         limiter, so it no longer counts this process's own backlog as venue
#:         latency; and the tape pull has a time budget, so it can no longer starve
#:         the book capture. Both change what a recorded line *means*: a 1.0.0 skew
#:         bound is inflated relative to a 1.1.0 one, and a 1.0.0 session's snapshot
#:         count depends on how busy the tape was. Recordings either side of this
#:         line are not pooled into one estimate.
STRATEGY_VERSION = "1.1.0"

#: What the *analysis* did with those lines. Kept separate on purpose. A recording is
#: evidence and does not change; the rules for reading it do, and re-reading an old
#: recording under a corrected rule is the point of recording it offline in the first
#: place. Every report carries both numbers so a result is always attributable to a
#: file and to the rules that were applied to it.
#:
#: 1.0.0 - queue-model maker fills; dislocation measured on synchronized snapshots.
#: 1.1.0 - implication pairs must pass the full proof, not the threshold predicate.
#:         The predicate alone paired 27 "will it rain in <city>" markets with each
#:         other - every one carries `greater` and a strike of 0, so `[X > 0]` implies
#:         `[X > 0]` held for all 351 pairs - and reported a 78c edge on rain in
#:         Dallas settling rain in Milwaukee. Results produced under 1.0.0 for the
#:         implication structure are withdrawn.
#: 1.2.0 - Polymarket fills include mints. A taker buying the complement token at p
#:         fills a resting bid on this token at 1 - p; the taker-only tape shows
#:         only the complement buy, so reading it per token missed most fills. No
#:         Polymarket maker result was published under 1.1.0 - the one run was held
#:         in memory, could not be replayed, and was discarded.
ANALYSIS_VERSION = "1.2.0"

#: The venue's batch orderbook endpoint rejects more than this many tickers per
#: request (observed: 100 accepted, 188 rejected with a ``max`` tag validation error).
#: An event wider than this has no synchronous capture and is refused, not truncated.
MAX_TICKERS_PER_REQUEST = 100


class CaptureMode(StrEnum):
    #: Every leg came back in one response. The only mode admissible as evidence
    #: for a multi-leg structure.
    SINGLE_REQUEST = "SINGLE_REQUEST"
    #: Legs were assembled from more than one request. Retained for coverage
    #: accounting and permanently barred from multi-leg claims.
    MULTI_REQUEST = "MULTI_REQUEST"
    #: Replayed from a file rather than fetched. Carries the original mode forward.
    REPLAY = "REPLAY"


class RejectionReason(StrEnum):
    TOO_MANY_LEGS = "TOO_MANY_LEGS"
    REQUEST_FAILED = "REQUEST_FAILED"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"
    UNREQUESTED_TICKER = "UNREQUESTED_TICKER"
    NO_MARKETS = "NO_MARKETS"


@dataclass(frozen=True)
class SnapshotRejection:
    """A capture that did not happen, written to the same log as the ones that did.

    Rejections are records, not errors. A record of only the successful captures
    would make the venue's slow or wide events invisible, and those are exactly the
    events where a dislocation is most likely to persist long enough to matter.
    """

    event_ticker: str
    attempted_at: datetime
    reason: RejectionReason
    detail: str = ""
    requested_legs: int = 0
    returned_legs: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "rejection",
            "strategy_version": STRATEGY_VERSION,
            "event_ticker": self.event_ticker,
            "attempted_at": iso(self.attempted_at),
            "reason": self.reason.value,
            "detail": self.detail,
            "requested_legs": self.requested_legs,
            "returned_legs": self.returned_legs,
        }


@dataclass(frozen=True)
class EventSnapshot:
    """Every leg of one event, as read inside one request window."""

    event_ticker: str
    platform: Platform
    request_sent_at: datetime
    response_received_at: datetime
    capture_mode: CaptureMode
    books: dict[str, OrderBook]
    requested_tickers: tuple[str, ...]
    #: Tickers that were requested but came back with no book at all.
    missing_tickers: tuple[str, ...] = ()
    sequence: int = 0

    @property
    def skew_bound_ms(self) -> Decimal:
        """Upper bound on how far apart two legs of this snapshot could have been read."""
        delta = self.response_received_at - self.request_sent_at
        return Decimal(str(round(delta.total_seconds() * 1000, 3)))

    @property
    def is_synchronized(self) -> bool:
        return self.capture_mode in (CaptureMode.SINGLE_REQUEST, CaptureMode.REPLAY)

    @property
    def two_sided_legs(self) -> int:
        return sum(1 for b in self.books.values() if b.yes_asks and b.no_asks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "snapshot",
            "strategy_version": STRATEGY_VERSION,
            "event_ticker": self.event_ticker,
            "platform": self.platform.value,
            "request_sent_at": iso(self.request_sent_at),
            "response_received_at": iso(self.response_received_at),
            "skew_bound_ms": str(self.skew_bound_ms),
            "capture_mode": self.capture_mode.value,
            "sequence": self.sequence,
            "requested_tickers": list(self.requested_tickers),
            "missing_tickers": list(self.missing_tickers),
            "books": {
                ticker: {
                    "yes_bids": [[str(l.price), str(l.size)] for l in book.yes_bids],
                    "yes_asks": [[str(l.price), str(l.size)] for l in book.yes_asks],
                    "no_bids": [[str(l.price), str(l.size)] for l in book.no_bids],
                    "no_asks": [[str(l.price), str(l.size)] for l in book.no_asks],
                }
                for ticker, book in sorted(self.books.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EventSnapshot":
        """Rebuild a snapshot for offline replay.

        ``capture_mode`` is carried through verbatim rather than being reset to
        ``REPLAY``: a degraded capture must stay degraded after a round trip through
        the file, or replaying the log would launder it into evidence.
        """
        sent = _parse(payload["request_sent_at"])
        received = _parse(payload["response_received_at"])
        books: dict[str, OrderBook] = {}
        for ticker, raw in (payload.get("books") or {}).items():
            books[ticker] = OrderBook(
                platform=Platform(payload["platform"]),
                platform_market_id=ticker,
                observed_at=received,
                yes_bids=_levels(raw.get("yes_bids")),
                yes_asks=_levels(raw.get("yes_asks")),
                no_bids=_levels(raw.get("no_bids")),
                no_asks=_levels(raw.get("no_asks")),
            )
        return cls(
            event_ticker=payload["event_ticker"],
            platform=Platform(payload["platform"]),
            request_sent_at=sent,
            response_received_at=received,
            capture_mode=CaptureMode(payload["capture_mode"]),
            books=books,
            requested_tickers=tuple(payload.get("requested_tickers") or ()),
            missing_tickers=tuple(payload.get("missing_tickers") or ()),
            sequence=int(payload.get("sequence") or 0),
        )


@dataclass(frozen=True)
class TradeRecord:
    """One printed trade, kept because it is the only evidence a maker order could fill.

    ``taker_side`` is the aggressor's side. A resting YES bid is filled by a taker
    who bought NO, and never by a taker who bought YES - so the field is not
    decoration, it is what decides whether a print is relevant to a given quote.
    """

    platform_market_id: str
    trade_id: str
    traded_at: datetime
    yes_price: Decimal
    size: Decimal
    taker_side: str
    is_block_trade: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "trade",
            "strategy_version": STRATEGY_VERSION,
            "platform_market_id": self.platform_market_id,
            "trade_id": self.trade_id,
            "traded_at": iso(self.traded_at),
            "yes_price": str(self.yes_price),
            "size": str(self.size),
            "taker_side": self.taker_side,
            "is_block_trade": self.is_block_trade,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TradeRecord":
        return cls(
            platform_market_id=payload["platform_market_id"],
            trade_id=payload["trade_id"],
            traded_at=_parse(payload["traded_at"]),
            yes_price=Decimal(payload["yes_price"]),
            size=Decimal(payload["size"]),
            taker_side=payload.get("taker_side") or "",
            is_block_trade=bool(payload.get("is_block_trade")),
        )


def _levels(raw: Any) -> list[BookLevel]:
    return [BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (raw or [])]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
