"""``python -m pmvl_worker.lip_cli`` - the LIP shadow study's recorder and replay.

Deliberately *not* registered in ``pmvl_worker.cli``: the maker study's daily cron
imports that module, and it must import exactly what it imported before this study
existed. Nothing here sends an order or reads a credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
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


def _freeze_sha() -> str:
    path = FORWARD / "lip-freeze.json"
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
        freeze_sha256=_freeze_sha(),
        run_id=os.environ.get("PMVL_LIP_RUN_ID") or f"{window.day.isoformat()}-{int(now.timestamp())}-{os.getpid()}",
    )
    try:
        health = run_until_signalled(recorder.run())
    except Signalled as exc:
        console.print(f"[red]stopped by signal {exc.signum}: checkpoint written, lock released")
        raise typer.Exit(128 + exc.signum) from exc
    except WindowRefused as exc:
        console.print(f"[red]refused: {exc}")
        raise typer.Exit(3) from exc
    except LockHeld as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(4) from exc
    console.print_json(json.dumps(health))
    if health.get("skipped"):
        raise typer.Exit(6)  # the cron script logs a skipped day like any other non-zero exit


class Signalled(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum}")
        self.signum = signum


def run_until_signalled(coro):
    """Run the recorder; SIGTERM, SIGHUP or SIGINT cancel it, so its finally-block still
    runs - the stream ends checkpointed, the segment closed, the lock released - and the
    process then exits 128 + the signal, which the cron wrapper records as SIGNALLED.
    SIGKILL cannot be caught: the last minute's checkpoint and the stale lock remain."""
    received: list[int] = []

    async def main():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def on_signal(signum: int) -> None:
            received.append(signum)
            task.cancel()

        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            loop.add_signal_handler(sig, on_signal, sig)
        return await coro

    try:
        return asyncio.run(main())
    except asyncio.CancelledError:
        if received:
            raise Signalled(received[0]) from None
        raise


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
    presentation = replay_presentation(day_dir)
    write_results(results, out, presentation=presentation)
    if presentation["not_performance_result"]:
        console.print(f"[bold yellow]{presentation['label']}[/bold yellow]: {presentation['why']}")
    console.print(f"{len(results)} market-days replayed -> {out}")


def replay_presentation(day_dir: Path) -> dict:
    """How a replay.json must be read. The replay charges every admitted hour of the window
    and counts unrecorded seconds as zeros (pre-registered), so a day recorded for part of
    its window - every shakedown - yields dollar figures that are not a performance result."""
    study = json.loads((day_dir / "study.json").read_text())
    health = json.loads((day_dir / "health.json").read_text()) if (day_dir / "health.json").exists() else {}
    lo = datetime.fromisoformat(study["window"]["window_start"]).timestamp()
    hi = datetime.fromisoformat(study["window"]["window_end"]).timestamp()
    start = datetime.fromisoformat(study["started_at"]).timestamp() if study.get("started_at") else lo
    end = (datetime.fromisoformat(health["finished_at"].replace("Z", "+00:00")).timestamp()
           if health.get("finished_at") else None)
    if end is None:
        from pmvl_markets.lip.store import read_checked, stream_ends

        sent = [b["sent_at"] for b in read_checked(day_dir, "books", stream_ends(day_dir, required=False))]
        end = max(sent) if sent else start
    recorded = max(0.0, min(end, hi) - max(start, lo))
    share = recorded / (hi - lo)
    study_day = study.get("tag") == "study"
    return {
        # a study day's unrecorded seconds are zeros by the pre-registered rule, so it is
        # evidence at any coverage - but only inside the one pooled final analysis
        "label": "STUDY_DAY_POOLED_ONLY" if study_day else "INCOMPLETE_COVERAGE_NOT_PERFORMANCE_RESULT",
        "not_performance_result": not study_day,
        "tag": study.get("tag"),
        "recorded_seconds_in_window": round(recorded, 1), "window_seconds": hi - lo,
        "recording_coverage_share": round(share, 4),
        "why": (f"{study.get('tag')} day recorded for {recorded / 60:.0f} of {(hi - lo) / 60:.0f} window minutes "
                f"({share:.1%}); the replay counts every admitted hour and treats unrecorded seconds as zero, "
                "so its dollar figures are a plumbing check, not evidence about the strategy"),
    }


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


