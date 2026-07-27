"""NYSE trading calendar and trading-time accounting.

Why this module exists
----------------------
Equity index volatility accrues **only while the market is open**. The crypto model
can use calendar time because spot trades continuously; an index cannot.

The error this prevents is large, not marginal. A Kalshi market settling at 10:00 ET
on Monday morning, priced on Sunday evening, is ~13 calendar hours away but only
**0.5 trading hours** away. Variance scales with time, so calendar time overstates
sigma*sqrt(tau) by sqrt(13/0.5) ~ 5x. That inflated width pushes every probability
toward 0.5, which on a deep out-of-the-money strike priced at 3c manufactures an
enormous apparent edge on a market that is actually priced correctly.

Trading time is measured in hours of regular NYSE session (09:30-16:00 ET, weekdays,
excluding holidays and honouring the 13:00 ET early closes), and converted to years
using 252 sessions x 6.5 hours.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

TRADING_HOURS_PER_DAY = 6.5
TRADING_DAYS_PER_YEAR = 252
#: Denominator converting trading hours to the "years" a sigma quoted per annum uses.
TRADING_HOURS_PER_YEAR = TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY  # 1638.0

#: NYSE full-day closures. Hardcoded and auditable rather than pulled from a
#: dependency: the list is short, changes once a year, and a wrong holiday silently
#: shifts every probability on that day.
MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2025
        date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
        date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
        date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }
)

#: Sessions closing at 13:00 ET (day after Thanksgiving, Christmas Eve, July 3rd).
EARLY_CLOSE_DAYS: frozenset[date] = frozenset(
    {
        date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
        date(2026, 11, 27), date(2026, 12, 24),
        date(2027, 11, 26),
    }
)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in MARKET_HOLIDAYS


def session_close(day: date) -> time:
    return EARLY_CLOSE if day in EARLY_CLOSE_DAYS else REGULAR_CLOSE


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Open/close instants for ``day`` as timezone-aware ET datetimes."""
    if not is_trading_day(day):
        return None
    return (
        datetime.combine(day, REGULAR_OPEN, tzinfo=ET),
        datetime.combine(day, session_close(day), tzinfo=ET),
    )


def trading_hours_between(start: datetime, end: datetime) -> float:
    """Hours of open NYSE session between two instants.

    Both bounds may fall inside or outside a session. Returns 0.0 when ``end`` is at
    or before ``start``. Iterates day by day, which is fine for the horizons this
    project ranks (at most 30 days).
    """
    if end <= start:
        return 0.0

    start_et = start.astimezone(ET)
    end_et = end.astimezone(ET)

    total = 0.0
    day = start_et.date()
    last_day = end_et.date()
    # Guard against an unbounded loop if a caller passes a far-future date.
    for _ in range((last_day - day).days + 2):
        if day > last_day:
            break
        bounds = session_bounds(day)
        if bounds is not None:
            open_at, close_at = bounds
            overlap_start = max(start_et, open_at)
            overlap_end = min(end_et, close_at)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total


def trading_years_between(start: datetime, end: datetime) -> float:
    """Trading time between two instants, expressed in years for a per-annum sigma."""
    return trading_hours_between(start, end) / TRADING_HOURS_PER_YEAR


def next_session_open(moment: datetime) -> datetime | None:
    """The next instant the market is open at or after ``moment``."""
    current = moment.astimezone(ET)
    day = current.date()
    for _ in range(14):
        bounds = session_bounds(day)
        if bounds is not None:
            open_at, close_at = bounds
            if current <= open_at:
                return open_at
            if current < close_at:
                return current
        day += timedelta(days=1)
        current = datetime.combine(day, REGULAR_OPEN, tzinfo=ET)
    return None


def is_market_open(moment: datetime) -> bool:
    current = moment.astimezone(ET)
    bounds = session_bounds(current.date())
    if bounds is None:
        return False
    return bounds[0] <= current < bounds[1]


