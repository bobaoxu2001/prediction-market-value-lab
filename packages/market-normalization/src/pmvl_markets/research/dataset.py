"""Building the Model-vs-Market dataset, and reporting honestly when it is empty.

The readiness verdict is computed from gates, not asserted. An empty dataset has
two very different causes - the pipeline is broken, or the data has not arrived -
and they demand opposite responses, so the exclusion codes are partitioned into
defects and shortfalls and the verdict reads them separately.

Independence is tracked from the start because it is the easiest number to
inflate. The archive holds ~80 books for a single market inside one window;
those are 80 observations of one bet, not 80 bets. Anything inferential has to
count settlement events, and the builder therefore reports both.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contract import ContractParseError, parse_contract
from .exclusions import DEFECT_CODES, Exclusion


@dataclass
class BuildReport:
    """What the build found, including everything it refused."""

    generated_at: str = ""
    code_commit: str = ""
    # -- source inventory --------------------------------------------------
    forecast_snapshots: int = 0
    archived_books: int = 0
    archived_markets: int = 0
    settled_weather_markets: int = 0
    #: Settled markets captured in the last 6h. Non-zero means the collector is
    #: still spending budget on resolved markets; zero with historical
    #: BOOK_AFTER_SETTLEMENT rows means it used to and no longer does.
    settled_markets_still_captured: int = 0
    # -- join outcome ------------------------------------------------------
    joinable_observations: int = 0
    unique_markets: int = 0
    unique_settlement_events: int = 0
    exclusions: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)

    @property
    def defects(self) -> int:
        return sum(n for code, n in self.exclusions.items() if code in DEFECT_CODES)

    @property
    def shortfalls(self) -> int:
        return sum(n for code, n in self.exclusions.items() if code not in DEFECT_CODES)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "code_commit": self.code_commit,
            "sources": {
                "forecast_snapshots": self.forecast_snapshots,
                "archived_books": self.archived_books,
                "archived_markets": self.archived_markets,
                "settled_weather_markets": self.settled_weather_markets,
                "settled_markets_still_captured": self.settled_markets_still_captured,
            },
            "dataset": {
                "joinable_observations": self.joinable_observations,
                "unique_markets": self.unique_markets,
                "unique_settlement_events": self.unique_settlement_events,
            },
            "exclusions": {c.value: n for c, n in sorted(
                self.exclusions.items(), key=lambda kv: -kv[1]
            )},
            "defect_exclusions": self.defects,
            "shortfall_exclusions": self.shortfalls,
            "notes": list(self.notes),
        }


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def build_report(*, data_dir: Path, code_commit: str = "") -> BuildReport:
    """Inventory the sources and establish how many PIT-valid rows exist.

    Read-only against both stores. Research must never be able to mutate the
    archive it is measuring.
    """
    report = BuildReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        code_commit=code_commit,
    )
    operational = data_dir / "pmvl.db"
    archive = data_dir / "research-archive.db"
    if not operational.exists() or not archive.exists():
        report.notes.append("operational or archive database absent")
        return report

    op, ar = _ro(operational), _ro(archive)
    try:
        report.forecast_snapshots = op.execute(
            "SELECT COUNT(*) FROM source_observations"
        ).fetchone()[0]
        report.settled_weather_markets = op.execute(
            "SELECT COUNT(*) FROM settlements s JOIN markets m ON m.id = s.market_id "
            "WHERE m.series_ticker LIKE 'KXHIGH%'"
        ).fetchone()[0]
        report.archived_books = ar.execute(
            "SELECT COUNT(*) FROM archived_books"
        ).fetchone()[0]
        archived_ids = [
            r[0] for r in ar.execute(
                "SELECT DISTINCT platform_market_id FROM archived_books"
            )
        ]
        report.archived_markets = len(archived_ids)

        # Settlement is a LABEL. It is read here only to decide whether a market
        # has an outcome at all and when that outcome became known - never to
        # select or shape a feature.
        settled: dict[str, str] = {
            r["platform_market_id"]: r["settled_at"]
            for r in op.execute(
                "SELECT m.platform_market_id, s.settled_at "
                "FROM settlements s JOIN markets m ON m.id = s.market_id "
                "WHERE m.series_ticker LIKE 'KXHIGH%'"
            )
        }

        markets: dict[str, sqlite3.Row] = {
            r["platform_market_id"]: r
            for r in op.execute(
                "SELECT platform_market_id, strike_type, floor_strike, cap_strike, "
                "settlement_station_id FROM markets WHERE series_ticker LIKE 'KXHIGH%'"
            )
        }

        usable_markets: set[str] = set()
        events: set[tuple[str, str]] = set()

        for market_id in archived_ids:
            row = markets.get(market_id)
            if row is None:
                report.exclusions[Exclusion.CONTRACT_PARSE_FAILED] += 1
                continue
            if not row["settlement_station_id"]:
                report.exclusions[Exclusion.STATION_UNMAPPED] += 1
                continue
            try:
                parse_contract(
                    strike_type=row["strike_type"],
                    floor_strike=row["floor_strike"],
                    cap_strike=row["cap_strike"],
                )
            except ContractParseError:
                report.exclusions[Exclusion.CONTRACT_PARSE_FAILED] += 1
                continue

            settled_at = settled.get(market_id)
            if settled_at is None:
                # Open market: books exist but no outcome yet. A shortfall, not
                # a defect - this is the normal state while data accumulates.
                report.exclusions[Exclusion.NO_SETTLEMENT] += 1
                continue

            cutoff = settled_at.replace(" ", "T")
            pre = ar.execute(
                "SELECT COUNT(*) FROM archived_books "
                "WHERE platform_market_id = ? AND available_at < ?",
                (market_id, cutoff),
            ).fetchone()[0]
            if pre == 0:
                # Every book for this market was captured at or after it
                # settled. The price then embeds the outcome; scoring a model
                # against it would be measuring hindsight.
                report.exclusions[Exclusion.BOOK_AFTER_SETTLEMENT] += 1
                continue

            report.joinable_observations += pre
            usable_markets.add(market_id)
            events.add((row["settlement_station_id"], market_id.split("-")[1]))

        # Is the repaired bug still occurring, or is this only residue?
        recent = {
            r[0] for r in ar.execute(
                "SELECT DISTINCT platform_market_id FROM archived_books "
                # Six hours, not a day. The horizon fix landed mid-collection,
                # so a 24-hour window still contains captures from before it and
                # would report a repaired bug as live.
                "WHERE available_at >= datetime('now','-6 hours')"
            )
        }
        settled_ids = {
            r[0] for r in op.execute(
                "SELECT platform_market_id FROM markets "
                "WHERE series_ticker LIKE 'KXHIGH%' AND status = 'settled'"
            )
        }
        report.settled_markets_still_captured = len(recent & settled_ids)
        if report.exclusions.get(Exclusion.BOOK_AFTER_SETTLEMENT) and not (
            report.settled_markets_still_captured
        ):
            report.notes.append(
                "BOOK_AFTER_SETTLEMENT rows are residue from before the "
                "near-dated horizon existed; the collector no longer captures "
                "settled markets"
            )

        report.unique_markets = len(usable_markets)
        report.unique_settlement_events = len(events)
    finally:
        op.close()
        ar.close()
    return report


#: Minimum independent settlement events before calibration is meaningful. Well
#: below anything that would support an alpha claim; this is the threshold for
#: "a calibration curve is worth drawing at all".
MIN_EVENTS_FOR_CALIBRATION = 30


def readiness_verdict(report: BuildReport) -> tuple[str, list[str]]:
    """Compute the verdict from gates, and say which gate decided it."""
    reasons: list[str] = []

    if report.defects:
        offenders = ", ".join(
            f"{c.value}={n}" for c, n in report.exclusions.items() if c in DEFECT_CODES
        )
        reasons.append(f"defect-class exclusions present: {offenders}")
        return "FOUNDATION PARTIAL — BLOCKERS REMAIN", reasons

    if report.joinable_observations == 0:
        reasons.append(
            "no point-in-time observation exists: no settled market has an "
            "orderbook captured before it settled"
        )
        return "FOUNDATION PARTIAL — BLOCKERS REMAIN", reasons

    if report.unique_settlement_events < MIN_EVENTS_FOR_CALIBRATION:
        reasons.append(
            f"{report.unique_settlement_events} independent settlement events; "
            f"{MIN_EVENTS_FOR_CALIBRATION} needed before calibration is meaningful"
        )
        return "FOUNDATION PARTIAL — BLOCKERS REMAIN", reasons

    reasons.append("all correctness gates satisfied and sample is non-trivial")
    return "MODEL-VS-MARKET FOUNDATION READY", reasons


def render(report: BuildReport) -> str:
    verdict, reasons = readiness_verdict(report)
    d = report.as_dict()
    lines = [
        "",
        "MODEL-VS-MARKET READINESS",
        "=" * 68,
        "",
        "--- SOURCES ---",
        f"  forecast snapshots        {d['sources']['forecast_snapshots']:>8}",
        f"  archived books            {d['sources']['archived_books']:>8}",
        f"  archived markets          {d['sources']['archived_markets']:>8}",
        f"  settled weather markets   {d['sources']['settled_weather_markets']:>8}",
        f"  settled still captured    {d['sources']['settled_markets_still_captured']:>8}"
        "   (must be 0)",
        "",
        "--- DATASET ---",
        f"  joinable PIT observations {d['dataset']['joinable_observations']:>8}",
        f"  unique markets            {d['dataset']['unique_markets']:>8}",
        f"  independent events        {d['dataset']['unique_settlement_events']:>8}",
        "",
        "--- EXCLUSIONS ---",
    ]
    for code, n in d["exclusions"].items():
        tag = "  (defect)" if Exclusion(code) in DEFECT_CODES else ""
        lines.append(f"  {code:<28} {n:>8}{tag}")
    if not d["exclusions"]:
        lines.append("  (none)")
    lines += ["", f"  defect-class    {report.defects}", f"  shortfall-class {report.shortfalls}", ""]
    for note in d["notes"]:
        lines.append(f"  note: {note}")
    lines += ["", f"VERDICT: {verdict}"]
    lines += [f"  because: {r}" for r in reasons]
    lines.append("")
    return "\n".join(lines)


def probability_units_are_consistent(*values: Decimal) -> bool:
    """Guard against a cents/probability mix-up.

    A single unmixed slip is a factor of 100 and produces an edge that looks
    spectacular rather than wrong, so it is checked rather than assumed.
    """
    return all(Decimal(0) <= v <= Decimal(1) for v in values)