def _day_status(day_dir: Path) -> dict:
    """health.json as the recorder closed the day - or, for a day whose recorder died,
    the outcome-blind health of what it verifiably recorded up to its last checkpoint."""
    health = day_dir / "health.json"
    if health.exists():
        return json.loads(health.read_text())
    if (day_dir / "study.json").exists() and (day_dir / "ends.json").exists():
        return day_health(day_dir)
    return {}


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
    counted, empty_run, d = 0, 0, day1
    while d <= last:
        day_dir = FORWARD / "lip-daily" / d.isoformat()
        body = _day_status(day_dir)
        if body.get("counted_day"):
            counted += 1
            if counted == SIM.min_counted_days:
                return day1, d
        # the exogenous stop: seven consecutive days with nothing admitted end the study
        empty_run = empty_run + 1 if body and not body.get("skipped") and body.get("due_ticks") == 0 else 0
        if empty_run >= 7:
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
        if not study_file.exists() and day_dir.exists() and (
                any(day_dir.glob("segment-*")) or (day_dir / "replay.json").exists()):
            problems.append(f"{d}: recordings but no study.json - the day cannot be identified")
            d += timedelta(days=1)
            continue
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
        if _day_status(day_dir).get("counted_day"):
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


def _day1_ahead(day1, now: datetime) -> bool:
    """Day 1's admission (12:25 UTC) is still ahead - a past date never qualifies,
    whatever the hour it is asked at."""
    from pmvl_markets.lip.params import RECORDING

    start = datetime.combine(day1, RECORDING.admission_from, tzinfo=timezone.utc)
    return now < start


