"""Is there an admissible forecast for every market we are actively collecting?

The collector will happily archive books for a target date while no forecast for
that date has ever been retrieved. Nothing errors, the books look healthy, and
the gap only surfaces much later as an unexplained exclusion - which is how KPHL
came to have 360 forecasts, none of them for the day its live contracts settle
on.

This makes the mismatch visible at collection time. It reports source
availability, not software failure: a forecast the office has not published yet
is a fact about the world, and calling it a defect sends someone hunting a bug
that does not exist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class CoverageStatus(str, Enum):
    OK = "OK"
    #: No forecast for this station at all.
    NO_FORECAST = "NO_FORECAST"
    #: Forecasts exist for the station, but none valid for this target date.
    #: The KPHL case: plenty of data, none of it about the right day.
    WRONG_TARGET_DATE = "WRONG_TARGET_DATE"
    #: A forecast exists but is older than the freshness bound.
    STALE_FORECAST = "STALE_FORECAST"


#: Beyond this, the newest forecast no longer describes the current information
#: set. NWS gridpoints refresh roughly every 6 hours; 12 tolerates one miss.
MAX_FORECAST_AGE = timedelta(hours=12)

#: Mirrors the frozen protocol's decision offset. Read here only to locate the
#: instant whose coverage is being reported; changing it would not change the
#: experiment, but it would make this table describe a different one.
DECISION_OFFSET_HOURS = 6.0


@dataclass(frozen=True)
class StationCoverage:
    station: str
    target_date: date
    markets: int
    latest_forecast_available_at: datetime | None
    forecast_age_seconds: float | None
    status: CoverageStatus

    def as_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "target_date": self.target_date.isoformat(),
            "markets": self.markets,
            "latest_forecast_available_at": (
                self.latest_forecast_available_at.isoformat()
                if self.latest_forecast_available_at else None
            ),
            "forecast_age_seconds": (
                round(self.forecast_age_seconds, 1)
                if self.forecast_age_seconds is not None else None
            ),
            "status": self.status.value,
        }


def forecast_coverage(
    *, data_dir: Path, now: datetime | None = None
) -> list[StationCoverage]:
    """Coverage for every station-day with live contracts."""
    now = now or datetime.now(timezone.utc)
    path = data_dir / "pmvl.db"
    if not path.exists():
        return []

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: list[StationCoverage] = []
    try:
        rows = con.execute(
            "SELECT settlement_station_id AS station, "
            "  substr(platform_market_id, instr(platform_market_id,'-')+1, 7) AS stamp, "
            "  COUNT(*) AS n "
            "FROM markets WHERE series_ticker LIKE 'KXHIGH%' AND status = 'open' "
            "  AND settlement_station_id IS NOT NULL AND settlement_station_id != '' "
            "GROUP BY 1, 2 ORDER BY 2, 1"
        ).fetchall()

        for row in rows:
            try:
                target = datetime.strptime(row["stamp"], "%y%b%d").date()
            except ValueError:
                continue

            latest = con.execute(
                "SELECT MAX(available_at) FROM source_observations "
                "WHERE settlement_station_id = ? AND date(forecast_valid_start) = ?",
                (row["station"], target.isoformat()),
            ).fetchone()[0]

            if latest is None:
                any_forecast = con.execute(
                    "SELECT COUNT(*) FROM source_observations "
                    "WHERE settlement_station_id = ?",
                    (row["station"],),
                ).fetchone()[0]
                status = (
                    CoverageStatus.WRONG_TARGET_DATE if any_forecast
                    else CoverageStatus.NO_FORECAST
                )
                out.append(
                    StationCoverage(row["station"], target, row["n"], None, None, status)
                )
                continue

            stamp = datetime.fromisoformat(str(latest).replace(" ", "T"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (now - stamp).total_seconds()
            status = (
                CoverageStatus.STALE_FORECAST
                if age > MAX_FORECAST_AGE.total_seconds()
                else CoverageStatus.OK
            )
            out.append(
                StationCoverage(row["station"], target, row["n"], stamp, age, status)
            )
    finally:
        con.close()
    return out


def render_coverage(rows: list[StationCoverage]) -> str:
    lines = ["", "--- FORECAST COVERAGE (open markets) ---",
             f"  {'station':<8}{'target':<12}{'mkts':>5}  {'age':>9}  status"]
    for r in sorted(rows, key=lambda x: (x.target_date, x.station)):
        age = (
            f"{r.forecast_age_seconds/3600:.1f}h"
            if r.forecast_age_seconds is not None else "-"
        )
        flag = "" if r.status is CoverageStatus.OK else "  <<<"
        lines.append(
            f"  {r.station:<8}{r.target_date.isoformat():<12}{r.markets:>5}  "
            f"{age:>9}  {r.status.value}{flag}"
        )
    bad = [r for r in rows if r.status is not CoverageStatus.OK]
    lines.append(
        f"  {len(rows) - len(bad)}/{len(rows)} station-days covered"
        + ("" if not bad else "   (gaps are source availability, not defects)")
    )
    return "\n".join(lines)


class ConversionBlocker(str, Enum):
    """Why a candidate station-day is not yet a clean settled event.

    Ordered by where the lifecycle actually stops, so the reported blocker is
    the first missing ingredient rather than whichever check happened to run
    last. A deficit is only actionable if it says which stage is holding.
    """

    NONE = ""
    ORDERBOOK_MISSING = "ORDERBOOK_MISSING"
    #: Books exist for the station-day, but none within tolerance AT the frozen
    #: decision instant. Distinct from ORDERBOOK_MISSING because it is not fixed
    #: by collecting more later - the instant has passed, and filling it from a
    #: newer book is exactly the look-ahead the protocol forbids.
    ORDERBOOK_STALE_AT_DECISION = "ORDERBOOK_STALE_AT_DECISION"
    FORECAST_MISSING = "FORECAST_MISSING"
    CLI_FINAL_PENDING = "CLI_FINAL_PENDING"
    VENUE_TERMINAL_PENDING = "VENUE_TERMINAL_PENDING"


@dataclass(frozen=True)
class StationDay:
    """One candidate station-day and the state of each required ingredient."""

    target_date: date
    station: str
    has_forecast: bool
    has_orderbook: bool
    has_cli_final: bool
    has_venue_terminal: bool
    #: A book within tolerance at the frozen decision instant. None when the
    #: decision instant has not yet passed, so a future station-day is never
    #: reported as already failed.
    book_at_decision: bool | None = None

    @property
    def clean(self) -> bool:
        return (
            self.has_forecast
            and self.has_orderbook
            and self.book_at_decision is not False
            and self.has_cli_final
            and self.has_venue_terminal
        )

    @property
    def blocker(self) -> ConversionBlocker:
        if self.clean:
            return ConversionBlocker.NONE
        if not self.has_orderbook:
            return ConversionBlocker.ORDERBOOK_MISSING
        if self.book_at_decision is False:
            # Reported ahead of the label blockers: waiting for a CLI final on a
            # station-day whose decision instant can never be covered is waiting
            # for something that will not help.
            return ConversionBlocker.ORDERBOOK_STALE_AT_DECISION
        if not self.has_forecast:
            return ConversionBlocker.FORECAST_MISSING
        if not self.has_cli_final:
            return ConversionBlocker.CLI_FINAL_PENDING
        return ConversionBlocker.VENUE_TERMINAL_PENDING


def _ticker_day(market_id: str) -> date | None:
    parts = market_id.split("-")
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(parts[1], "%y%b%d").date()
    except ValueError:
        return None


def station_day_conversions(*, data_dir: Path) -> list[StationDay]:
    """Every candidate station-day, with each ingredient resolved independently.

    A candidate is any station-day for which at least one archived orderbook
    exists. Station-days with no book at all are not listed: they were never
    candidates, and padding the table with them would hide the real blockers
    behind rows nothing can be done about.
    """
    op_path, ar_path = data_dir / "pmvl.db", data_dir / "research-archive.db"
    if not op_path.exists() or not ar_path.exists():
        return []

    op = sqlite3.connect(f"file:{op_path}?mode=ro", uri=True)
    ar = sqlite3.connect(f"file:{ar_path}?mode=ro", uri=True)
    try:
        candidates: set[tuple[date, str]] = set()
        for market_id, station in ar.execute(
            "SELECT DISTINCT platform_market_id, settlement_station_id "
            "FROM archived_books WHERE settlement_station_id != ''"
        ):
            day = _ticker_day(market_id)
            if day is not None:
                candidates.add((day, station))

        # Decision-instant coverage, evaluated with the SAME tolerance the cohort
        # builder uses. Without this the table reports "book: YES" for a
        # station-day whose decision instant was never covered, which is how an
        # 18-hour outage looked survivable.
        from .pit import MAX_ORDERBOOK_AGE

        closes: dict[tuple[date, str], datetime] = {}
        for market_id, station, close_time in op.execute(
            "SELECT platform_market_id, settlement_station_id, close_time "
            "FROM markets WHERE series_ticker LIKE 'KXHIGH%' "
            "AND settlement_station_id != '' AND close_time IS NOT NULL"
        ):
            day = _ticker_day(market_id)
            if day is None or (day, station) in closes:
                continue
            stamp = datetime.fromisoformat(str(close_time).replace(" ", "T"))
            closes[(day, station)] = (
                stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            )

        decision_ok: dict[tuple[date, str], bool | None] = {}
        now_utc = datetime.now(timezone.utc)
        for key, close in closes.items():
            if key not in candidates:
                continue
            decision = close - timedelta(hours=DECISION_OFFSET_HOURS)
            if decision > now_utc:
                decision_ok[key] = None  # not yet due; not a failure
                continue
            newest = ar.execute(
                "SELECT MAX(available_at) FROM archived_books "
                "WHERE settlement_station_id = ? AND available_at <= ?",
                (key[1], decision.isoformat()),
            ).fetchone()[0]
            if newest is None:
                decision_ok[key] = False
                continue
            age = (decision - datetime.fromisoformat(newest)).total_seconds()
            decision_ok[key] = age <= MAX_ORDERBOOK_AGE.total_seconds()

        cli = {
            (date.fromisoformat(d), s)
            for d, s in op.execute(
                "SELECT observation_date, station_id FROM daily_climate_observations "
                "WHERE is_final = 1"
            )
        }
        forecasts = {
            (date.fromisoformat(d), s)
            for d, s in op.execute(
                "SELECT DISTINCT date(forecast_valid_start), settlement_station_id "
                "FROM source_observations WHERE research_eligible = 1 "
                "AND settlement_station_id != ''"
            )
            if d
        }
        venue: set[tuple[date, str]] = set()
        for market_id, station in op.execute(
            "SELECT m.platform_market_id, m.settlement_station_id "
            "FROM settlements s JOIN markets m ON m.id = s.market_id "
            "WHERE m.series_ticker LIKE 'KXHIGH%' AND s.result IN ('yes','no')"
        ):
            day = _ticker_day(market_id)
            if day is not None and station:
                venue.add((day, station))
    finally:
        op.close()
        ar.close()

    return [
        StationDay(
            target_date=day,
            station=station,
            has_forecast=(day, station) in forecasts,
            has_orderbook=True,
            has_cli_final=(day, station) in cli,
            has_venue_terminal=(day, station) in venue,
            book_at_decision=decision_ok.get((day, station)),
        )
        for day, station in sorted(candidates)
    ]


def render_conversions(rows: list[StationDay]) -> str:
    lines = [
        "",
        "--- STATION-DAY CONVERSION ---",
        f"  {'date':<12}{'stn':<7}{'fcst':<6}{'book':<6}{'CLI':<6}{'venue':<7}"
        f"{'dec':<5}{'clean':<7}blocker",
    ]
    blockers: dict[str, int] = {}
    clean = 0
    for r in rows:
        yn = lambda b: "YES" if b else "NO"  # noqa: E731
        dec = (
            "-" if r.book_at_decision is None else yn(r.book_at_decision)
        )
        if r.clean:
            clean += 1
        else:
            blockers[r.blocker.value] = blockers.get(r.blocker.value, 0) + 1
        lines.append(
            f"  {r.target_date.isoformat():<12}{r.station:<7}"
            f"{yn(r.has_forecast):<6}{yn(r.has_orderbook):<6}"
            f"{yn(r.has_cli_final):<6}{yn(r.has_venue_terminal):<7}"
            f"{dec:<5}{yn(r.clean):<7}{r.blocker.value}"
        )
    lines.append(f"  candidate station-days: {len(rows)}   ingredient-complete: {clean}")
    for name, n in sorted(blockers.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {name:<26} {n}")
    lines.append(
        "  note: ingredient-complete is necessary, not sufficient - the cohort "
        "also requires a book within staleness tolerance AT the decision instant."
    )
    return "\n".join(lines)
