"""When NWS publishes, and therefore when the order book is worth watching closely.

Why windows exist at all
------------------------
A source-lag study needs the market's reaction *shortly after* a forecast
publishes. At a flat 180-second book cadence the shortest measurable markout is
about fifteen minutes and every one is built from a handful of observations - so
the interesting part of the response, the first few minutes, is unobservable.

Sampling faster everywhere would be the obvious fix and is the wrong one: it
multiplies venue load by six for twenty-four hours a day to capture information
that only exists for a few minutes after each publication. So the cadence follows
the source's schedule.

Where the schedule comes from
-----------------------------
Two sources, in order of authority:

1. **Observed publication times.** ``source_observations.published_at`` is the
   venue-independent record of when NWS actually published. Once enough of those
   exist, the schedule is a fact about this source rather than an assumption.
2. **The documented NDFD issuance cycle**, as a bootstrap for the period before
   observations exist.

The second is a stated fallback, recorded as such on every window
(:attr:`IssuanceWindow.derivation`), so a later reader can tell which windows
were scheduled from evidence and which from a default. That distinction matters:
a window derived from an assumption that turns out to be wrong produces a
research gap in exactly the minutes the study cares about, and nothing else would
reveal it.

Nothing here is a hard-coded wall-clock timestamp. The cycle is expressed as
hours-of-day in UTC, so it is a schedule rather than a list of dates that silently
expires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Iterable, Sequence

from pmvl_shared.timeutil import ensure_utc, utcnow


class WindowDerivation(str, Enum):
    """Where a window's timing came from. Evidence or assumption, never blurred."""

    #: Derived from observed ``published_at`` values for this source.
    OBSERVED_PUBLICATION = "observed_publication"
    #: The documented NDFD cycle, used before enough observations exist.
    DOCUMENTED_SCHEDULE = "documented_schedule"


#: NDFD forecast issuance hours, UTC.
#:
#: The National Digital Forecast Database is prepared on a roughly six-hourly
#: cycle keyed to the 00/06/12/18Z model runs, with the derived gridpoint product
#: published shortly after each. These are the *documented* hours and are the
#: bootstrap only - :func:`windows_for_day` prefers observed publication times as
#: soon as there are enough of them.
DOCUMENTED_ISSUANCE_HOURS_UTC: tuple[int, ...] = (2, 8, 14, 20)

#: Half-width of a research window. ±30 minutes around each publication.
DEFAULT_WINDOW_HALF_WIDTH = timedelta(minutes=30)

#: Cadence inside a window. 30 seconds gives ~120 captures per hour-long window,
#: comfortably above the 40 the acceptance criterion asks for even with a
#: collector that misses two thirds of its ticks.
RESEARCH_CADENCE_SECONDS = 30

#: Cadence outside a window. The existing baseline, unchanged: the point is to
#: sample faster when it matters, not to sample faster always.
BASELINE_CADENCE_SECONDS = 180

#: Hours to resolution beyond which a market is excluded from the 30-second
#: window loop. It is still captured at baseline cadence, every cycle.
#:
#: Forced by arithmetic, not preference. Kalshi's batch orderbook endpoint is
#: validated rather than trusted - it has been seen returning one empty book for
#: a whole chunk - so a rejected batch falls back to per-market fetches. At the
#: client's 9 req/s that is ~28 s for the 252 open research markets, and under
#: real contention with the product jobs it measured 58 s. The universe size and
#: a 30-second cadence are simply incompatible.
#:
#: Given that, capturing every market at 58 s is worse for the research question
#: than capturing the near-dated ones at 30 s: a revision to tomorrow's forecast
#: moves the contracts settling tomorrow, and the cadence is the entire reason
#: the window exists. Nothing is lost - the full board keeps being archived at
#: baseline cadence outside and during the window's own baseline ticks.
#:
#: This is a declared scoping decision. It is recorded on every window loop's
#: job details so a later reader can see which markets were eligible and why.
WINDOW_HORIZON_HOURS = 48

