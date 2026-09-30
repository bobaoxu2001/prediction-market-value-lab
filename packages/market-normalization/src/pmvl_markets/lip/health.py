"""Outcome-blind health of a recorded LIP day.

Interim reports carry health and coverage only - no credit, no fills, no dollars
(pre-registration 7). This module is how that is kept honest: it cannot import the
modules that compute money (scoring, fills, simulator, valuation), and a test checks
that it does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .params import RECORDING, RecordingParams
from .store import read_checked, stream_ends

__all__ = ["day_health"]


def day_health(day_dir: Path, params: RecordingParams = RECORDING) -> dict[str, Any]:
    day_dir = Path(day_dir)
    study = json.loads((day_dir / "study.json").read_text())
    recorded = json.loads((day_dir / "health.json").read_text()) if (day_dir / "health.json").exists() else {}
    from datetime import datetime

    def ts(s: str) -> float:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()

    lo, hi = ts(study["window"]["window_start"]), ts(study["window"]["window_end"])
    if study.get("skipped"):
        return {"label": "INTERIM_NOT_A_DECISION", "tag": study.get("tag"), "day": study.get("day"),
                "skipped": study["skipped"], "counted_day": False}
    ends = stream_ends(day_dir)
    lanes: dict[str, dict[str, int]] = {}
    for body in read_checked(day_dir, "books", ends):
        lane = lanes.setdefault(body.get("lane", ""), {"requests": 0, "ok": 0, "ok_in_window": 0})
        lane["requests"] += 1
        if body.get("ok"):
            lane["ok"] += 1
            if lo <= body["received_at"] < hi:
                lane["ok_in_window"] += 1
    polls = list(read_checked(day_dir, "tape_polls", ends))
    complete = sum(1 for p in polls if p.get("complete"))
    admitted = sum(1 for b in read_checked(day_dir, "universe", ends) if b.get("decision") == "admitted")
    rejected: dict[str, int] = {}
    for b in read_checked(day_dir, "universe", ends):
        if b.get("decision") == "rejected":
            rejected[b.get("reason", "")] = rejected.get(b.get("reason", ""), 0) + 1
    # the recorder counts due and read ticks at the day's own cadence; read its figure
    # when it wrote one, else approximate from the books at the recorded cadence
    cadence = float(study.get("book_cadence_s") or params.book_cadence_s)
    ticks = max(1, int((hi - lo) / cadence))
    if "ok_ticks" in recorded:
        share = float(recorded["ok_ticks"]) / ticks
    else:
        used = [v for v in lanes.values() if v["requests"]]
        share = min((v["ok_in_window"] for v in used), default=0) / ticks
    storage = sum(f.stat().st_size for f in day_dir.rglob("*") if f.is_file())
    return {
        "label": "INTERIM_NOT_A_DECISION",
        "tag": study.get("tag"),
        "day": study.get("day"),
        "admitted_pairs": admitted,
        "rejections": rejected,
        "book_lanes": lanes,
        "book_success_share_of_window": share,
        "counted_day": study.get("tag") == "study" and not study.get("skipped")
                       and share >= params.counted_day_share,
        "tape_polls": len(polls),
        "tape_polls_complete": complete,
        "rate_limited": {k: v.get("rate_limited", 0) for k, v in (recorded.get("lanes") or {}).items()},
        "storage_bytes": storage,
        #: the recorder died before closing the day: read to its last checkpoint only
        "crashed": bool(ends.prefix),
        "verified_until": ends.at,
    }
