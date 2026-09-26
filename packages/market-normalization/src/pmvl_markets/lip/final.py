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
from .store import read_checked, stream_ends

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


def _request_spans(day_dir: Path, offday_log: Path | None) -> Iterable[tuple[float, float]]:
    """Every LIP request as a time span: the per-minute lane log covers every lane -
    books, tape, status and control - and the off-day log covers the commands run
    outside a recording day (programs-paid, preflight)."""
    ends = stream_ends(day_dir, required=False)
    for body in read_checked(day_dir, "lanes", ends):
        if body.get("kind") == "requests":
            yield float(body["first"]), float(body["last"])
    for body in read_checked(day_dir, "books", ends):
        yield float(body["sent_at"]), float(body["sent_at"])
    if offday_log is not None and Path(offday_log).exists():
        for entry in read_log(Path(offday_log)):
            yield float(entry.body["first"]), float(entry.body["last"])


def isolation_breaches(day_dir: Path, maker_root: Path, offday_log: Path | None = None) -> list[float]:
    """Start times of LIP request spans that overlap any maker session's +-60 s envelope."""
    spans = sorted(maker_sessions(maker_root))
    if not spans:
        return []
    breaches = []
    for lo, hi in _request_spans(day_dir, offday_log):
        for a, b in spans:
            if lo <= b and a <= hi:
                breaches.append(lo)
                break
    return breaches


def payment_status(day_dirs: list[Path], paid_log: Path | None) -> tuple[set[str], set[str]]:
    """(verified, withdrawn) program ids.

    Verified: later seen paid_out, with the same terms in *every* row ever recorded for
    it - a reward raised for a day and then restored is not verified (1, step 12).
    Withdrawn: present in a complete active listing, then absent from a later complete
    active listing before its end, and never paid. Complete listings are the ones the
    recorder marks ``full``; a diff listing cannot show an absence.
    """
    terms: dict[str, set[tuple]] = {}
    ends: dict[str, float] = {}
    listings: list[tuple[float, set[str]]] = []
    within: set[str] = set()
    for day in day_dirs:
        for body in read_checked(day, "programs", stream_ends(day, required=False)):
            for row in body.get("rows") or []:
                pid = str(row["id"])
                terms.setdefault(pid, set()).add(_terms(row))
                if row.get("end_date"):
                    ends[pid] = _ts(row["end_date"])
            if body.get("listing") == "active" and body.get("full"):
                listings.append((float(body["at"]), {str(r["id"]) for r in body.get("rows") or []}))
        registry_file = day / "registry.json"
        if registry_file.exists():  # the recorder's own within-day withdrawals
            within.update((json.loads(registry_file.read_text()).get("withdrawn_at") or {}).keys())
    paid: set[str] = set()
    verified: set[str] = set()
    if paid_log is not None and Path(paid_log).exists():
        for entry in read_log(Path(paid_log)):
            row: dict[str, Any] = entry.body.get("row") or {}
            pid = str(row.get("id", ""))
            if not pid or not row.get("paid_out"):
                continue
            paid.add(pid)
            seen = terms.get(pid, set()) | {_terms(row)}
            if len(seen) == 1:
                verified.add(pid)
    withdrawn: set[str] = set(within)
    listings.sort()
    for i, (_t, present) in enumerate(listings):
        for pid in present:
            for t2, later in listings[i + 1:]:
                if pid not in later and t2 < ends.get(pid, float("inf")):
                    withdrawn.add(pid)
                    break
    return verified, withdrawn - paid


def _terms(row: dict[str, Any]) -> tuple:
    return (row.get("period_reward"), str(row.get("target_size_fp")), row.get("discount_factor_bps"),
            row.get("max_reward_per_account"))