# ---------------------------------------------------------------- futures time
#
# CME equity index futures (ES, NQ, YM, RTY) trade Sunday 18:00 ET through Friday
# 17:00 ET, with a daily maintenance halt from 17:00 to 18:00 ET.
#
# This matters because the underlying genuinely moves overnight. The cash-session
# clock above treats a gap as zero width, which is right for "how long is the NYSE
# open" and wrong for "how much can the index still move". Counting overnight futures
# hours at a reduced weight replaces the earlier ad-hoc overnight premium with an
# actual time model.

FUTURES_OPEN = time(18, 0)   # Sunday open / daily reopen
FUTURES_HALT = time(17, 0)   # daily halt

#: Variance weight of one overnight futures hour relative to one cash-session hour.
#:
#: Calibrated so overnight accounts for roughly 20% of close-to-close daily variance,
#: which is the long-run figure for the S&P: 16.5 tradeable overnight hours x 0.10 =
#: 1.65 cash-hour equivalents against 6.5 cash hours, i.e. 20% of the 8.15-hour total.
#: Overnight futures are thinner and carry less information flow per hour, so the
#: weight is well below 1 - using 1.0 is the calendar-time error this module exists
#: to prevent.
OVERNIGHT_VARIANCE_WEIGHT = 0.10

#: One close-to-close day in cash-hour equivalents. The overnight span is 17.5 clock
#: hours but only 16.5 are tradeable - the 17:00-18:00 ET maintenance halt is dead
#: time in which the index cannot move.
OVERNIGHT_TRADEABLE_HOURS = 16.5
EFFECTIVE_HOURS_PER_DAY = (
    TRADING_HOURS_PER_DAY + OVERNIGHT_TRADEABLE_HOURS * OVERNIGHT_VARIANCE_WEIGHT
)
#: Denominator for a sigma estimated from close-to-close daily returns.
EFFECTIVE_HOURS_PER_YEAR = TRADING_DAYS_PER_YEAR * EFFECTIVE_HOURS_PER_DAY


def is_futures_open(moment: datetime) -> bool:
    """Whether CME equity index futures are trading at ``moment``."""
    current = moment.astimezone(ET)
    weekday = current.weekday()  # Mon=0 .. Sun=6
    clock = current.time()

    if weekday == 5:  # Saturday: closed all day
        return False
    if weekday == 6:  # Sunday: opens at 18:00
        return clock >= FUTURES_OPEN
    if weekday == 4:  # Friday: closes at 17:00
        return clock < FUTURES_HALT
    # Mon-Thu: open except the 17:00-18:00 maintenance halt.
    return not (FUTURES_HALT <= clock < FUTURES_OPEN)


def effective_volatility_hours(start: datetime, end: datetime) -> tuple[float, float]:
    """``(cash_hours, overnight_hours)`` between two instants.

    ``cash_hours`` is regular NYSE session time. ``overnight_hours`` is time when
    futures are open but the cash market is not. Time when neither is open (the
    weekend gap, the daily halt) contributes to neither, because the index cannot
    move then.

    Sampled at 15-minute granularity, which is finer than any decision this feeds.
    """
    if end <= start:
        return 0.0, 0.0

    cash = trading_hours_between(start, end)

    step = timedelta(minutes=15)
    overnight = 0.0
    cursor = start.astimezone(ET)
    end_et = end.astimezone(ET)
    # Bound the loop: 30 days at 15-minute steps.
    max_steps = 30 * 24 * 4 + 8
    steps = 0
    while cursor < end_et and steps < max_steps:
        nxt = min(cursor + step, end_et)
        width = (nxt - cursor).total_seconds() / 3600.0
        midpoint = cursor + (nxt - cursor) / 2
        if is_futures_open(midpoint) and not is_market_open(midpoint):
            overnight += width
        cursor = nxt
        steps += 1
    return cash, overnight


def volatility_years_between(start: datetime, end: datetime) -> float:
    """Variance-weighted time between two instants, in years.

    Consistent with a sigma estimated from close-to-close daily returns, since one
    such day equals :data:`EFFECTIVE_HOURS_PER_DAY` cash-hour equivalents.
    """
    cash, overnight = effective_volatility_hours(start, end)
    effective = cash + overnight * OVERNIGHT_VARIANCE_WEIGHT
    return effective / EFFECTIVE_HOURS_PER_YEAR