@app.command("close-gates")
def close_gates(day1: str = typer.Option(..., help="Day 1, YYYY-MM-DD: the first 12:30 UTC window after this.")) -> None:
    """Record that every gate is closed and fix day 1 (section 8). Refuses unless the
    pre-flight cadence exists and the code is tagged lip-sim-1.0.0."""
    from datetime import date

    cadence = FORWARD / "lip-cadence.json"
    if not cadence.exists():
        console.print("[red]refused: G5 is open - no artifacts/forward/lip-cadence.json")
        raise typer.Exit(2)
    tag = subprocess.run(["git", "-C", str(REPO), "rev-list", "-n", "1", "lip-sim-1.0.0"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    if not tag:
        console.print("[red]refused: G4 is open - the code is not tagged lip-sim-1.0.0")
        raise typer.Exit(2)
    if not _day1_ahead(date.fromisoformat(day1), datetime.now(timezone.utc)):
        console.print("[red]refused: day 1 must be a window that has not started")
        raise typer.Exit(2)
    freeze = FORWARD / "lip-freeze.json"
    if not freeze.exists():
        console.print("[red]refused: no artifacts/forward/lip-freeze.json - run freeze-manifest --write first")
        raise typer.Exit(2)
    stored = json.loads(freeze.read_text())
    if stored["code"]["git"]["commit"] != tag:
        console.print(f"[red]refused: the freeze manifest is for {stored['code']['git']['commit'][:12]}, "
                      f"the tag lip-sim-1.0.0 is at {tag[:12]}")
        raise typer.Exit(2)
    drift = _freeze_drift(stored)
    if drift:
        console.print("[red]refused: files changed since the freeze manifest: " + ", ".join(drift[:10]))
        raise typer.Exit(2)
    gates = FORWARD / "lip-gates.json"
    if gates.exists():
        console.print(f"[red]refused: {gates} already exists - day 1 is fixed once")
        raise typer.Exit(2)
    body = {"day1": day1, "closed_at": datetime.now(timezone.utc).isoformat(),
            "prereg_commit": _prereg_commit(), "code_tag": "lip-sim-1.0.0", "code_commit": tag,
            "cadence_sha256": _cadence_sha(), "freeze_sha256": _freeze_sha(),
            "gates": {"G1": "Amendment 1", "G2": "Amendment 1", "G3": "user, 2026-09-24",
                      "G4": f"tag lip-sim-1.0.0 at {tag}", "G5": "lip-cadence.json", "G6": "cron line installed"}}
    gates.write_text(json.dumps(body, indent=2) + "\n")
    console.print_json(json.dumps(body))


@app.command("manifest")
def manifest_cmd(
    day_dir: Path = typer.Argument(..., help="A study day directory (lip-daily/YYYY-MM-DD)."),
    write: bool = typer.Option(True, help="Write manifest.json into the day directory."),
) -> None:
    """The day's run manifest: lifecycle, coverage, gaps, faults, integrity, provenance and
    the pre-registered day status. Local files only; no dollars. The cron wrapper runs it
    when the recorder exits; it can be re-run at any time and gives the same answer."""
    from pmvl_markets.lip.daymanifest import build_manifest

    m = build_manifest(day_dir, runs_log=day_dir.parent / "runs.jsonl", freeze_path=FORWARD / "lip-freeze.json")
    if write:
        _atomic_write(day_dir / "manifest.json", json.dumps(m, indent=1, sort_keys=True) + "\n")
    q, life = m["qualification"], m["lifecycle"]
    gaps = (m.get("coverage") or {}).get("book_gaps") or {}
    console.print_json(json.dumps({
        "day": m["day"], "status": q["status"], "termination": life["termination"],
        "run_status": life["run_status"], "exit_code": life["exit_code"], "restarts": life["restarts"],
        "book_success_share": (m.get("coverage") or {}).get("book_success_share"),
        "gaps": {k: gaps.get(k) for k in ("count", "total_seconds", "largest_seconds", "unrecovered_seconds")},
        "integrity_verified": (m.get("integrity") or {}).get("verified"),
        "duplicate_trade_ids": (m.get("integrity") or {}).get("duplicate_trade_ids"),
        "slept_seconds": (m.get("faults") or {}).get("slept_seconds"),
    }))


@app.command("run-status")
def run_status_cmd(
    day: str = typer.Option("", help="YYYY-MM-DD; default: the study day whose window contains now."),
    record: bool = typer.Option(False, help="Append the final status of every past day from day 1 to "
                                            "lip-daily/run-status.jsonl (once per day and status)."),
    now: str = typer.Option("", help="Evaluate as of this ISO time (for checking a past day)."),
) -> None:
    """The missed-run sentinel: did the scheduled run happen? Local files only. It never
    creates a day and never re-runs one - a missed day stays missed (section 8)."""
    from datetime import date as _date

    from pmvl_markets.lip.daymanifest import record_final_statuses, run_status

    t = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    root = FORWARD / "lip-daily"
    if record:
        gates = FORWARD / "lip-gates.json"
        if not gates.exists():
            console.print_json(json.dumps({"recorded": [], "why": "no lip-gates.json: day 1 not set"}))
            return
        day1 = _date.fromisoformat(json.loads(gates.read_text())["day1"])
        written = record_final_statuses(root, day1, t)
        console.print_json(json.dumps({"recorded": written}))
        return
    d = _date.fromisoformat(day) if day else day_window(t).day
    console.print_json(json.dumps(run_status(d, t, root)))


FREEZE_EXTRA = (PREREG, "docs/lip-runbook.md", "scripts/record_lip_day.sh", "requirements.txt",
                "requirements.lock.txt", "requirements-dev.txt", "packages/market-normalization/pyproject.toml",
                "packages/shared/pyproject.toml", "services/worker/pyproject.toml",
                "artifacts/research/lip-1/known_series.json", "artifacts/forward/lip-cadence.json", "pytest.ini")


def _sha_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _freeze_code() -> dict:
    """What produced the data: every repository module the recorder, the replay and this
    CLI load (their import closure), the documents and scripts that define the run, the
    parameters, the dependency locks and the installed environment."""
    import platform
    import sys
    from dataclasses import asdict

    import pmvl_markets.lip.daymanifest  # noqa: F401
    import pmvl_markets.lip.final  # noqa: F401
    import pmvl_markets.lip.replay  # noqa: F401
    from pmvl_markets.lip.params import RECORDING
    from pmvl_markets.lip.sim_params import SIM
    from pmvl_markets.lip.version import LIP_RECORDER_VERSION, LIP_SIM_VERSION, SAMPLE_SALT

    root = REPO.resolve()
    modules = {}
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        path = Path(f).resolve()
        if path.is_relative_to(root) and ".venv" not in path.parts and "tests" not in path.parts:
            modules[str(path.relative_to(root))] = _sha_file(path)
    files = {rel: _sha_file(REPO / rel) for rel in FREEZE_EXTRA}
    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze", "--exclude-editable"],
                            capture_output=True, text=True, timeout=120).stdout
    status = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--", "packages", "services",
                             "scripts", "tests", "docs", "pytest.ini", "requirements.txt",
                             "requirements.lock.txt", "requirements-dev.txt"],
                            capture_output=True, text=True, timeout=30).stdout.splitlines()
    config = {"recording": asdict(RECORDING), "sim": asdict(SIM)}
    config_json = json.dumps(config, sort_keys=True, default=str)
    return {
        "git": {"commit": _head_commit(), "dirty": sorted(status)},
        "versions": {"recorder": LIP_RECORDER_VERSION, "sim": LIP_SIM_VERSION, "sample_salt": SAMPLE_SALT},
        "modules": dict(sorted(modules.items())), "files": files,
        "config": json.loads(config_json), "config_sha256": hashlib.sha256(config_json.encode()).hexdigest(),
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "pip_freeze_sha256": hashlib.sha256(freeze.encode()).hexdigest(),
                        "packages": len(freeze.splitlines())},
    }


