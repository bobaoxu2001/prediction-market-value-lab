#!/usr/bin/env python3
"""Independently verify one issuance window straight from the archive.

Deliberately does NOT import the collection-health module. Its whole value is
being a second implementation: if this and ``pmvl collection-health`` disagree,
one of them is wrong and that disagreement is the finding.

Everything below is raw SQL against the immutable research archive and the
operational job history, printed with the query that produced it so a reader can
paste it into sqlite3 and get the same number.

    python scripts/verify_issuance_window.py --start 2026-08-26T07:30:00 \
                                             --end   2026-08-26T08:30:00
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data" / "research-archive.db"
OPERATIONAL = ROOT / "data" / "pmvl.db"


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _say(label: str, value: object, sql: str = "") -> None:
    print(f"  {label:<44} {value}")
    if sql:
        print(f"       SQL: {' '.join(sql.split())}")


def _parse(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--gate", type=int, default=40)
    ap.add_argument("--cadence", type=float, default=30.0)
    args = ap.parse_args()

    start, end = _parse(args.start), _parse(args.end)
    s, e = start.isoformat(), end.isoformat()
    print(f"\nINDEPENDENT WINDOW VERIFICATION  {s} .. {e}")
    print("=" * 78)

    if not ARCHIVE.exists():
        print("  research archive absent")
        return 1

    con = _ro(ARCHIVE)

    # ---- capture counts -------------------------------------------------
    print("\n-- CAPTURES --")
    q_best = (
        "SELECT platform_market_id, COUNT(*) c FROM archived_books "
        "WHERE observed_at >= ? AND observed_at <= ? GROUP BY 1 ORDER BY c DESC"
    )
    per_market = con.execute(q_best, (s, e)).fetchall()
    best = per_market[0] if per_market else ("(none)", 0)
    _say("markets with >=1 capture", len(per_market))
    _say("best market", f"{best[0]}  n={best[1]}", q_best)
    _say("median captures per market",
         int(statistics.median([r[1] for r in per_market])) if per_market else 0)
    _say(f"markets reaching the >={args.gate} bar",
         sum(1 for r in per_market if r[1] >= args.gate))
    _say("total rows in window",
         con.execute(
             "SELECT COUNT(*) FROM archived_books WHERE observed_at>=? AND observed_at<=?",
             (s, e)).fetchone()[0])

    # ---- timestamp integrity -------------------------------------------
    print("\n-- TIMESTAMP INTEGRITY --")
    mid = best[0]
    _say("distinct observed_at (best market)",
         con.execute(
             "SELECT COUNT(DISTINCT observed_at) FROM archived_books "
             "WHERE platform_market_id=? AND observed_at>=? AND observed_at<=?",
             (mid, s, e)).fetchone()[0])
    _say("distinct available_at (best market)",
         con.execute(
             "SELECT COUNT(DISTINCT available_at) FROM archived_books "
             "WHERE platform_market_id=? AND observed_at>=? AND observed_at<=?",
             (mid, s, e)).fetchone()[0])
    q_dup = (
        "SELECT COUNT(*) FROM (SELECT platform, platform_market_id, observed_at "
        "FROM archived_books WHERE observed_at>=? AND observed_at<=? "
        "GROUP BY 1,2,3 HAVING COUNT(*)>1)"
    )
    dups = con.execute(q_dup, (s, e)).fetchone()[0]
    _say("duplicate (market, observed_at) groups", dups, q_dup)
    q_order = (
        "SELECT COUNT(*) FROM archived_books "
        "WHERE observed_at>=? AND observed_at<=? AND available_at < observed_at"
    )
    inverted = con.execute(q_order, (s, e)).fetchone()[0]
    _say("rows with available_at < observed_at", f"{inverted}   (must be 0)", q_order)
    lat = con.execute(
        "SELECT AVG((julianday(available_at)-julianday(observed_at))*86400.0), "
        "       MAX((julianday(available_at)-julianday(observed_at))*86400.0) "
        "FROM archived_books WHERE observed_at>=? AND observed_at<=?",
        (s, e)).fetchone()
    if lat and lat[0] is not None:
        _say("observed_at -> available_at lag (mean/max s)",
             f"{lat[0]:.2f} / {lat[1]:.2f}")

    # ---- cadence --------------------------------------------------------
    print("\n-- CADENCE (best market) --")
    stamps = [
        _parse(r[0]) for r in con.execute(
            "SELECT available_at FROM archived_books WHERE platform_market_id=? "
            "AND observed_at>=? AND observed_at<=? ORDER BY available_at",
            (mid, s, e))
    ]
    gaps = sorted(
        (b - a).total_seconds() for a, b in zip(stamps, stamps[1:])
    )
    if gaps:
        def pct(p: float) -> float:
            return gaps[min(len(gaps) - 1, int(len(gaps) * p))]
        _say("n intervals", len(gaps))
        _say("min / median / max",
             f"{gaps[0]:.1f} / {statistics.median(gaps):.1f} / {gaps[-1]:.1f} s")
        _say("p90 / p95", f"{pct(0.90):.1f} / {pct(0.95):.1f} s")
        _say(f"median <= {args.cadence:g}s",
             "PASS" if statistics.median(gaps) <= args.cadence else "FAIL")
        _say("longest missing interval", f"{gaps[-1]:.1f} s")
        _say("intervals over 2x cadence",
             sum(1 for g in gaps if g > 2 * args.cadence))
    else:
        _say("n intervals", 0)

    # ---- boundary behaviour --------------------------------------------
    print("\n-- BOUNDARY BEHAVIOUR --")
    for label, lo, hi in (
        ("pre-window  (30m before)", start - timedelta(minutes=30), start),
        ("in-window", start, end),
        ("post-window (30m after)", end, end + timedelta(minutes=30)),
    ):
        n = con.execute(
            "SELECT COUNT(DISTINCT available_at) FROM archived_books "
            "WHERE platform_market_id=? AND available_at>=? AND available_at<?",
            (mid, lo.isoformat(), hi.isoformat())).fetchone()[0]
        _say(label, f"{n} captures")

    # ---- integrity / mapping -------------------------------------------
    print("\n-- ARCHIVE INTEGRITY --")
    _say("books with no settlement_station_id",
         con.execute(
             "SELECT COUNT(*) FROM archived_books WHERE observed_at>=? AND "
             "observed_at<=? AND (settlement_station_id IS NULL OR "
             "settlement_station_id='')", (s, e)).fetchone()[0])
    _say("distinct settlement stations",
         con.execute(
             "SELECT COUNT(DISTINCT settlement_station_id) FROM archived_books "
             "WHERE observed_at>=? AND observed_at<=?", (s, e)).fetchone()[0])
    _say("levels archived in window",
         con.execute(
             "SELECT COUNT(*) FROM archived_levels l JOIN archived_books b "
             "ON l.book_id=b.id WHERE b.observed_at>=? AND b.observed_at<=?",
             (s, e)).fetchone()[0])
    con.close()

    # ---- scheduler evidence --------------------------------------------
    print("\n-- SCHEDULER / LIVENESS --")
    if not OPERATIONAL.exists():
        print("  operational database absent")
        return 0
    op = _ro(OPERATIONAL)
    # The operational store writes naive UTC ("YYYY-MM-DD HH:MM:SS.ffffff"),
    # while the archive writes ISO-8601 with an offset. Comparing one format
    # against the other silently matches nothing, which reads as "the scheduler
    # did not run" rather than as a bad query - so normalise explicitly.
    ns = start.replace(tzinfo=None).isoformat(sep=" ")
    ne = end.replace(tzinfo=None).isoformat(sep=" ")
    rows = op.execute(
        "SELECT job_name,started_at,finished_at,status,heartbeat_at FROM job_runs "
        "WHERE job_name='research_books' AND started_at>=? AND started_at<=? "
        "ORDER BY started_at", (ns, ne)).fetchall()
    _say("research_books rows in window", len(rows))
    for r in rows[:6]:
        print(f"       {r[1][:19]}  {r[3]:<11} hb={str(r[4])[:19]}")
    _say("rows with a heartbeat", sum(1 for r in rows if r[4]))
    q_reclaim = (
        "SELECT COUNT(*) FROM job_runs WHERE status='interrupted' "
        "AND started_at>=? AND started_at<=?"
    )
    _say("stale-run reclaim events", op.execute(q_reclaim, (ns, ne)).fetchone()[0],
         q_reclaim)
    _say("skipped (slot held) events",
         op.execute(
             "SELECT COUNT(*) FROM job_runs WHERE status='skipped' AND "
             "job_name='research_books' AND started_at>=? AND started_at<=?",
             (ns, ne)).fetchone()[0])
    try:
        q_att = (
            "SELECT COUNT(*), SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) "
            "FROM poll_attempts WHERE started_at>=? AND started_at<=?"
        )
        att = op.execute(q_att, (ns, ne)).fetchone()
        _say("source poll attempts / failures",
             f"{att[0]} / {att[1] or 0}", q_att)
        served = op.execute(
            "SELECT COUNT(*) FROM poll_attempts WHERE started_at>=? AND "
            "started_at<=? AND served_from_cache=1", (ns, ne)).fetchone()[0]
        _say("served from cache (must be 0)", served)
    except sqlite3.Error as exc:
        _say("source poll attempts", f"unavailable: {exc}")
    op.close()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
