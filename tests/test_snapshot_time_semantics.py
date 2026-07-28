"""Quote age must be measured against the same clock as everything else.

`ageLabel` measures against the real clock. On a frozen snapshot that is wrong in a
way that worsens daily: resolution times were anchored to the capture instant while
quote age counted up against now(), so one page showed a quote "3d old" beside a
market resolving "in 2h" - two clocks, and no way to know which to trust.

These mirror the TypeScript helper's contract. The frontend has no test runner in
this repo, so the semantics are pinned here and the implementation is kept in step
by a source check at the end.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORMAT_TS = ROOT / "apps/web/lib/format.ts"


def age_relative_to_snapshot(observed_at, snapshot_at, *, now=None):  # noqa: ANN001
    """Reference implementation of the TypeScript helper."""
    if not observed_at:
        return "—"
    anchor = snapshot_at or now or datetime.now(timezone.utc)
    diff = anchor - observed_at
    seconds = diff.total_seconds()
    if seconds < 0:
        return "after snapshot" if snapshot_at else "just now"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = int(seconds // 3600)
    if hours < 48:
        return f"{hours}h"
    return f"{int(seconds // 86400)}d"


UTC = timezone.utc


class TestQuoteAgeSemantics:
    def test_quote_at_ten_snapshot_at_twelve_is_two_hours(self) -> None:
        observed = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
        snapshot = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        assert age_relative_to_snapshot(observed, snapshot) == "2h"

    def test_snapshot_mode_output_does_not_drift_with_real_time(self) -> None:
        """The whole point: a frozen deployment must not age its own data."""
        observed = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
        snapshot = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        first = age_relative_to_snapshot(observed, snapshot, now=snapshot)
        much_later = age_relative_to_snapshot(
            observed, snapshot, now=snapshot + timedelta(days=30)
        )
        assert first == much_later == "2h"

    def test_live_mode_uses_the_real_clock(self) -> None:
        observed = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
        now = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
        assert age_relative_to_snapshot(observed, None, now=now) == "5h"

    def test_missing_timestamp_is_neutral(self) -> None:
        assert age_relative_to_snapshot(None, datetime.now(UTC)) == "—"

    def test_future_quote_is_not_a_negative_age(self) -> None:
        """A quote after the anchor is a clock artefact, not '-3h old'."""
        observed = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
        snapshot = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        assert age_relative_to_snapshot(observed, snapshot) == "after snapshot"

    def test_sub_minute_and_day_boundaries(self) -> None:
        base = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        assert age_relative_to_snapshot(base - timedelta(seconds=30), base) == "<1m"
        assert age_relative_to_snapshot(base - timedelta(minutes=45), base) == "45m"
        assert age_relative_to_snapshot(base - timedelta(hours=47), base) == "47h"
        assert age_relative_to_snapshot(base - timedelta(days=3), base) == "3d"


class TestFrontendKeepsTheSameContract:
    """Guard against the TS helper drifting from the semantics pinned above."""

    def test_helper_exists_and_handles_the_edge_cases(self) -> None:
        source = FORMAT_TS.read_text()
        assert "export function ageRelativeToSnapshot(" in source
        # Anchors to the snapshot, falls back to now() only when none is given.
        assert "snapshotAt ? new Date(snapshotAt).getTime() : Date.now()" in source
        # Negative ages are named, not rendered as negative durations.
        assert "after snapshot" in source
        assert '"—"' in source

    def test_no_page_still_ages_a_quote_against_the_wall_clock(self) -> None:
        """ageLabel on a quote timestamp reintroduces the two-clock bug."""
        offenders = []
        for page in (ROOT / "apps/web/app").rglob("*.tsx"):
            text = page.read_text()
            if re.search(r"ageLabel\(\s*\w+\.quote_observed_at", text):
                offenders.append(str(page.relative_to(ROOT)))
        assert not offenders, f"quote age still uses the wall clock in: {offenders}"