def _freeze_drift(stored: dict) -> list[str]:
    """Every path whose hash differs from the stored freeze manifest, and the config."""
    now = _freeze_code()
    drift = [k for k in sorted(set(stored["code"]["modules"]) | set(now["modules"]))
             if stored["code"]["modules"].get(k) != now["modules"].get(k)]
    drift += [k for k in sorted(set(stored["code"]["files"]) | set(now["files"]))
              if stored["code"]["files"].get(k) != now["files"].get(k)]
    for key in ("config_sha256", "versions", "environment", "git"):
        if stored["code"][key] != now[key]:
            drift.append(key)
    return drift


def _suite_result(log: Path | None) -> dict | None:
    import re

    if log is None:
        return None
    text = log.read_text()
    lines = [ln for ln in text.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    if not lines:
        return {"log_sha256": _sha_file(log), "parsed": False}
    last = lines[-1]
    counts = {k: int(m.group(1)) for k in ("passed", "failed", "skipped", "error", "errors", "xfailed")
              for m in [re.search(rf"(\d+) {k}\b", last)] if m}
    return {"log_sha256": _sha_file(log), "summary": last.strip(" ="), **counts,
            "green": counts.get("failed", 0) == 0 and counts.get("error", 0) == 0 and counts.get("errors", 0) == 0}


def _replay_validation(path: Path) -> dict | None:
    import math

    if not path.exists():
        return None
    body = json.loads(path.read_text())
    res = body.get("results") or []
    pairs = violations = nonfinite = 0
    for r in res:
        for k, v in r["paths"].items():
            nonfinite += sum(1 for f in ("MARK_1H", "DAY_EXIT", "accrued", "pay") if not math.isfinite(v[f]))
            if k.endswith("|LOW"):
                h = r["paths"][k[: -len("LOW")] + "HIGH"]
                pairs += 1
                violations += sum(1 for f in ("accrued", "pay", "MARK_1H", "DAY_EXIT") if v[f] > h[f] + 1e-9)
    return {"file": str(path.relative_to(REPO)), "sha256": _sha_file(path), "sim_version": body.get("sim_version"),
            "label": (body.get("presentation") or {}).get("label"), "market_days": len(res),
            "low_high_pairs": pairs, "low_above_high": violations, "non_finite": nonfinite,
            "passed": bool(res) and violations == 0 and nonfinite == 0}


@app.command("freeze-manifest")
def freeze_manifest_cmd(
    write: bool = typer.Option(False, help="Write artifacts/forward/lip-freeze.json."),
    suite_log: Path = typer.Option(None, help="The full test-suite log to attest (pytest output)."),
    check: bool = typer.Option(False, help="Compare the code, config and environment with the stored manifest."),
) -> None:
    """Bind the implementation the counted days will run: hashes of every module and file
    that defines the run, the parameters, the environment, and the validation results.
    Generated, never embedded in source; regenerating on unchanged files gives the same
    semantic_digest."""
    path = FORWARD / "lip-freeze.json"
    if check:
        if not path.exists():
            console.print("[red]no lip-freeze.json")
            raise typer.Exit(1)
        drift = _freeze_drift(json.loads(path.read_text()))
        console.print_json(json.dumps({"unchanged": not drift, "drift": drift}))
        raise typer.Exit(0 if not drift else 1)
    body = build_freeze(suite_log)
    if write:
        if body["code"]["git"]["dirty"]:
            console.print("[red]refused: tracked code or docs have uncommitted changes: "
                          + ", ".join(body["code"]["git"]["dirty"][:10]))
            raise typer.Exit(2)
        _atomic_write(path, json.dumps(body, indent=1, sort_keys=True) + "\n")
    console.print_json(json.dumps({k: body[k] for k in ("semantic_digest", "validation")} |
                                  {"commit": body["code"]["git"]["commit"], "dirty": body["code"]["git"]["dirty"],
                                   "modules": len(body["code"]["modules"])}))


def build_freeze(suite_log: Path | None) -> dict:
    semantic = {"freeze_version": 1, "study": "lip-shadow", "code": _freeze_code(),
                "validation": {"shakedown_replay": _replay_validation(
                                   FORWARD / "lip-shakedown" / "2026-09-25" / "replay.json"),
                               "suite": _suite_result(suite_log)}}
    digest = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
    return {**semantic, "semantic_digest": digest, "generated_at": datetime.now(timezone.utc).isoformat()}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@app.command("health")
def health(day_dir: Path = typer.Argument(..., help="A recorded day directory.")) -> None:
    """Coverage and lane health only. No credit, fills or dollars - ever."""
    console.print_json(json.dumps(day_health(day_dir)))


if __name__ == "__main__":
    app()