#: Distinct publication INSTANTS needed before observations outrank the cycle.
#:
#: Distinct instants, not rows. One gridpoint forecast is written once per market
#: it is relevant to, so a single poll of seven stations produced 84 rows and
#: only 7 publications - and the first version of this function counted the 84.
#: It concluded that NWS publishes at seven different hours a day, from one
#: snapshot of when each station happened to have last updated.
MIN_OBSERVATIONS_FOR_SCHEDULE = 12

#: Distinct UTC days the observations must span.
#:
#: A schedule is a claim about what happens *every* day. Twelve publications from
#: one afternoon are twelve facts about that afternoon; the same twelve spread
#: over three days are evidence of a cycle. Without this, the first poll of a
#: fresh deployment silently redefines the collection schedule.
MIN_DAYS_FOR_SCHEDULE = 3


@dataclass(frozen=True)
class IssuanceWindow:
    """A period of elevated collection, and the reason it exists.

    ``derivation`` and ``anchor`` are stored on every window so a later reader
    can reconstruct *why* a burst of captures happened at a particular minute -
    which is the difference between a research artefact and an unexplained spike
    in the archive.
    """

    start: datetime
    end: datetime
    #: The publication instant the window is centred on.
    anchor: datetime
    derivation: WindowDerivation
    source_id: str = "nws"
    source_product: str = "gridpoint_forecast"

    def contains(self, moment: datetime) -> bool:
        return self.start <= ensure_utc(moment) <= self.end

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def expected_captures(self, cadence_seconds: int = RESEARCH_CADENCE_SECONDS) -> int:
        """How many captures a window should yield at full uptime.

        The denominator of the coverage gate. Reported rather than assumed so a
        shortfall reads as "37 of an expected 120" instead of a bare count that
        could mean anything.
        """
        return int(self.duration.total_seconds() // cadence_seconds) + 1

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "anchor": self.anchor.isoformat(),
            "derivation": self.derivation.value,
            "source_id": self.source_id,
            "source_product": self.source_product,
        }


def documented_windows_for_day(
    day: datetime,
    *,
    hours: Sequence[int] = DOCUMENTED_ISSUANCE_HOURS_UTC,
    half_width: timedelta = DEFAULT_WINDOW_HALF_WIDTH,
) -> tuple[IssuanceWindow, ...]:
    """Windows from the documented cycle, for one UTC day."""
    date = ensure_utc(day).date()
    out = []
    for hour in sorted(hours):
        anchor = datetime.combine(date, time(hour=hour), tzinfo=timezone.utc)
        out.append(
            IssuanceWindow(
                start=anchor - half_width,
                end=anchor + half_width,
                anchor=anchor,
                derivation=WindowDerivation.DOCUMENTED_SCHEDULE,
            )
        )
    return tuple(out)


