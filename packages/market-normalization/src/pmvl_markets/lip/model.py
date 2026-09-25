"""The data contract between the LIP recorder and the LIP simulator.

Written before either side, so the two can be built independently and still meet:
the reader turns recorded files into these types, and scoring, fills and the
simulator consume only these types. Nothing here does I/O.

Conventions, fixed here once:

- **Time** is float seconds since the Unix epoch, UTC.
- **Prices are integer cents** on each side's own grid, 1..99. A YES bid at 42 is
  42 cents for YES. A NO bid at 58 is 58 cents for NO - the same book entry as a YES
  *ask* at 42. The study only admits linear 1-cent markets (pre-registration 2.2), so
  one grid step is always one cent.
- **Sizes** are float contracts (the venue's fixed-point sizes have 0.01 granularity;
  a float carries that exactly enough for sums of a few thousand levels).
- **Books are bids only, per side**, best (highest) first. That is all LIP scoring
  reads, and it is all the fill model needs: a YES bid is filled by a print whose
  aggressor sold YES, and the opposite side's bids are this side's asks.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

import numpy as np

__all__ = [
    "BookSide",
    "BookSnapshot",
    "BookSeries",
    "LipSide",
    "MarketDay",
    "ProgramPeriod",
    "StatusPoint",
    "StatusSeries",
    "TapeCoverage",
    "TradeSeries",
]

#: The highest valid price on a linear-cent market. A best bid at this price empties
#: its side for LIP purposes (rule: "less than the highest possible price").
HIGHEST_CENT = 99


class LipSide(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def other(self) -> "LipSide":
        return LipSide.NO if self is LipSide.YES else LipSide.YES


@dataclass(frozen=True)
class BookSide:
    """One side's resting bids, best first: parallel arrays of price (cents) and size."""

    px: np.ndarray  # int16, strictly decreasing
    sz: np.ndarray  # float64, > 0

    @staticmethod
    def of(levels: list[tuple[int, float]]) -> "BookSide":
        ordered = sorted((int(p), float(q)) for p, q in levels if q > 0)
        ordered.reverse()
        return BookSide(
            px=np.array([p for p, _ in ordered], dtype=np.int16),
            sz=np.array([q for _, q in ordered], dtype=np.float64),
        )

    @property
    def empty(self) -> bool:
        return self.px.size == 0

    @property
    def best(self) -> int | None:
        return int(self.px[0]) if self.px.size else None

    def size_at(self, price: int) -> float:
        hit = np.nonzero(self.px == price)[0]
        return float(self.sz[hit[0]]) if hit.size else 0.0

    def size_better_than(self, price: int) -> float:
        return float(self.sz[self.px > price].sum())

    def total(self) -> float:
        return float(self.sz.sum())


@dataclass(frozen=True)
class BookSnapshot:
    """Other traders' book for one market, from one batched request.

    ``sent_at`` and ``received_at`` bracket the socket: the venue's state was read at
    some instant inside that window, and nothing recorded says where.
    """

    sent_at: float
    received_at: float
    yes: BookSide
    no: BookSide
    #: which recorder lane fetched it (a day has one or two book lanes)
    lane: str = ""

    def side(self, s: LipSide) -> BookSide:
        return self.yes if s is LipSide.YES else self.no

    @property
    def rtt(self) -> float:
        return self.received_at - self.sent_at


