"""``python -m pmvl_worker.lip_cli`` - the LIP shadow study's recorder and replay.

Deliberately *not* registered in ``pmvl_worker.cli``: the maker study's daily cron
imports that module, and it must import exactly what it imported before this study
existed. Nothing here sends an order or reads a credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from pmvl_markets.lip.health import day_health
from pmvl_markets.lip.recorder import LipRecorder, LockHeld, WindowRefused, day_window

app = typer.Typer(add_completion=False, help="LIP maker shadow study (read-only).")
console = Console()

REPO = Path(__file__).resolve().parents[4]
FORWARD = REPO / "artifacts" / "forward"
KNOWN_SERIES = REPO / "artifacts" / "research" / "lip-1" / "known_series.json"
PREREG = "docs/lip-preregistration.md"


def _head_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _offday_guard(command: str) -> None:
    """Commands that call Kalshi outside a recording day obey the same isolation: never
    inside the maker study's window, never while its recorder runs."""
    from pmvl_markets.lip.recorder import maker_busy, refused

    now = datetime.now(timezone.utc)
    if refused(now.time()):
        console.print(f"[red]{command} refused: {now.time()} UTC is inside the maker study's window 02:00-12:25")
        raise typer.Exit(3)
    if maker_busy():
        console.print(f"[red]{command} refused: the maker study's recorder is running")
        raise typer.Exit(3)


def _log_offday(command: str, first: float, last: float, n: int) -> None:
    from pmvl_markets.recording.writer import AppendOnlyLog

    AppendOnlyLog(FORWARD / "lip-offday-requests.jsonl").append(
        {"command": command, "first": first, "last": last, "n": n})


def _prereg_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%H", "--", PREREG],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _cadence_sha() -> str:
    path = FORWARD / "lip-cadence.json"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


@app.command("record-day")
def record_day(
    shakedown: bool = typer.Option(False, help="Record under lip-shakedown/: never scored or pooled."),
    until: str = typer.Option("", help="ISO time to stop early (shakedown only)."),
) -> None:
    """Record one study day (12:25 UTC to 02:00 UTC), or a shakedown run."""
    if until and not shakedown:
        console.print("[red]--until is for shakedown runs; a study day runs its full window")
        raise typer.Exit(2)
    now = datetime.now(timezone.utc)
    window = day_window(now)
    if until and not (now < datetime.fromisoformat(until) <= window.drain_at):
        console.print(f"[red]--until must fall between now and the drain time {window.drain_at.isoformat()}")
        raise typer.Exit(2)
    root = FORWARD / ("lip-shakedown" if shakedown else "lip-daily")
    out = root / window.day.isoformat()
    state = root / "admission_state.json"
    known = set(json.loads(KNOWN_SERIES.read_text())) if KNOWN_SERIES.exists() else set()
    from dataclasses import replace as _replace

    from pmvl_markets.lip.params import RECORDING

    overrides = _cadence_overrides()
    if overrides.get("book_cadence_s") and overrides.get("book_cadence_s") != 1.0:
        console.print(f"[yellow]cadence.json sets the book cadence to {overrides['book_cadence_s']} s")
    recorder = LipRecorder(
        params=_replace(RECORDING, **overrides),
        out_dir=out, state_path=state, settlements_path=FORWARD / (
            "lip-shakedown-settlements.jsonl" if shakedown else "lip-settlements.jsonl"),
        known_series=known, shakedown=shakedown,
        stop_at=datetime.fromisoformat(until) if until else None,
        prereg_commit=_prereg_commit(), code_commit=_head_commit(), cadence_sha256=_cadence_sha(),
    )
    try:
        health = asyncio.run(recorder.run())
    except WindowRefused as exc:
        console.print(f"[red]refused: {exc}")
        raise typer.Exit(3) from exc
    except LockHeld as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(4) from exc
    console.print_json(json.dumps(health))
    if health.get("skipped"):
        raise typer.Exit(6)  # the cron script logs a skipped day like any other non-zero exit


def _cadence_overrides() -> dict:
    path = FORWARD / "lip-cadence.json"
    if not path.exists():
        return {}
    body = json.loads(path.read_text())
    out = {k: body[k] for k in ("tape_limit", "book_cadence_s") if k in body}
    if "min_ts_honoured" in body:
        out["min_ts_honoured"] = bool(body["min_ts_honoured"])
    if body.get("tape_mode") == "TAPE_PT":
        # the per-ticker tape: one book lane and a total cap of 100 (section 8)
        out.update(tape_mode="per_ticker", book_lanes=1, total_cap=100)
    return out


