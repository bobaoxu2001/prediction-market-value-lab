"""One machine-readable account of every scheduled study day, and of days that never ran.

``build_manifest`` answers, from the day's own files and the cron wrapper's run records:
did the run start, when, did it stay alive, did the machine sleep, which lanes failed and
why, how long the gaps were and whether they recovered, whether any request burst
followed a recovery, whether the tape stayed continuous, whether rows were duplicated or
lost, what code produced the data - and whether the day is counted under the
pre-registered rule (Amendment 2 item 6, Amendment 3.1). It reads files only, never the
network, and it is deterministic: the same files give the same manifest.

``run_status`` is the missed-run sentinel: a scheduled day that should have started but
left no trace is ``NEVER_LAUNCHED``, which the wrapper cannot report for itself.

Like ``health``, this module is outcome-blind: it cannot import the modules that compute
money, and a test checks that it does not.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..recording.writer import LogIntegrityError
from .health import day_health
from .params import RECORDING, RecordingParams
from .recorder import day_window
from .store import read_checked, stream_ends

__all__ = ["RUN_STATUSES", "build_manifest", "classify_error", "qualify", "run_records", "run_status"]

MANIFEST_VERSION = 1
STREAMS = ("books", "trades", "tape_polls", "status", "programs", "universe", "markets", "series",
           "lanes", "audit", "clock")
#: the sentinel's answers, one per scheduled day
RUN_STATUSES = ("NOT_YET_DUE", "RUNNING", "COMPLETED", "INTENTIONAL_SKIP", "FAILED", "DIED", "NEVER_LAUNCHED")
#: a heartbeat older than this, with no terminal record, means the process is gone
STALE_S = 180.0


def _ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def classify_error(message: str) -> str:
    """One fault class per failed request, from the provider's own error text."""
    m = (message or "").lower()
    if not m:
        return "none"
    if "nodename nor servname" in m or "name or service not known" in m or "getaddrinfo" in m \
            or "temporary failure in name resolution" in m:
        return "dns"
    if "429" in m or "rate limited" in m:
        return "http_429"
    if "timed out" in m or "timeout" in m:
        return "timeout"
    if "non-json" in m or "malformed" in m or "decode" in m:
        return "malformed"
    if re.search(r"\b5\d\d on /", m):
        return "http_5xx"
    if re.search(r"\b4\d\d on /", m):
        return "http_4xx"
    if "connection" in m or "reset by peer" in m or "remoteprotocol" in m or "network is unreachable" in m \
            or "transport" in m:
        return "connection"
    return "other"


def run_records(runs_log: Path | None, day: str) -> list[dict[str, Any]]:
    """The cron wrapper's start and end records for one day, in the order written."""
    if runs_log is None or not Path(runs_log).exists():
        return []
    out = []
    for line in Path(runs_log).read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn last line from a killed wrapper
        if row.get("day") == day:
            out.append(row)
    return out


# ---------------------------------------------------------------------------------- sentinel
def run_status(day: date, now: datetime, root: Path, *, runs_log: Path | None = None,
               params: RecordingParams = RECORDING, stale_s: float = STALE_S) -> dict[str, Any]:
    """Did the day's scheduled run happen? Reads files only; never creates a day.

    status: NOT_YET_DUE before 12:30 UTC with no trace; NEVER_LAUNCHED after it with none;
    RUNNING while a start has a fresh heartbeat (or a start younger than ``stale_s`` and
    no heartbeat yet); DIED when a start has neither a terminal record nor a fresh
    heartbeat; FAILED / INTENTIONAL_SKIP / COMPLETED from the terminal record or the day's
    own files. launch: ON_TIME if it started before 12:30 UTC, LATE after, NONE if never.
    """
    window = day_window(datetime(day.year, day.month, day.day, 12, 30, tzinfo=timezone.utc), params)
    day_dir = Path(root) / day.isoformat()
    runs = run_records(runs_log if runs_log is not None else Path(root) / "runs.jsonl", day.isoformat())
    starts = [r for r in runs if r.get("event") == "start"]
    ends = [r for r in runs if r.get("event") == "end"]
    study = _read_json(day_dir / "study.json")
    health = _read_json(day_dir / "health.json")
    beat = _read_json(day_dir / "heartbeat.json")
    started = sorted([_ts(r["started_at"]) for r in starts if r.get("started_at")]
                     + ([_ts(study["started_at"])] if study and study.get("started_at") else []))
    started_at = started[0] if started else (float(beat["at_ts"]) if beat else None)
    t = now.timestamp()
    if started_at is None:
        launch = "NONE"
    else:
        launch = "LATE" if started_at > window.window_start.timestamp() else "ON_TIME"
    last_end = ends[-1] if ends else None
    if (study and study.get("skipped")) or (last_end and last_end.get("status") == "INTENTIONAL_SKIP"):
        status = "INTENTIONAL_SKIP"
    elif health and not health.get("skipped") and "streams" in health:
        status = "COMPLETED"
    elif last_end and last_end.get("status") == "SUCCESS":
        status = "COMPLETED"
    elif last_end is not None:
        status = "FAILED"
    elif started_at is not None:
        fresh = beat is not None and beat.get("state") not in ("closed", "aborted", "skipped") \
            and t - float(beat.get("at_ts", 0)) <= stale_s
        young = beat is None and t - started_at <= stale_s
        status = "RUNNING" if (fresh or young) else "DIED"
    else:
        status = "NOT_YET_DUE" if t < window.window_start.timestamp() else "NEVER_LAUNCHED"
    return {
        "day": day.isoformat(), "status": status, "launch": launch,
        "expected_start": window.admission_from.isoformat(),
        "started_at": None if started_at is None else datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "startup_delay_s": None if started_at is None else round(started_at - window.admission_from.timestamp(), 3),
        "terminal": None if last_end is None else {k: last_end.get(k) for k in ("status", "reason", "exit_code",
                                                                                 "ended_at")},
        "starts": len(starts), "heartbeat_state": beat.get("state") if beat else None,
        "evaluated_at": now.isoformat(),
    }


