"""Synthetic LIP market-days with controllable books, prints, coverage and status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable

import numpy as np

from pmvl_markets.lip.model import (
    BookSeries,
    BookSide,
    BookSnapshot,
    MarketDay,
    ProgramPeriod,
    StatusPoint,
    StatusSeries,
    TapeCoverage,
    TradeSeries,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 25, 12, 30, tzinfo=UTC).timestamp()

Levels = list[tuple[int, float]]


def program(*, reward: float = 100.0, hours: float = 24.0, target: float = 1000.0, discount: float = 0.5,
            start: float = T0) -> ProgramPeriod:
    s = datetime.fromtimestamp(start, tz=UTC)
    return ProgramPeriod(id="prog", market_ticker="KXSYN-1", series="KXSYN", start=s,
                         end=s + timedelta(hours=hours), reward=Decimal(str(reward)), target=Decimal(str(target)),
                         discount=Decimal(str(discount)), cap=None, paid_out=False)


def market_day(
    seconds: int = 600,
    *,
    yes: Callable[[float], Levels] = lambda t: [(40, 1500.0), (39, 1000.0)],
    no: Callable[[float], Levels] = lambda t: [(55, 1500.0), (54, 1000.0)],
    prints: Iterable[tuple[float, int, float, bool]] = (),
    rtt: float = 0.2,
    cadence: float = 1.0,
    covered: bool = True,
    status: str = "active",
    reward: float = 100.0,
    target: float = 1000.0,
    fee_type: str = "quadratic",
    close_after: float = 3 * 86400,
    settlement_yes: Decimal | None = None,
    start: float = T0,
    gaps: Iterable[tuple[float, float]] = (),
) -> MarketDay:
    """A market-day of `seconds`, books every `cadence` s. prints: (t, yes_px, size, taker_yes)."""
    snaps = []
    gaps = list(gaps)
    t = start - 30.0
    cache: dict[tuple, BookSide] = {}

    def side(levels: Levels) -> BookSide:
        key = tuple(levels)
        if key not in cache:
            cache[key] = BookSide.of(list(levels))
        return cache[key]

    while t < start + seconds + 120:
        if not any(a <= t < b for a, b in gaps):
            snaps.append(BookSnapshot(t, t + rtt, side(yes(t)), side(no(t)), lane="book-A"))
        t += cadence
    rows = sorted(prints)
    trades = TradeSeries(
        "KXSYN-1",
        np.array([r[0] for r in rows], dtype=np.float64),
        np.array([r[1] for r in rows], dtype=np.int16),
        np.array([r[2] for r in rows], dtype=np.float64),
        np.array([r[3] for r in rows], dtype=bool),
        np.zeros(len(rows), dtype=bool),
        [f"t{i}" for i in range(len(rows))],
    )
    pts = [StatusPoint(start - 30 + 30 * i, status, True) for i in range(int((seconds + 200) / 30) + 2)]
    cov = TapeCoverage([(start - 60, start + seconds + 200)] if covered else [])
    return MarketDay(market="KXSYN-1", family="KXSYN", program=program(reward=reward, target=target, start=start),
                     admitted_from=start, admitted_until=start + seconds, close_time=start + close_after,
                     fee_type=fee_type, fee_multiplier=Decimal(1), books=BookSeries("KXSYN-1", snaps),
                     trades=trades, coverage=cov, status=StatusSeries(pts), settlement_yes=settlement_yes)
