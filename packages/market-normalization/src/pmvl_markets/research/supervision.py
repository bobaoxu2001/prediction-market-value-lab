"""Operational supervision: liveness, gaps, and prospective decision coverage.

Everything here is monitoring. It exists because the collector died silently
four times and each death was discovered hours later by a human looking at a
number, and because a dead collector destroys frozen decision instants that no
amount of later collection can restore.

The boundary that matters: this module may read the frozen protocol, and may
never define an experimental value of its own. The monitoring thresholds below
decide when to *page someone*; the cohort's 300-second staleness tolerance
decides what is *admissible*. Two numbers, two jobs. If monitoring invented its
own tolerance the two would drift, and the drift would surface as a table that
disagrees with the cohort about which events exist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .pit import MAX_ORDERBOOK_AGE

#: Monitoring only. Deliberately tighter than the protocol's 300 s tolerance:
#: an alert should fire while a decision instant can still be saved, not once it
#: has already been lost.
HEALTHY_CAPTURE_AGE = timedelta(seconds=120)
DEGRADED_CAPTURE_AGE = timedelta(seconds=240)

#: A discontinuity longer than this is an outage rather than a slow cycle.
#: Baseline cadence is 180 s, so this tolerates one missed cycle plus slack.
GAP_THRESHOLD = timedelta(seconds=600)

#: The classification rule, stored verbatim with every epoch so its meaning
#: cannot be quietly re-read later as whatever a future question needs.
UNATTENDED_DEFINITION = (
    "An event is UNATTENDED when its frozen decision instant fell after this "
    "epoch began, its qualifying book was collected by the production "
    "scheduler, and no human triggered collection, CLI ingestion, venue "
    "finalization or backfill for it. Deploying the supervised infrastructure "
    "is not itself an intervention: the epoch marks where human operation "
    "stops, not where it never happened."
)


class Liveness(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    NO_DATA = "NO_DATA"


class Reachability(str, Enum):
    """Whether a candidate station-day can still legitimately convert."""

    CLEAN = "CLEAN"
    #: Its decision instant has passed without a qualifying book. Nothing can
    #: fix this; a later book would be look-ahead.
    PERMANENTLY_UNREACHABLE = "PERMANENTLY_UNREACHABLE"
    #: Decision instant covered; waiting on a label that is still coming.
    CURRENTLY_PENDING = "CURRENTLY_PENDING"
    #: Decision instant still in the future.
    PROSPECTIVELY_REACHABLE = "PROSPECTIVELY_REACHABLE"


@dataclass(frozen=True)
class CollectorHealth:
    latest_capture: datetime | None
    capture_age_seconds: float | None
    state: Liveness

    def as_dict(self) -> dict[str, Any]:
        return {
            "latest_capture": (
                self.latest_capture.isoformat() if self.latest_capture else None
            ),
            "capture_age_seconds": (
                round(self.capture_age_seconds, 1)
                if self.capture_age_seconds is not None else None
            ),
            "state": self.state.value,
        }


def collector_health(
    *, data_dir: Path, now: datetime | None = None
) -> CollectorHealth:
    """Liveness from the newest archived book, never from a PID.

    A process can be alive and collecting nothing - that is precisely how the
    76-minute and 18-hour outages presented - so the authoritative signal is
    whether data is still arriving.
    """
    now = now or datetime.now(timezone.utc)
    path = data_dir / "research-archive.db"
    if not path.exists():
        return CollectorHealth(None, None, Liveness.NO_DATA)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        latest = con.execute("SELECT MAX(available_at) FROM archived_books").fetchone()[0]
    finally:
        con.close()
    if latest is None:
        return CollectorHealth(None, None, Liveness.NO_DATA)
    stamp = datetime.fromisoformat(latest)
    age = (now - stamp).total_seconds()
    if age <= HEALTHY_CAPTURE_AGE.total_seconds():
        state = Liveness.HEALTHY
    elif age <= DEGRADED_CAPTURE_AGE.total_seconds():
        state = Liveness.DEGRADED
    else:
        state = Liveness.CRITICAL
    return CollectorHealth(stamp, age, state)


@dataclass(frozen=True)
class CollectionGap:
    last_good_capture: datetime
    first_good_capture: datetime
    duration_seconds: float

    def covers(self, instant: datetime) -> bool:
        return self.last_good_capture < instant < self.first_good_capture

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_good_capture": self.last_good_capture.isoformat(),
            "first_good_capture": self.first_good_capture.isoformat(),
            "duration_seconds": round(self.duration_seconds, 1),
            "duration_human": f"{self.duration_seconds / 3600:.1f}h",
        }


def detect_gaps(*, data_dir: Path, since: datetime | None = None) -> list[CollectionGap]:
    """Discontinuities in the capture series.

    Derived from capture timestamps rather than from process uptime, because a
    suspended host keeps its uptime counter and collects nothing. A sleep
    interval is a real outage and shows up here as one.
    """
    path = data_dir / "research-archive.db"
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        query = "SELECT DISTINCT available_at FROM archived_books"
        params: tuple[Any, ...] = ()
        if since is not None:
            query += " WHERE available_at >= ?"
            params = (since.isoformat(),)
        stamps = [datetime.fromisoformat(r[0]) for r in con.execute(query + " ORDER BY 1", params)]
    finally:
        con.close()

    gaps: list[CollectionGap] = []
    for earlier, later in zip(stamps, stamps[1:]):
        delta = (later - earlier).total_seconds()
        if delta > GAP_THRESHOLD.total_seconds():
            gaps.append(CollectionGap(earlier, later, delta))
    return gaps


@dataclass(frozen=True)
class DecisionWatch:
    """One frozen decision instant and whether its evidence exists."""

    target_date: date
    station: str
    decision_at: datetime
    qualifying_book_at: datetime | None
    book_age_seconds: float | None
    status: str
    seconds_until: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_date": self.target_date.isoformat(),
            "station": self.station,
            "decision_at": self.decision_at.isoformat(),
            "qualifying_book_at": (
                self.qualifying_book_at.isoformat() if self.qualifying_book_at else None
            ),
            "book_age_seconds": (
                round(self.book_age_seconds, 1)
                if self.book_age_seconds is not None else None
            ),
            "status": self.status,
            "hours_until": round(self.seconds_until / 3600, 2),
        }


def decision_watch(
    *, data_dir: Path, now: datetime | None = None, decision_offset_hours: float = 6.0
) -> list[DecisionWatch]:
    """Every frozen decision instant, past and upcoming, with its evidence.

    Upcoming instants are the point. Discovering that a decision instant was
    uncovered on the day its market settles is discovering it far too late to
    have done anything; this reports the failure while the window is still open,
    or at worst the moment it closes.
    """
    now = now or datetime.now(timezone.utc)
    op_path, ar_path = data_dir / "pmvl.db", data_dir / "research-archive.db"
    if not op_path.exists() or not ar_path.exists():
        return []

    op = sqlite3.connect(f"file:{op_path}?mode=ro", uri=True)
    ar = sqlite3.connect(f"file:{ar_path}?mode=ro", uri=True)
    out: list[DecisionWatch] = []
    try:
        # Station attribution falls back to the archive when the market row has
        # not been mapped yet.
        #
        # `map_stations` runs daily, so a market listed after it last ran has an
        # empty settlement_station_id for up to a day - while the archiver, which
        # derives the station from the settlement rules directly, is already
        # capturing its books. Reading only the column made this view blind to
        # precisely the upcoming decision instants it exists to protect: 42
        # Aug-28 markets with 1,932 archived books reported as zero upcoming
        # instants. A monitor that cannot see the next failure is how the last
        # one went unnoticed for 18 hours.
        archive_station: dict[str, str] = {
            market_id: station
            for market_id, station in ar.execute(
                "SELECT platform_market_id, settlement_station_id "
                "FROM archived_books WHERE settlement_station_id != '' "
                "GROUP BY platform_market_id"
            )
        }

        seen: set[tuple[date, str]] = set()
        for market_id, station, close_time in op.execute(
            "SELECT platform_market_id, settlement_station_id, close_time "
            "FROM markets WHERE series_ticker LIKE 'KXHIGH%' "
            "AND close_time IS NOT NULL ORDER BY close_time"
        ):
            station = station or archive_station.get(market_id, "")
            if not station:
                continue
            parts = market_id.split("-")
            if len(parts) < 2:
                continue
            try:
                day = datetime.strptime(parts[1], "%y%b%d").date()
            except ValueError:
                continue
            if (day, station) in seen:
                continue
            seen.add((day, station))

            stamp = datetime.fromisoformat(str(close_time).replace(" ", "T"))
            close = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            decision = close - timedelta(hours=decision_offset_hours)
            until = (decision - now).total_seconds()

            if until > 0:
                out.append(
                    DecisionWatch(day, station, decision, None, None, "UPCOMING", until)
                )
                continue

            newest = ar.execute(
                "SELECT MAX(available_at) FROM archived_books "
                "WHERE settlement_station_id = ? AND available_at <= ?",
                (station, decision.isoformat()),
            ).fetchone()[0]
            if newest is None:
                out.append(
                    DecisionWatch(
                        day, station, decision, None, None,
                        "ORDERBOOK_MISSING_AT_DECISION", until,
                    )
                )
                continue
            book = datetime.fromisoformat(newest)
            age = (decision - book).total_seconds()
            # The protocol's tolerance, imported - not a local constant.
            status = (
                "DECISION_COVERED"
                if age <= MAX_ORDERBOOK_AGE.total_seconds()
                else "ORDERBOOK_STALE_AT_DECISION"
            )
            out.append(DecisionWatch(day, station, decision, book, age, status, until))
    finally:
        op.close()
        ar.close()
    return sorted(out, key=lambda w: (w.decision_at, w.station))


def classify_reachability(watch: DecisionWatch, *, clean: bool) -> Reachability:
    """Whether effort spent on this station-day can still pay off."""
    if clean:
        return Reachability.CLEAN
    if watch.status == "UPCOMING":
        return Reachability.PROSPECTIVELY_REACHABLE
    if watch.status == "DECISION_COVERED":
        return Reachability.CURRENTLY_PENDING
    # Its instant has passed uncovered. A later book cannot fix it, and using
    # one would be the look-ahead the protocol exists to prevent.
    return Reachability.PERMANENTLY_UNREACHABLE