def record_final_statuses(root: Path, day1: date, now: datetime, *, params: RecordingParams = RECORDING,
                          ledger: Path | None = None) -> list[dict[str, Any]]:
    """For every day from day 1 whose window has fully passed, append its final status to
    the ledger once. Never creates a day directory and never re-runs anything."""
    ledger = ledger or Path(root) / "run-status.jsonl"
    seen = set()
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            seen.add((row.get("day"), row.get("status")))
    written = []
    d = day1
    while True:
        window = day_window(datetime(d.year, d.month, d.day, 12, 30, tzinfo=timezone.utc), params)
        if now < window.hard_stop:
            break
        row = run_status(d, now, root, params=params)
        if (row["day"], row["status"]) not in seen:
            with ledger.open("a") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            written.append(row)
        d += timedelta(days=1)
    return written


# ---------------------------------------------------------------------------------- qualification
def qualify(day_dir: Path, params: RecordingParams = RECORDING) -> dict[str, Any]:
    """The pre-registered day status, and nothing else: a day counts if successful book
    reads cover at least 80% of the window's ticks at the recorded cadence (Amendment 2
    item 6; ticks as Amendment 3.1). The figure is the recorder's own (health.json); for a
    day whose recorder died it is the outcome-blind health of the verified prefix
    (Amendment 3.3). There is no third category: a counted day with gaps is COUNTED."""
    day_dir = Path(day_dir)
    rule = (f"COUNTED if ok_ticks >= {params.counted_day_share:.2f} x window_ticks at the recorded "
            "book cadence (pre-registration, Amendment 2 item 6 and Amendment 3.1); shakedowns never count")
    study = _read_json(day_dir / "study.json")
    if study is None:
        return {"status": "NOT_RECORDED", "counted_day": False, "source": None, "rule": rule}
    if study.get("skipped"):
        return {"status": "SKIPPED", "counted_day": False, "source": "study.json", "reason": study["skipped"],
                "rule": rule}
    health = _read_json(day_dir / "health.json")
    if health is not None and "streams" in health:
        counted = bool(health.get("counted_day"))
        return {"status": "COUNTED" if counted else "NOT_COUNTED", "counted_day": counted,
                "source": "health.json", "tag": study.get("tag"), "ok_ticks": health.get("ok_ticks"),
                "window_ticks": health.get("window_ticks"),
                "share": health.get("book_success_share_of_window"),
                "threshold": params.counted_day_share, "rule": rule}
    try:
        h = day_health(day_dir, params)
    except (LogIntegrityError, OSError, ValueError) as exc:
        return {"status": "NOT_COUNTED", "counted_day": False, "source": "unreadable", "error": str(exc)[:300],
                "rule": rule}
    counted = bool(h.get("counted_day"))
    return {"status": "COUNTED" if counted else "NOT_COUNTED", "counted_day": counted,
            "source": "checkpoint (the recorder died)", "tag": study.get("tag"),
            "share": h.get("book_success_share_of_window"), "threshold": params.counted_day_share,
            "verified_until": h.get("verified_until"),
            "rule_inputs_missing": bool(h.get("rule_inputs_missing")), "rule": rule}


