"""UTC-only time handling and resolution-horizon bucketing.

Every timestamp inside the system is timezone-aware UTC. Localisation happens in the
browser. Mixing naive and aware datetimes is the classic way to silently mis-bucket a
market that resolves "tomorrow" in one timezone and "today" in another, so parsing is
strict and centralised here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

UTC = timezone.utc

Horizon = Literal["24h", "7d", "30d"]
HORIZONS: tuple[Horizon, ...] = ("24h", "7d", "30d")
HORIZON_DELTAS: dict[Horizon, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime; convert an aware one.

    Naive datetimes reaching this function come from SQLite, which drops tzinfo on
    round-trip. Since we only ever *write* UTC, interpreting naive as UTC is correct.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_ts(value: str | int | float | datetime | None) -> datetime | None:
    """Parse the timestamp formats both venues emit.

    Handles ISO-8601 with ``Z``, fractional seconds of arbitrary length, and unix
    epochs in seconds or milliseconds.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, (int, float)):
        seconds = float(value)
        # Polymarket returns ms in some payloads (book timestamp) and s in others.
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC)

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_ts(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return ensure_utc(datetime.strptime(text, fmt))
            except ValueError:
                continue
    return None


def iso(value: datetime | None) -> str | None:
    v = ensure_utc(value)
    return v.isoformat().replace("+00:00", "Z") if v else None


def horizon_for(
    resolution_time: datetime | None, *, now: datetime | None = None
) -> Horizon | None:
    """Bucket a market by *expected resolution* time.

    Returns the tightest bucket the market fits in, so a 12-hour market appears in
    24h, 7d and 30d views via ``horizons_for``. Markets already past their expected
    resolution return ``None`` - they are pending settlement, not tradeable horizons.
    """
    resolution_time = ensure_utc(resolution_time)
    if resolution_time is None:
        return None
    now = now or utcnow()
    if resolution_time <= now:
        return None
    delta = resolution_time - now
    for name in HORIZONS:
        if delta <= HORIZON_DELTAS[name]:
            return name
    return None


def horizons_for(
    resolution_time: datetime | None, *, now: datetime | None = None
) -> tuple[Horizon, ...]:
    """All buckets a market qualifies for (a 24h market is also within 7d and 30d)."""
    tightest = horizon_for(resolution_time, now=now)
    if tightest is None:
        return ()
    start = HORIZONS.index(tightest)
    return HORIZONS[start:]


def hours_until(target: datetime | None, *, now: datetime | None = None) -> float | None:
    target = ensure_utc(target)
    if target is None:
        return None
    now = now or utcnow()
    return (target - now).total_seconds() / 3600.0


def years_until(target: datetime | None, *, now: datetime | None = None) -> float:
    """Year fraction used for capital-cost and volatility scaling. Never negative."""
    hours = hours_until(target, now=now)
    if hours is None or hours <= 0:
        return 0.0
    return hours / (24.0 * 365.25)


def age_seconds(observed_at: datetime | None, *, now: datetime | None = None) -> float | None:
    observed_at = ensure_utc(observed_at)
    if observed_at is None:
        return None
    now = now or utcnow()
    return max(0.0, (now - observed_at).total_seconds())


def earliest(values: Iterable[datetime | None]) -> datetime | None:
    present = [ensure_utc(v) for v in values if v is not None]
    return min(present) if present else None


def humanize_seconds(seconds: float) -> str:
    """Render an age as something a reader can judge at a glance.

    Risk flags reached the page reading "oldest quote is 179581s old". That is
    two days, but nobody converts six-figure second counts while scanning a
    table, so the flag failed at the one job it had: making staleness obvious.

    The unit is chosen so the number stays small, and sub-minute ages keep
    seconds because that is the range where seconds are the natural unit.
    """
    seconds = abs(float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h".replace(".0h", "h")
    return f"{hours / 24:.1f}d".replace(".0d", "d")
