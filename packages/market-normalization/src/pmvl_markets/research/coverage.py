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