@app.command("preflight")
def preflight(
    shakedown_dir: Path = typer.Argument(..., help="The shakedown day to read (60+ minutes in the window)."),
    write: bool = typer.Option(False, help="Write artifacts/forward/lip-cadence.json."),
) -> None:
    """Gate G5: the pre-registered cadence ladder, from a shakedown and one live read.

    - L = 1000 if a limit=1000 tape read returns more than 200 rows, else 200;
    - whether min_ts is honoured (no row older than it comes back);
    - TAPE_PT (per-ticker tape) if the tape's p95 cost exceeds 1.5 requests/s or the
      audit found any print missing from the global feed;
    - a 2 s book cadence for the whole study if any 429 appeared.
    """
    from pmvl_markets.lip.params import RECORDING
    from pmvl_markets.lip.reader import load_day
    from pmvl_markets.lip.store import read_stream

    polls = list(read_stream(shakedown_dir, "tape_polls"))
    if not polls:
        console.print("[red]no tape polls in that directory")
        raise typer.Exit(1)
    first, last = polls[0]["sent_at"], polls[-1]["sent_at"]
    minutes = (last - first) / 60
    pages = sorted(p["pages"] for p in polls)
    p95_pages = pages[int(0.95 * (len(pages) - 1))]
    p95_cost = p95_pages / RECORDING.tape_every_s
    cap_hits = sum(1 for p in polls if p.get("page_cap_hit"))
    incomplete = sum(1 for p in polls if not p.get("complete"))
    day = load_day(shakedown_dir)
    mismatches = len(day.coverage.audit_mismatch)
    health = json.loads((shakedown_dir / "health.json").read_text()) if (shakedown_dir / "health.json").exists() else {}
    rate_limited = sum(v.get("rate_limited", 0) for v in (health.get("lanes") or {}).values())

    async def live_probe() -> dict:
        from pmvl_markets.lip.http import lane_provider

        p = lane_provider("preflight", 0.5, attempts=1)
        try:
            min_ts = int(datetime.now(timezone.utc).timestamp()) - 60
            data = await p._client.get_json("/markets/trades", params={"min_ts": min_ts, "limit": 1000},
                                            use_cache=False)
            rows = (data or {}).get("trades") or []
            older = sum(1 for r in rows if r.get("created_time") and
                        datetime.fromisoformat(r["created_time"].replace("Z", "+00:00")).timestamp() < min_ts)
            return {"rows_limit_1000": len(rows), "rows_older_than_min_ts": older}
        finally:
            await p.aclose()

    _offday_guard("preflight")
    t0 = datetime.now(timezone.utc).timestamp()
    probe = asyncio.run(live_probe())
    _log_offday("preflight", t0, datetime.now(timezone.utc).timestamp(), 1)
    body = {
        "shakedown_dir": str(shakedown_dir), "minutes_in_window": round(minutes, 1),
        "tape_limit": 1000 if probe["rows_limit_1000"] > 200 else 200,
        "min_ts_honoured": probe["rows_older_than_min_ts"] == 0,
        "tape_mode": "TAPE_PT" if (p95_cost > 1.5 or mismatches > 0) else "global",
        "book_cadence_s": 2.0 if rate_limited > 0 else 1.0,
        "evidence": {"polls": len(polls), "p95_pages_per_poll": p95_pages, "p95_cost_req_per_s": p95_cost,
                     "page_cap_hits": cap_hits, "incomplete_polls": incomplete, "audit_mismatches": mismatches,
                     "rate_limited": rate_limited, **probe},
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    if minutes < 60:
        body["warning"] = "shakedown shorter than the 60 minutes G5 requires"
    console.print_json(json.dumps(body))
    if write:
        (FORWARD / "lip-cadence.json").write_text(json.dumps(body, indent=2) + "\n")
        console.print(f"wrote {FORWARD / 'lip-cadence.json'}")


@app.command("replay-day")
def replay_day_cmd(
    day_dir: Path = typer.Argument(..., help="A recorded day directory."),
    health_only: bool = typer.Option(False, "--health-only", help="Counts and exceptions only; no dollars."),
    workers: int = typer.Option(4, help="Parallel processes across markets."),
) -> None:
    """Replay every admitted market-day through all 24 paths.

    Before the final analysis only --health-only may be run on study days (section 7):
    it prints counts and exceptions and no credit, fill or dollar figure. Results are
    written next to the day; nothing is printed that could steer anyone.
    """
    from pmvl_markets.lip.replay import replay_day, write_results

    study = json.loads((day_dir / "study.json").read_text())
    root = day_dir.parent
    if health_only:
        console.print_json(json.dumps(day_health(day_dir)))
        return
    if study.get("tag") == "study":
        verdict = _verdict_date()
        if verdict is None or datetime.now(timezone.utc).date() < verdict or not (FORWARD / "lip-final.flag").exists():
            console.print(f"[red]refused: study days are replayed for dollars only at the final analysis, "
                          f"on or after the verdict date ({verdict}), with artifacts/forward/lip-final.flag")
            raise typer.Exit(5)
    results = replay_day(day_dir, settlements_path=FORWARD / (
        "lip-shakedown-settlements.jsonl" if study.get("tag") == "shakedown" else "lip-settlements.jsonl"),
        state_path=root / "admission_state.json", workers=workers)
    out = day_dir / "replay.json"
    write_results(results, out)
    console.print(f"{len(results)} market-days replayed -> {out}")


def _study_days() -> list[Path]:
    root = FORWARD / "lip-daily"
    return sorted(d for d in root.glob("20*") if d.is_dir() and (d / "study.json").exists())


@app.command("programs-paid")
def programs_paid() -> None:
    """Record, read-only, which admitted programs Kalshi has since paid or closed.

    Appends every paid_out / closed listing row for a program admitted on any study day
    to artifacts/forward/lip-programs-paid.jsonl. Run at the verdict date (last study
    day + 14 days), before the final analysis.
    """
    from pmvl_markets.lip.http import lane_provider
    from pmvl_markets.lip.store import read_stream
    from pmvl_markets.recording.writer import AppendOnlyLog

    _offday_guard("programs-paid")
    admitted = {b["program_id"] for d in _study_days() for b in read_stream(d, "universe")
                if b.get("decision") == "admitted"}
    t0 = datetime.now(timezone.utc).timestamp()

    async def fetch() -> list[dict]:
        p = lane_provider("control", 0.3, attempts=2)
        rows: list[dict] = []
        try:
            for status in ("paid_out", "closed"):
                cursor = None
                while True:
                    params = {"status": status, "type": "liquidity", "limit": 10000}
                    if cursor:
                        params["cursor"] = cursor
                    data = await p._client.get_json("/incentive_programs", params=params, use_cache=False)
                    batch = (data or {}).get("incentive_programs") or []
                    rows.extend(r for r in batch if r.get("id") in admitted)
                    cursor = (data or {}).get("next_cursor") or (data or {}).get("cursor")
                    if not cursor or not batch:
                        break
        finally:
            await p.aclose()
        return rows

    rows = asyncio.run(fetch())
    _log_offday("programs-paid", t0, datetime.now(timezone.utc).timestamp(), 0)
    log = AppendOnlyLog(FORWARD / "lip-programs-paid.jsonl")
    at = datetime.now(timezone.utc).isoformat()
    for r in rows:
        log.append({"fetched_at": at, "row": r})
    console.print(f"{len(admitted)} admitted programs; {len(rows)} paid_out/closed rows appended")


def _calendar():
    """(day 1, last day) from lip-gates.json: day 1 through the 20th counted day or day 1
    + 29, whichever comes first (section 7). None before the gates are recorded."""
    from datetime import date, timedelta

    from pmvl_markets.lip.sim_params import SIM

    gates = FORWARD / "lip-gates.json"
    if not gates.exists():
        return None
    day1 = date.fromisoformat(json.loads(gates.read_text())["day1"])
    last = day1 + timedelta(days=SIM.max_calendar_days - 1)
    counted, d = 0, day1
    while d <= last:
        health = FORWARD / "lip-daily" / d.isoformat() / "health.json"
        if health.exists() and json.loads(health.read_text()).get("counted_day"):
            counted += 1
            if counted == SIM.min_counted_days:
                return day1, d
        d += timedelta(days=1)
    return day1, last


def _verdict_date():
    from datetime import timedelta

    from pmvl_markets.lip.sim_params import SIM

    cal = _calendar()
    return None if cal is None else cal[1] + timedelta(days=SIM.verdict_lag_days)


@app.command("study")
def study_cmd(final: bool = typer.Option(False, "--final", help="The one final look, after the verdict date.")) -> None:
    """Pool the pre-registered calendar's days into cells and evaluate the decision table - once.

    Every calendar day from day 1 enters: a day that was never recorded, or was
    skipped, adds its live pairs' admitted hours at $0; a recorded day without its
    replay stops the analysis rather than leaving quietly. Without --final, or before
    the verdict date, it prints INTERIM_NOT_A_DECISION and no dollar figure.
    """
    from datetime import timedelta

    from pmvl_markets.lip.final import isolation_breaches, payment_status
    from pmvl_markets.lip.replay import build_cells, skipped_day_results
    from pmvl_markets.lip.study import decide

    cal = _calendar()
    if cal is None:
        console.print_json(json.dumps({"verdict": "INTERIM_NOT_A_DECISION", "why": "no lip-gates.json: day 1 not set"}))
        return
    day1, last = cal
    verdict_date = _verdict_date()
    is_final = final and datetime.now(timezone.utc).date() >= verdict_date
    state = FORWARD / "lip-daily" / "admission_state.json"
    results, versions, counted, breaches, problems, recorded = [], set(), 0, 0, [], []
    d = day1
    while d <= last:
        day_dir = FORWARD / "lip-daily" / d.isoformat()
        study_file = day_dir / "study.json"
        if not study_file.exists():
            # never recorded (the machine slept, cron never fired): zeros for its live pairs
            w = day_window(datetime(d.year, d.month, d.day, 12, 30, tzinfo=timezone.utc))
            stub = {"day": d.isoformat(), "window": {"window_start": w.window_start.isoformat(),
                                                     "window_end": w.window_end.isoformat()}}
            results.extend(skipped_day_results(stub, state))
            d += timedelta(days=1)
            continue
        study = json.loads(study_file.read_text())
        if study.get("tag") != "study" or study.get("recorder_version") != "lip-1.0.0":
            problems.append(f"{d}: tag {study.get('tag')!r} / recorder {study.get('recorder_version')!r}")
        versions.add(f"recorder:{study.get('recorder_version')}")
        health_file = day_dir / "health.json"
        if health_file.exists() and json.loads(health_file.read_text()).get("counted_day"):
            counted += 1
        if study.get("skipped"):
            results.extend(skipped_day_results(study, state))
        else:
            recorded.append(day_dir)
            breaches += len(isolation_breaches(day_dir, FORWARD / "maker-daily",
                                               FORWARD / "lip-offday-requests.jsonl"))
            replay_file = day_dir / "replay.json"
            if replay_file.exists():
                body = json.loads(replay_file.read_text())
                versions.add(f"sim:{body.get('sim_version')}")
                results.extend(body.get("results") or [])
            elif is_final:
                problems.append(f"{d}: recorded but not replayed")
        d += timedelta(days=1)
    if not is_final:
        console.print_json(json.dumps({"verdict": "INTERIM_NOT_A_DECISION", "calendar": [str(day1), str(last)],
                                       "counted_days": counted, "isolation_breaches": breaches,
                                       "verdict_date": verdict_date.isoformat()}))
        return
    if problems:
        console.print("[red]refused - the calendar is incomplete:\n  " + "\n  ".join(problems))
        raise typer.Exit(7)
    verified, withdrawn = payment_status(recorded, FORWARD / "lip-programs-paid.jsonl")
    cells = build_cells(results, verified=verified, withdrawn=withdrawn)
    # the precondition wants exactly one recorder and one sim version across the study:
    # anything else is handed to decide() as more than one version, and it refuses
    recorders = {v for v in versions if v.startswith("recorder:")}
    sims = {v for v in versions if v.startswith("sim:")}
    one = len(recorders) == 1 and len(sims) == 1
    v = decide(cells, policies=["J1", "I1", "J5", "I5"], counted_days=counted, final=True,
               versions={"|".join(sorted(versions))} if one else (versions or {"none", "missing"}),
               gates_closed=True, isolation_ok=breaches == 0)
    out = FORWARD / "lip-verdict.json"
    out.write_text(json.dumps(v.as_dict(), indent=2, default=str) + "\n")
    console.print(f"[bold]{v.verdict}[/bold]: {v.why}\nwrote {out}")


@app.command("health")
def health(day_dir: Path = typer.Argument(..., help="A recorded day directory.")) -> None:
    """Coverage and lane health only. No credit, fills or dollars - ever."""
    console.print_json(json.dumps(day_health(day_dir)))


if __name__ == "__main__":
    app()
