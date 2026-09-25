"""What the final analysis needs beyond the replay: isolation, payment, and the pooling.

**Isolation** (pre-registration 8). The LIP recorder and the maker study must never
share a moment of the read budget: any LIP request within 60 s of a maker session is a
breach, and a breach stops the study.

**Payment** (1, step 12). LOW counts a program's payout only if the program was later
seen ``paid_out`` with the terms it was first seen under; a program that vanished
from the active list before its end and was never paid pays nothing in either
envelope. Both need listings read *after* the study days, which is why this is here and
not in the replay.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..recording.writer import read_log
from .store import read_stream

__all__ = ["isolation_breaches", "maker_sessions", "payment_status"]

ISOLATION_S = 60.0


def _ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def maker_sessions(maker_root: Path) -> list[tuple[float, float]]:
    """Every maker-study session as [start - 60 s, end + 60 s], across all recorded days."""
    out: list[tuple[float, float]] = []
    for manifest in sorted(Path(maker_root).glob("*/study.json")):
        body = json.loads(manifest.read_text())
        for s in body.get("sessions") or []:
            if s.get("started_at") and s.get("ended_at"):
                out.append((_ts(s["started_at"]) - ISOLATION_S, _ts(s["ended_at"]) + ISOLATION_S))
    return out


def _request_times(day_dir: Path) -> Iterable[float]:
    for body in read_stream(day_dir, "books"):
        yield float(body["sent_at"])
    for body in read_stream(day_dir, "tape_polls"):
        yield float(body["sent_at"])
    for body in read_stream(day_dir, "status"):
        yield float(body["at"])


def isolation_breaches(day_dir: Path, maker_root: Path) -> list[float]:
    """LIP request times that fall inside any maker session's +-60 s envelope."""
    spans = sorted(maker_sessions(maker_root))
    if not spans:
        return []
    breaches = []
    for t in _request_times(day_dir):
        for a, b in spans:
            if a <= t <= b:
                breaches.append(t)
                break
    return breaches


def payment_status(day_dirs: list[Path], paid_log: Path | None) -> tuple[set[str], set[str]]:
    """(verified, withdrawn) program ids.

    Terms are compared with the first time each program was seen on any study day; a
    program paid under different terms is not verified.
    """
    first_terms: dict[str, tuple] = {}
    withdrawn_seen: set[str] = set()
    for day in day_dirs:
        for body in read_stream(day, "programs"):
            for row in body.get("rows") or []:
                first_terms.setdefault(str(row["id"]), _terms(row))
        registry_file = day / "registry.json"
        if registry_file.exists():
            withdrawn_seen.update((json.loads(registry_file.read_text()).get("withdrawn_at") or {}).keys())
    verified: set[str] = set()
    paid: set[str] = set()
    if paid_log is not None and Path(paid_log).exists():
        for entry in read_log(Path(paid_log)):
            row: dict[str, Any] = entry.body.get("row") or {}
            pid = str(row.get("id", ""))
            if not pid or not row.get("paid_out"):
                continue
            paid.add(pid)
            if first_terms.get(pid) == _terms(row):
                verified.add(pid)
    return verified, withdrawn_seen - paid


def _terms(row: dict[str, Any]) -> tuple:
    return (row.get("period_reward"), str(row.get("target_size_fp")), row.get("discount_factor_bps"),
            row.get("max_reward_per_account"))