@dataclass
class BookSeries:
    """Every recorded book of one market in one study day, ordered by ``received_at``."""

    market: str
    snapshots: list[BookSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.snapshots.sort(key=lambda b: b.received_at)
        self._recv = [b.received_at for b in self.snapshots]
        #: send order can differ from receive order when two lanes overlap
        self._by_sent = sorted(self.snapshots, key=lambda b: b.sent_at)
        self._sent = [b.sent_at for b in self._by_sent]

    def before(self, t: float) -> BookSnapshot | None:
        """The last book *received* at or before t - what a decision at t could see."""
        i = bisect_right(self._recv, t) - 1
        return self.snapshots[i] if i >= 0 else None

    def after(self, t: float) -> BookSnapshot | None:
        """The first book whose request was *sent* at or after t."""
        i = bisect_left(self._sent, t)
        return self._by_sent[i] if i < len(self._by_sent) else None

    def overlapping(self, t0: float, t1: float) -> list[BookSnapshot]:
        """Books whose socket window [sent_at, received_at] intersects [t0, t1)."""
        return [b for b in self.snapshots if b.sent_at < t1 and b.received_at >= t0]

    def received_between(self, t0: float, t1: float) -> list[BookSnapshot]:
        """Books received in [t0, t1)."""
        i, j = bisect_left(self._recv, t0), bisect_left(self._recv, t1)
        return self.snapshots[i:j]


@dataclass(frozen=True)
class TradeSeries:
    """One market's prints, ordered by ``ts``. Prices are YES cents.

    ``taker_yes`` is True when the aggressor *bought* YES. Such a print can fill a
    resting NO bid (the aggressor lifted the YES ask, which is a NO bid); a print whose
    aggressor sold YES (``taker_yes`` False) can fill a resting YES bid.
    """

    market: str
    ts: np.ndarray  # float64
    yes_px: np.ndarray  # int16
    size: np.ndarray  # float64
    taker_yes: np.ndarray  # bool
    block: np.ndarray  # bool
    trade_id: list[str] = field(default_factory=list)

    @staticmethod
    def empty(market: str) -> "TradeSeries":
        return TradeSeries(market, np.zeros(0), np.zeros(0, dtype=np.int16), np.zeros(0),
                           np.zeros(0, dtype=bool), np.zeros(0, dtype=bool), [])

    def between(self, t0: float, t1: float) -> np.ndarray:
        """Indices of prints with t0 <= ts < t1."""
        return np.arange(np.searchsorted(self.ts, t0, "left"), np.searchsorted(self.ts, t1, "left"))


@dataclass(frozen=True)
class TapeCoverage:
    """Intervals over which the tape is known complete, and audit mismatches.

    Outside a complete interval, the absence of a print is not evidence that nothing
    traded. The HEAVY fill scenario reads unexplained book shrinkage there as fills.
    """

    complete: list[tuple[float, float]]
    audit_mismatch: list[tuple[float, float]] = field(default_factory=list)

    def covered(self, t0: float, t1: float) -> bool:
        if any(a < t1 and t0 < b for a, b in self.audit_mismatch):
            return False
        return any(a <= t0 and t1 <= b for a, b in self.complete)


@dataclass(frozen=True)
class StatusPoint:
    at: float
    market_status: str  # as the venue reported it: "active", "open", "paused", ...
    exchange_active: bool | None  # None when the exchange status was not read


@dataclass
class StatusSeries:
    points: list[StatusPoint] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.points.sort(key=lambda p: p.at)

    def bracketing(self, t: float) -> tuple[StatusPoint | None, StatusPoint | None]:
        ts = [p.at for p in self.points]
        i = bisect_right(ts, t)
        return (self.points[i - 1] if i > 0 else None, self.points[i] if i < len(ts) else None)


@dataclass(frozen=True)
class ProgramPeriod:
    """One liquidity program on one market, as the venue published it."""

    id: str
    market_ticker: str
    series: str
    start: datetime
    end: datetime
    #: reward for the whole period, USD (the API field is in centi-cents)
    reward: Decimal
    target: Decimal
    discount: Decimal
    cap: Decimal | None
    paid_out: bool

    @property
    def start_ts(self) -> float:
        return self.start.timestamp()

    @property
    def end_ts(self) -> float:
        return self.end.timestamp()


@dataclass
class MarketDay:
    """Everything the simulator needs for one admitted (market, program period) on one day."""

    market: str
    family: str
    program: ProgramPeriod
    #: admitted seconds in this day's quoting window: [first, last) epoch seconds
    admitted_from: float
    admitted_until: float
    close_time: float
    fee_type: str
    fee_multiplier: Decimal
    books: BookSeries
    trades: TradeSeries
    coverage: TapeCoverage
    status: StatusSeries
    #: YES payout if the market settled by the verdict date, else None
    settlement_yes: Decimal | None = None
