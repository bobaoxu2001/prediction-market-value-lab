"""Is collection actually happening, and is what it captured usable?

Two questions, and the second is the one that gets missed. A collector can run
perfectly and still produce a dataset that cannot test the hypothesis - because
``published_at`` was absent on most rows, or because the high-frequency windows
never fired, or because responses came from a cache and carry a fabricated
availability.

So this reports coverage *and* the properties that decide usability, against
gates that are stated rather than implied. Every number is deterministic: given
the same database and the same lookback it produces the same report, with no
clock-dependent term except the lookback boundary the caller supplies.

**This computes no returns.** No markout, no forward price, no IC, no PnL. It
counts rows and compares them to expectations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmvl_shared.research_archive import (
    ResearchArchive,
    archive_freshness,
    default_archive_path,
)
from pmvl_shared.timeutil import ensure_utc, utcnow

from ..db_models import (
    Market,
    PollAttemptRow,
    SourceObservationRow,
    StationObservationRow,
)
from ..normalize.weather_stations import (
    RESEARCH_SERIES_PREFIX,
    parse_settlement_station,
    research_station_ids,
)
from .issuance import RESEARCH_CADENCE_SECONDS, windows_between


@dataclass(frozen=True)
class Gate:
    """One stated expectation and whether it is met.

    ``expected`` is carried beside ``actual`` so a shortfall reads as "37 of an
    expected 120" rather than as a bare count that could mean anything.
    """

    name: str
    actual: float
    expected: float
    #: True when higher is better. False for gates that must stay at zero.
    higher_is_better: bool = True
    detail: str = ""

    @property
    def passing(self) -> bool:
        return (
            self.actual >= self.expected
            if self.higher_is_better
            else self.actual <= self.expected
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actual": self.actual,
            "expected": self.expected,
            "passing": self.passing,
            # Carried, not inferred. The renderer used to decide the comparison
            # direction from a hardcoded set of gate NAMES, so any gate added
            # without also editing that set printed the wrong operator - which
            # is how a lower-is-better staleness gate came to read
            # "0.9 (expect >= 20)" while passing correctly.
            "higher_is_better": self.higher_is_better,
            "detail": self.detail,
        }


#: The gates. Stated here so a reader can disagree with them in one place.
MIN_OBSERVATIONS_PER_STATION_DAY = 20
MIN_SOURCE_COVERAGE = 0.95
MIN_BOOKS_PER_ISSUANCE_WINDOW = 40

#: How stale the newest archived book may be before collection is considered
#: stopped, in minutes.
#:
#: Every other gate is retrospective: they read a lookback window or the best
#: COMPLETED issuance window, so they keep passing on yesterday's evidence. On
#: 2026-08-26 the collector wrote nothing for 76 minutes while the worker
#: process stayed alive and the scheduler kept firing - and every gate still
#: reported passing, because none of them asked the only question that would
#: have caught it: is data still arriving *now*.
#:
#: Generous on purpose. Baseline cadence is 180s, and a gate that fires on a
#: single slow cycle trains an operator to ignore it.
MAX_ARCHIVE_STALENESS_MINUTES = 20


def best_completed_window(
    window_counts: dict[str, int], completed: set[str] | frozenset[str]
) -> int:
    """Captures for the best-covered market in the best COMPLETED window.

    The acceptance question is whether the collector can sustain cadence through
    a window at all - "at least one window reached the bar" - so it is a max over
    completed windows, not a min over every window in the lookback.

    Taking the min asked a different question than the gate's own label. It fails
    unless EVERY window was well sampled, so windows that predate the collector's
    existence pinned the gate to zero and no amount of healthy collection could
    clear it. Whether collection has been *continuous* is a real and separate
    question; it is reported on its own line and preserved in the append-only
    health history, where a gap cannot be erased by a later good day.

    In-progress windows are excluded: one has not had its chance yet, and
    counting it would report a healthy collector as failing for thirty minutes
    out of every six hours.
    """
    return max((window_counts[a] for a in completed if a in window_counts), default=0)


def build_report(session: Session, *, days: int = 1, now: datetime | None = None) -> dict[str, Any]:
    """The full report as plain data. JSON-serialisable, deterministic."""
    now = ensure_utc(now or utcnow())
    since = now - timedelta(days=days)

    stations = research_station_ids()
    station_days = max(1, days) * len(stations)

    # --- station observations ------------------------------------------------
    per_station: dict[str, int] = {}
    for station_id, count in session.execute(
        select(
            StationObservationRow.station_id, func.count()
        )
        .where(StationObservationRow.available_at >= since)
        .group_by(StationObservationRow.station_id)
    ):
        per_station[station_id] = int(count)
    for station_id in stations:
        per_station.setdefault(station_id, 0)

    observations_total = sum(per_station.values())
    expected_observations = station_days * MIN_OBSERVATIONS_PER_STATION_DAY
    worst_station = min(per_station.items(), key=lambda kv: kv[1]) if per_station else ("", 0)

    # --- poll attempts -------------------------------------------------------
    attempts = session.scalars(
        select(PollAttemptRow).where(PollAttemptRow.started_at >= since)
    ).all()
    failures = [a for a in attempts if not a.success]
    cached = [a for a in attempts if a.served_from_cache]
    by_error: dict[str, int] = {}
    for attempt in failures:
        key = attempt.error_type or "unclassified"
        by_error[key] = by_error.get(key, 0) + 1

    # --- source observations and timestamp completeness ----------------------
    sources = session.scalars(
        select(SourceObservationRow).where(SourceObservationRow.available_at >= since)
    ).all()
    with_published = [s for s in sources if s.published_at is not None]
    absence: dict[str, int] = {}
    for row in sources:
        missing = ((row.timestamp_absence or {}).get("missing") or {})
        for field_name in missing:
            absence[field_name] = absence.get(field_name, 0) + 1

    latest_by_source: dict[str, str] = {}
    for source_id, latest in session.execute(
        select(SourceObservationRow.source_id, func.max(SourceObservationRow.available_at))
        .group_by(SourceObservationRow.source_id)
    ):
        latest_by_source[source_id] = str(latest) if latest else ""
    station_latest = session.scalar(select(func.max(StationObservationRow.available_at)))
    if station_latest:
        latest_by_source["nws_station_observations"] = str(station_latest)

    # --- settlement mapping --------------------------------------------------
    mapping = _mapping_report(session)

    # --- research archive ----------------------------------------------------
    archive_stats, per_market, window_counts, completed_windows = _archive_report(
        since=since, now=now,
        observed_published=[s.published_at for s in with_published],
    )

    gates = [
        Gate(
            "station observations per station-day",
            actual=round(observations_total / station_days, 1),
            expected=MIN_OBSERVATIONS_PER_STATION_DAY,
            detail=f"worst station {worst_station[0] or 'n/a'}={worst_station[1]}",
        ),
        Gate(
            "source poll success rate",
            actual=round(
                (len(attempts) - len(failures)) / len(attempts), 4
            ) if attempts else 0.0,
            expected=MIN_SOURCE_COVERAGE,
            detail=f"{len(failures)} failed of {len(attempts)} attempts",
        ),
        Gate(
            "books per issuance window (best market)",
            actual=best_completed_window(window_counts, completed_windows),
            expected=MIN_BOOKS_PER_ISSUANCE_WINDOW,
            detail=(
                f"best of {len(completed_windows)} completed "
                f"({len(window_counts)} in range)"
            ),
        ),
        Gate(
            "research cache hits",
            actual=len(cached),
            expected=0,
            higher_is_better=False,
            detail="a cached body stamped with a new available_at is fabricated",
        ),
        Gate(
            "unmapped settlement stations",
            actual=mapping["unmapped"],
            expected=0,
            higher_is_better=False,
            detail="silently unmapped markets must be zero",
        ),
        Gate(
            "payload integrity failures",
            actual=mapping["payload_failures"],
            expected=0,
            higher_is_better=False,
        ),
        Gate(
            "archive staleness (minutes)",
            actual=archive_staleness_minutes(archive_stats, now=now),
            expected=MAX_ARCHIVE_STALENESS_MINUTES,
            higher_is_better=False,
            detail="a live worker with a frozen archive passes every other gate",
        ),
    ]

    return {
        "generated_at": now.isoformat(),
        "lookback_days": days,
        "window": [since.isoformat(), now.isoformat()],
        # Stated on the report itself so it cannot be quoted as anything else.
        "scope": (
            "COLLECTION HEALTH ONLY. Counts rows and compares them to stated "
            "gates. No markout, forward return, IC, Sharpe or PnL is computed."
        ),
        "station_observations": {
            "total": observations_total,
            "per_station": dict(sorted(per_station.items())),
            "station_days": station_days,
            "expected_total": expected_observations,
            "coverage": (
                f"{observations_total / expected_observations:.1%}"
                if expected_observations
                else "n/a"
            ),
        },
        "poll_attempts": {
            "total": len(attempts),
            "failed": len(failures),
            "success_rate": (
                f"{(len(attempts) - len(failures)) / len(attempts):.1%}"
                if attempts
                else "n/a"
            ),
            "by_error_type": dict(sorted(by_error.items())),
            "served_from_cache": len(cached),
        },
        "source_observations": {
            "total": len(sources),
            "with_published_at": len(with_published),
            "published_at_coverage": (
                f"{len(with_published) / len(sources):.1%}" if sources else "n/a"
            ),
            "timestamp_absence": dict(sorted(absence.items())),
        },
        "latest_available_at": dict(sorted(latest_by_source.items())),
        "settlement_mapping": mapping,
        "research_archive": {
            **archive_stats,
            "freshness": archive_freshness_line(archive_stats),
            "books_per_market": per_market,
            "books_per_issuance_window": dict(sorted(window_counts.items())),
            "completed_issuance_windows": sorted(completed_windows),
            # Continuity, reported next to the gate but never folded into it.
            # A gap here is a fact about the past that a later good window must
            # not erase, which is why it also lands in the append-only history.
            "completed_windows_meeting_gate": sum(
                1
                for a in completed_windows
                if window_counts[a] >= MIN_BOOKS_PER_ISSUANCE_WINDOW
            ),
        },
        "gates": [g.as_dict() for g in gates],
        "all_gates_passing": all(g.passing for g in gates),
    }


def _mapping_report(session: Session) -> dict[str, Any]:
    """Mapped / unmapped counts, with the unmapped listed explicitly."""
    rows = session.scalars(
        select(Market).where(Market.series_ticker.like(f"{RESEARCH_SERIES_PREFIX}%"))
    ).all()
    mapped, inexact, persisted = 0, 0, 0
    unmapped: list[dict[str, str]] = []
    by_station: dict[str, int] = {}
    for market in rows:
        result = parse_settlement_station(
            market.settlement_rules_raw, series_ticker=market.series_ticker
        )
        if not result.mapped:
            unmapped.append(
                {
                    "market": market.platform_market_id,
                    "series": market.series_ticker or "",
                    "reason": result.reason.value if result.reason else "",
                }
            )
            continue
        mapped += 1
        if not result.exact:
            inexact += 1
        if market.settlement_station_id == result.station_id:
            persisted += 1
        by_station[result.station_id] = by_station.get(result.station_id, 0) + 1

    return {
        "research_markets": len(rows),
        "mapped": mapped,
        "unmapped": len(unmapped),
        "inexact": inexact,
        "persisted_to_column": persisted,
        "by_station": dict(sorted(by_station.items())),
        "unmapped_markets": unmapped[:25],
        "payload_failures": _payload_failures(),
    }


def _payload_failures() -> int:
    """Objects in the payload store that no longer hash to their reference."""
    from pmvl_shared.config import DEFAULT_SQLITE_PATH
    from pmvl_shared.payload_store import PayloadStore

    store = PayloadStore(DEFAULT_SQLITE_PATH.parent / "payloads")
    if not store.root.exists():
        return 0
    return len(store.verify_all())


def _archive_report(
    *, since: datetime, now: datetime, observed_published: Iterable[datetime]
) -> tuple[dict[str, Any], dict[str, int], dict[str, int], set[str]]:
    """Archive growth, books per market, and books per issuance window."""
    from pmvl_shared.config import DEFAULT_SQLITE_PATH

    archive = ResearchArchive(default_archive_path(DEFAULT_SQLITE_PATH.parent.parent))
    stats = archive.stats().as_dict()
    if not archive.path.exists():
        return stats, {}, {}, set()

    published = [p for p in observed_published if p is not None]
    windows = windows_between(since, now, observed_published_at=published)

    per_market: dict[str, int] = {}
    window_counts: dict[str, int] = {}
    #: Anchors whose window has actually ended. A window still in progress has
    #: not had its chance yet, and counting it as a shortfall would report a
    #: healthy collector as failing for thirty minutes out of every six hours.
    completed: set[str] = set()
    with archive.connect() as con:
        for market_key, count in con.execute(
            "SELECT platform || ':' || platform_market_id, COUNT(*) "
            "FROM archived_books WHERE observed_at >= ? GROUP BY 1 "
            "ORDER BY 2 DESC LIMIT 25",
            (since.isoformat(),),
        ):
            per_market[market_key] = int(count)

        for window in windows:
            # Books for the BEST-COVERED single market, not the row total.
            #
            # The row total passes trivially: one capture of 252 markets is 252
            # books. The question the gate is meant to ask is whether any market
            # was captured at high frequency THROUGH the window, and that is a
            # per-market count.
            row = con.execute(
                "SELECT MAX(n) FROM (SELECT COUNT(*) n FROM archived_books "
                " WHERE observed_at >= ? AND observed_at <= ? "
                " GROUP BY platform, platform_market_id)",
                (window.start.isoformat(), window.end.isoformat()),
            ).fetchone()
            anchor = window.anchor.isoformat()
            window_counts[anchor] = int(row[0] or 0)
            if window.end <= now:
                completed.add(anchor)

    return stats, per_market, window_counts, completed


def archive_staleness_minutes(
    stats: Mapping[str, Any], *, now: datetime | None = None
) -> float:
    """Minutes since the newest archived book became available.

    The one present-tense question in the health report. Everything else can be
    satisfied by history, which is how a 76-minute outage went unreported while
    the worker was alive and scheduling.

    An empty archive returns infinity rather than zero: "nothing has ever been
    collected" is not freshness.
    """
    latest = stats.get("latest")
    if not latest:
        return float("inf")
    try:
        stamp = ensure_utc(
            latest if isinstance(latest, datetime) else datetime.fromisoformat(str(latest))
        )
    except (TypeError, ValueError):
        return float("inf")
    delta = (ensure_utc(now or utcnow()) - stamp).total_seconds() / 60.0
    return max(0.0, round(delta, 1))


def archive_freshness_line(stats: dict[str, Any]) -> str:
    from pmvl_shared.research_archive import ArchiveStats

    return archive_freshness(
        ArchiveStats(
            books=stats.get("books", 0), levels=stats.get("levels", 0),
            trades=stats.get("trades", 0), markets=stats.get("markets", 0),
            earliest=stats.get("earliest"), latest=stats.get("latest"),
            size_bytes=stats.get("size_bytes", 0),
        )
    )


def render(report: dict[str, Any]) -> list[str]:
    """Human-readable lines. Same data as the JSON, no extra computation."""
    lines = [
        "=" * 74,
        "COLLECTION HEALTH",
        "=" * 74,
        report["scope"],
        "",
        f"window: {report['window'][0]} .. {report['window'][1]}",
        "",
        "--- STATION OBSERVATIONS ---",
    ]
    obs = report["station_observations"]
    lines.append(
        f"  total {obs['total']} over {obs['station_days']} station-days "
        f"(expected {obs['expected_total']}, coverage {obs['coverage']})"
    )
    for station, count in obs["per_station"].items():
        flag = "" if count >= MIN_OBSERVATIONS_PER_STATION_DAY else "   << below gate"
        lines.append(f"    {station:8s} {count:5d}{flag}")

    polls = report["poll_attempts"]
    lines += [
        "",
        "--- POLL ATTEMPTS ---",
        f"  {polls['total']} attempts, {polls['failed']} failed "
        f"(success {polls['success_rate']}), cached {polls['served_from_cache']}",
    ]
    for error, count in polls["by_error_type"].items():
        lines.append(f"    {error:20s} {count}")

    src = report["source_observations"]
    lines += [
        "",
        "--- SOURCE OBSERVATIONS ---",
        f"  {src['total']} rows, published_at on {src['with_published_at']} "
        f"({src['published_at_coverage']})",
    ]
    for field_name, count in src["timestamp_absence"].items():
        lines.append(f"    absent: {field_name:24s} {count}")

    mapping = report["settlement_mapping"]
    lines += [
        "",
        "--- SETTLEMENT MAPPING ---",
        f"  {mapping['mapped']} mapped / {mapping['unmapped']} unmapped "
        f"of {mapping['research_markets']} research markets "
        f"({mapping['inexact']} inexact, {mapping['persisted_to_column']} persisted)",
    ]
    for station, count in mapping["by_station"].items():
        lines.append(f"    {station:8s} {count}")
    for row in mapping["unmapped_markets"]:
        lines.append(f"    UNMAPPED {row['market']} ({row['reason']})")

    arc = report["research_archive"]
    lines += ["", "--- RESEARCH ARCHIVE ---", f"  {arc['freshness']}"]
    done = set(arc.get("completed_issuance_windows", []))
    for window, count in arc["books_per_issuance_window"].items():
        if window not in done:
            flag = "   (in progress)"
        elif count >= MIN_BOOKS_PER_ISSUANCE_WINDOW:
            flag = ""
        else:
            flag = "   << below gate"
        lines.append(f"    window {window}  books={count}{flag}")
    lines.append(
        f"  continuity: {arc.get('completed_windows_meeting_gate', 0)}"
        f"/{len(done)} completed windows met the >={MIN_BOOKS_PER_ISSUANCE_WINDOW} bar"
    )

    lines += ["", "--- LATEST available_at BY SOURCE ---"]
    for source, latest in report["latest_available_at"].items():
        lines.append(f"    {source:32s} {latest or 'never'}")

    lines += ["", "--- GATES ---"]
    for gate in report["gates"]:
        mark = "PASS" if gate["passing"] else "FAIL"
        lines.append(
            f"  [{mark}] {gate['name']:38s} {gate['actual']} "
            f"(expect {'>=' if gate.get('higher_is_better', True) else '<='} "
            f"{gate['expected']})  {gate['detail']}"
        )
    lines.append("")
    lines.append(
        "ALL GATES PASSING" if report["all_gates_passing"] else "GATES FAILING"
    )
    return lines