def observed_issuance_hours(
    published_at_values: Iterable[datetime],
    *,
    minimum: int = MIN_OBSERVATIONS_FOR_SCHEDULE,
    minimum_days: int = MIN_DAYS_FOR_SCHEDULE,
) -> tuple[int, ...] | None:
    """The hours this source actually publishes at, or None if too few samples.

    Returns ``None`` rather than a partial answer. A schedule inferred from one
    afternoon is not a schedule, and a caller that received one would have no way
    to tell it apart from a well-supported one.

    Two properties do the work:

    **Distinct instants, not rows.** One forecast is recorded once per market it
    is relevant to, so seven publications became eighty-four rows. Deduplicating
    first is what stops market fan-out from being read as publication frequency.

    **A multi-day span.** An hour that appears on every one of three days is a
    cycle; an hour that appears once is when that station last updated.
    """
    instants = {ensure_utc(v) for v in published_at_values if v is not None}
    if len(instants) < minimum:
        return None
    days = {i.date() for i in instants}
    if len(days) < minimum_days:
        return None

    hours: dict[int, set] = {}
    for instant in instants:
        hours.setdefault(instant.hour, set()).add(instant.date())
    # An hour must recur on at least half the observed days. A one-off late
    # publication should not permanently widen the schedule.
    needed = max(2, len(days) // 2)
    recurring = tuple(sorted(h for h, seen in hours.items() if len(seen) >= needed))
    return recurring or None


def windows_for_day(
    day: datetime,
    *,
    observed_published_at: Sequence[datetime] = (),
    half_width: timedelta = DEFAULT_WINDOW_HALF_WIDTH,
) -> tuple[IssuanceWindow, ...]:
    """The day's research windows, preferring evidence over the documented cycle."""
    hours = observed_issuance_hours(observed_published_at)
    if hours is None:
        return documented_windows_for_day(day, half_width=half_width)

    date = ensure_utc(day).date()
    return tuple(
        IssuanceWindow(
            start=datetime.combine(date, time(hour=h), tzinfo=timezone.utc) - half_width,
            end=datetime.combine(date, time(hour=h), tzinfo=timezone.utc) + half_width,
            anchor=datetime.combine(date, time(hour=h), tzinfo=timezone.utc),
            derivation=WindowDerivation.OBSERVED_PUBLICATION,
        )
        for h in hours
    )


def active_window(
    moment: datetime,
    *,
    observed_published_at: Sequence[datetime] = (),
    half_width: timedelta = DEFAULT_WINDOW_HALF_WIDTH,
) -> IssuanceWindow | None:
    """The window ``moment`` falls in, if any.

    Checks the previous and next UTC day as well as the current one. A window
    centred at 00:15 UTC straddles midnight, and a same-day-only check would drop
    half of it - losing exactly the captures on one side of a publication.
    """
    moment = ensure_utc(moment)
    for offset in (-1, 0, 1):
        day = moment + timedelta(days=offset)
        for window in windows_for_day(
            day, observed_published_at=observed_published_at, half_width=half_width
        ):
            if window.contains(moment):
                return window
    return None


def cadence_for(
    moment: datetime,
    *,
    observed_published_at: Sequence[datetime] = (),
    half_width: timedelta = DEFAULT_WINDOW_HALF_WIDTH,
) -> tuple[int, IssuanceWindow | None]:
    """``(cadence_seconds, window)`` for a moment. The scheduler's whole question."""
    window = active_window(
        moment, observed_published_at=observed_published_at, half_width=half_width
    )
    if window is None:
        return BASELINE_CADENCE_SECONDS, None
    return RESEARCH_CADENCE_SECONDS, window


def windows_between(
    start: datetime,
    end: datetime,
    *,
    observed_published_at: Sequence[datetime] = (),
    half_width: timedelta = DEFAULT_WINDOW_HALF_WIDTH,
) -> tuple[IssuanceWindow, ...]:
    """Every window overlapping a period. Used by the health report."""
    start, end = ensure_utc(start), ensure_utc(end)
    out: list[IssuanceWindow] = []
    day = datetime.combine(start.date(), time(), tzinfo=timezone.utc) - timedelta(days=1)
    limit = end + timedelta(days=1)
    while day <= limit:
        for window in windows_for_day(
            day, observed_published_at=observed_published_at, half_width=half_width
        ):
            if window.end >= start and window.start <= end:
                out.append(window)
        day += timedelta(days=1)
    return tuple(sorted(set(out), key=lambda w: w.anchor))


def within_window_horizon(
    resolution: datetime | None,
    *,
    now: datetime | None = None,
    horizon_hours: float = WINDOW_HORIZON_HOURS,
) -> bool:
    """Is this market near-dated enough for the 30-second window loop?

    Markets with no declared resolution time are excluded rather than included.
    The horizon exists to bound the per-capture request count, and admitting
    every unknown would let an unbounded set back in through the gap - which is
    the failure this is here to prevent.

    Already-resolved markets are excluded too: their book no longer moves on a
    forecast revision, so spending window budget on them buys nothing.
    """
    if resolution is None:
        return False
    hours = (ensure_utc(resolution) - ensure_utc(now or utcnow())).total_seconds() / 3600.0
    return 0.0 <= hours <= horizon_hours