# ---------------------------------------------------------------------------------- manifest
def _gaps(runs: list[list[int]], cadence: float, due_end: int | None) -> dict[str, Any]:
    lengths = [(b - a + 1) * cadence for a, b in runs]
    unrecovered = [r for r in runs if due_end is not None and r[1] >= due_end]
    return {"count": len(runs), "total_seconds": sum(lengths), "largest_seconds": max(lengths, default=0.0),
            "recovered": len(runs) - len(unrecovered),
            "unrecovered_seconds": sum((b - a + 1) * cadence for a, b in unrecovered),
            "runs": runs[:200], "runs_truncated": len(runs) > 200}


def build_manifest(day_dir: Path, *, runs_log: Path | None = None, freeze_path: Path | None = None,
                   params: RecordingParams = RECORDING) -> dict[str, Any]:
    day_dir = Path(day_dir)
    study = _read_json(day_dir / "study.json") or {}
    health = _read_json(day_dir / "health.json") or {}
    beat = _read_json(day_dir / "heartbeat.json")
    day = study.get("day") or day_dir.name
    d = date.fromisoformat(day)
    window = day_window(datetime(d.year, d.month, d.day, 12, 30, tzinfo=timezone.utc), params)
    runs = run_records(runs_log if runs_log is not None else day_dir.parent / "runs.jsonl", day)
    cadence = float(study.get("book_cadence_s") or params.book_cadence_s)
    out: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION, "study": "lip-shadow", "day": day,
        "identity": {
            "tag": study.get("tag"), "recorder_version": study.get("recorder_version"),
            "sim_version": study.get("sim_version"), "run_id": study.get("run_id"),
            "prereg_commit": study.get("prereg_commit"), "code_commit": study.get("code_commit"),
            "cadence_sha256": study.get("cadence_sha256"), "freeze_sha256": study.get("freeze_sha256"),
            "freeze_manifest_sha256_now": _sha256(freeze_path) if freeze_path else None,
            "study_json_sha256": _sha256(day_dir / "study.json"),
            "book_cadence_s": cadence, "tape_mode": study.get("tape_mode"),
        },
        "expected": {k: getattr(window, k).isoformat() for k in
                     ("admission_from", "window_start", "window_end", "drain_at", "hard_stop")}
                    | {"expected_duration_s": (window.drain_at - window.admission_from).total_seconds()},
    }
    starts = [r for r in runs if r.get("event") == "start"]
    ends = [r for r in runs if r.get("event") == "end"]
    started_at = study.get("started_at") or (starts[0].get("started_at") if starts else None)
    ended_at = health.get("finished_at") or (ends[-1].get("ended_at") if ends else None)
    termination = ("not_recorded" if not study else "skipped" if study.get("skipped")
                   else "closed" if "streams" in health else "crashed" if (day_dir / "ends.json").exists()
                   else "unknown")
    out["lifecycle"] = {
        "wrapper_runs": runs, "restarts": max(0, len(starts) - 1),
        "started_at": started_at, "ended_at": ended_at,
        "startup_delay_s": None if not started_at else round(_ts(started_at) - window.admission_from.timestamp(), 3),
        "alive_seconds": None if not (started_at and ended_at) else round(_ts(ended_at) - _ts(started_at), 3),
        "exit_code": ends[-1].get("exit_code") if ends else None,
        "run_status": ends[-1].get("status") if ends else None,
        "termination": termination,
        "last_heartbeat": None if beat is None else {k: beat.get(k) for k in ("state", "at", "pid")},
    }
    out["qualification"] = qualify(day_dir, params)
    if not study or study.get("skipped"):
        return out

    # ---- integrity: every stream verified against the recorded ends, or the checkpoint
    try:
        ends_rec = stream_ends(day_dir)
    except LogIntegrityError as exc:
        out["integrity"] = {"verified": False, "error": str(exc)[:300]}
        return out
    bodies: dict[str, list[dict[str, Any]]] = {}
    error = ""
    for name in STREAMS:
        try:
            bodies[name] = list(read_checked(day_dir, name, ends_rec))
        except LogIntegrityError as exc:
            error = error or f"{name}: {str(exc)[:250]}"
            bodies[name] = []
    streams = {k: {"rows": v[0], "digest": v[1]} for k, v in sorted(ends_rec.items())}
    books, polls, lanes = bodies["books"], bodies["tape_polls"], bodies["lanes"]
    ids = [p.get("trade_id") for b in bodies["trades"] for p in b.get("prints") or []]
    dup_ids = sum(n - 1 for n in Counter(ids).values() if n > 1)
    dup_reads = sum(n - 1 for n in Counter((b.get("lane"), b.get("sent_at")) for b in books).values() if n > 1)
    per_minute: dict[str, int] = {}
    for body in lanes:
        if body.get("kind") == "requests":
            per_minute[body["lane"]] = max(per_minute.get(body["lane"], 0), int(body["n"]))
    rates = {"tape": params.tape_rate, "status": params.status_rate, "control": params.control_rate}
    burst = {lane: n for lane, n in per_minute.items()
             if n > rates.get(lane, params.book_lane_rate) * 60 + 1}
    complete = [p for p in polls if p.get("complete") and not p.get("ticker")]
    breaks = sum(1 for a, b in zip(complete, complete[1:]) if float(b["min_ts"]) > float(a["sent_at"]))
    out["integrity"] = {
        "verified": not error, "error": error,
        "read_to": "checkpoint" if ends_rec.prefix else "health.json", "verified_until": ends_rec.at,
        "streams": streams,
        "data_digest": hashlib.sha256(json.dumps(streams, sort_keys=True).encode()).hexdigest(),
        "duplicate_trade_ids": dup_ids, "duplicate_book_reads": dup_reads,
        "first_book_sent_at": min((b["sent_at"] for b in books), default=None),
        "last_book_sent_at": max((b["sent_at"] for b in books), default=None),
        "tape_first_min_ts": complete[0]["min_ts"] if complete else None,
        "tape_last_newest": max((p.get("newest") or 0 for p in complete), default=None),
        "tape_continuity_breaks": breaks,
        "max_requests_per_minute": per_minute, "burst_lanes": burst,
    }

    # ---- coverage: the recorder's own tick figures, and the gap runs it wrote
    lo, hi = window.window_start.timestamp(), window.window_end.timestamp()
    n_ticks = int((hi - lo) / cadence)
    ok_by_lane: dict[str, set[int]] = {}
    for b in books:  # only window ticks count, as in the recorder; pre-window reads are negative
        if b.get("ok") and b.get("tick") is not None and 0 <= int(b["tick"]) < n_ticks:
            ok_by_lane.setdefault(b["lane"], set()).add(int(b["tick"]))
    gap_runs = health.get("book_gaps")
    due_end = None
    if gap_runs is not None and health.get("due_ticks"):
        due_end = max([r[1] for r in gap_runs] + [max((max(s) for s in ok_by_lane.values() if s), default=0)])
    spans = sorted((max(lo, float(p["min_ts"])), min(hi, float(p["sent_at"]))) for p in complete)
    merged: list[list[float]] = []
    for a, b in spans:
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out["coverage"] = {
        "window_ticks": health.get("window_ticks"), "due_ticks": health.get("due_ticks"),
        "ok_ticks": health.get("ok_ticks"), "book_success_share": health.get("book_success_share_of_window"),
        "ok_ticks_by_lane_from_books": {k: len(v) for k, v in sorted(ok_by_lane.items())},
        "ok_ticks_by_lane_recorded": {k: v.get("ok_ticks_in_window") for k, v in
                                      sorted((health.get("lanes") or {}).items()) if k.startswith("book")},
        "book_gaps": None if gap_runs is None else _gaps(gap_runs, cadence, due_end),
        "tape": {"polls": len([p for p in polls if not p.get("ticker")]), "complete": len(complete),
                 "incomplete": sum(1 for p in polls if not p.get("complete")),
                 "pages": sum(int(p.get("pages", 0)) for p in polls),
                 "page_cap_hits": sum(1 for p in polls if p.get("page_cap_hit")),
                 "covered_seconds_in_window": round(sum(b - a for a, b in merged), 3),
                 "window_seconds": hi - lo},
    }

    # ---- faults, by lane and class, from each failed request's own error text
    faults: dict[str, Counter] = {"books": Counter(), "tape": Counter(), "status": Counter(), "control": Counter()}
    for b in books:
        if not b.get("ok"):
            faults["books"][classify_error(str(b.get("detail")))] += 1
    for p in polls:
        if p.get("error"):
            faults["tape"][classify_error(p["error"])] += 1
    for s in bodies["status"]:
        for ch in s.get("chunks") or []:
            if ch.get("error"):
                faults["status"][classify_error(ch["error"])] += 1
        if s.get("exchange_error"):
            faults["status"][classify_error(s["exchange_error"])] += 1
    unexpected = 0
    for body in lanes:
        if body.get("kind") in ("control_error", "final_error"):
            faults["control"][classify_error(body.get("error", ""))] += 1
            unexpected += 1
    jumps = [b for b in lanes if b.get("kind") == "wall_jump"]
    out["faults"] = {
        **{k: dict(sorted(v.items())) for k, v in faults.items()},
        "rate_limited": sum(1 for b in lanes if b.get("kind") == "rate_limited"),
        "control_or_final_errors": unexpected,
        "wall_jumps": [{k: j[k] for k in ("from", "to", "jump_s")} for j in jumps],
        "slept_seconds": round(sum(float(j["jump_s"]) for j in jumps), 3),
        "restarts": out["lifecycle"]["restarts"],
    }
    return out
