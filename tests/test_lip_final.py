"""Isolation from the maker study, and which payouts the LOW envelope may count."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pmvl_markets.lip.final import isolation_breaches, payment_status
from pmvl_markets.lip.store import SegmentWriter
from pmvl_markets.recording.writer import AppendOnlyLog

UTC = timezone.utc
T = datetime(2026, 9, 25, 13, 0, tzinfo=UTC)


def lip_day(tmp: Path, *, request_at: datetime, rows: list[dict]) -> Path:
    day = tmp / "lip" / "2026-09-25"
    w = SegmentWriter(day, gzip_closed=False)
    w.append("books", {"sent_at": request_at.timestamp(), "received_at": request_at.timestamp() + 0.3}, at=request_at)
    w.append("programs", {"at": request_at.timestamp(), "rows": rows}, at=request_at)
    return day


def maker_day(tmp: Path, start: datetime, end: datetime) -> Path:
    root = tmp / "maker"
    (root / "2026-09-25").mkdir(parents=True)
    (root / "2026-09-25" / "study.json").write_text(json.dumps(
        {"sessions": [{"started_at": start.isoformat(), "ended_at": end.isoformat()}]}))
    return root


def test_a_lip_request_near_a_maker_session_is_a_breach(tmp_path):
    day = lip_day(tmp_path, request_at=T, rows=[])
    assert isolation_breaches(day, maker_day(tmp_path / "a", T - timedelta(minutes=30), T - timedelta(seconds=30)))
    assert not isolation_breaches(day, maker_day(tmp_path / "b", T - timedelta(hours=2), T - timedelta(minutes=5)))


def row(pid: str, reward: int, paid: bool) -> dict:
    return {"id": pid, "period_reward": reward, "target_size_fp": "1000.00", "discount_factor_bps": 5000,
            "max_reward_per_account": None, "paid_out": paid}


def test_only_programs_paid_under_their_first_terms_are_verified(tmp_path):
    day = lip_day(tmp_path, request_at=T, rows=[row("a", 1_000_000, False), row("b", 1_000_000, False)])
    log = AppendOnlyLog(tmp_path / "paid.jsonl")
    log.append({"row": row("a", 1_000_000, True)})
    log.append({"row": row("b", 500_000, True)})  # paid, but under changed terms
    verified, withdrawn = payment_status([day], tmp_path / "paid.jsonl")
    assert verified == {"a"} and withdrawn == set()


def test_without_the_paid_listing_nothing_is_verified(tmp_path):
    day = lip_day(tmp_path, request_at=T, rows=[row("a", 1_000_000, False)])
    (day / "registry.json").write_text(json.dumps({"withdrawn_at": {"a": T.timestamp()}}))
    verified, withdrawn = payment_status([day], None)
    assert verified == set() and withdrawn == {"a"}


def test_a_program_that_leaves_the_active_list_between_days_is_withdrawn(tmp_path):
    later = T + timedelta(hours=26)
    first = lip_day(tmp_path / "d1", request_at=T, rows=[])
    second = lip_day(tmp_path / "d2", request_at=later, rows=[])
    long_program = {**row("a", 1_000_000, False), "end_date": (T + timedelta(days=5)).isoformat()}
    SegmentWriter(first, gzip_closed=False).append(
        "programs", {"at": T.timestamp(), "listing": "active", "full": True, "rows": [long_program]}, at=T)
    SegmentWriter(second, gzip_closed=False).append(
        "programs", {"at": later.timestamp(), "listing": "active", "full": True, "rows": []}, at=later)
    verified, withdrawn = payment_status([first, second], None)
    assert withdrawn == {"a"}
