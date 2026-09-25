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
    root = FORWARD / ("lip-shakedown" if shakedown else "lip-daily")
    out = root / window.day.isoformat()
    state = root / "admission_state.json"
    known = set(json.loads(KNOWN_SERIES.read_text())) if KNOWN_SERIES.exists() else set()
    recorder = LipRecorder(
        out_dir=out, state_path=state, settlements_path=FORWARD / (
            "lip-shakedown-settlements.jsonl" if shakedown else "lip-settlements.jsonl"),
        known_series=known, shakedown=shakedown,
        stop_at=datetime.fromisoformat(until) if until else None,
        prereg_commit=_prereg_commit(), cadence_sha256=_cadence_sha(),
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


@app.command("health")
def health(day_dir: Path = typer.Argument(..., help="A recorded day directory.")) -> None:
    """Coverage and lane health only. No credit, fills or dollars - ever."""
    console.print_json(json.dumps(day_health(day_dir)))


if __name__ == "__main__":
    app()
