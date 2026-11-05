"""Deterministic point-in-time selection.

The research invariant is that nothing after ``t`` may influence anything except
the settlement label. Enforcing it is not a matter of care at the call site - it
has to be structurally impossible to write the leaking query - so selection goes
through these functions and they take the cutoff as a required argument.

Two timestamps are easy to confuse and mean different things:

``published_at``
    when the source says it issued the forecast.
``available_at``
    when this system finished the poll that returned it, and could therefore
    have acted on it.

``available_at`` is the admissibility timestamp. ``published_at`` is strictly
earlier and describes the world, not our knowledge of it: a forecast issued at
14:00 and first retrieved at 14:07 was not actionable at 14:03, and using
``published_at`` would grant seven minutes of hindsight on every observation.
That gap is exactly the size of the effect a source-lag study would be looking
for, which is what makes the substitution so dangerous here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from pmvl_shared.timeutil import ensure_utc

from .exclusions import Exclusion

#: How old a forecast may be and still describe the information set at ``t``.
#: NWS gridpoint forecasts update roughly every 6 hours; 12 hours allows one
#: missed cycle without silently pairing a market with day-old information.
MAX_FORECAST_AGE = timedelta(hours=12)

#: How old an orderbook may be and still stand in for the market at ``t``.
#: Baseline capture is 180s, so 300s tolerates one missed cycle. Beyond that the
#: book is not evidence about ``t``.
MAX_ORDERBOOK_AGE = timedelta(seconds=300)


@dataclass(frozen=True)
class Selection:
    """What was chosen at a cutoff, and why - or why nothing was."""

    record: Any | None
    age_seconds: float | None
    reason: Exclusion | None
    #: Every candidate considered, for audit. A researcher asking "why this
    #: snapshot?" gets the full comparison, not just the winner.
    considered: int = 0

    @property
    def ok(self) -> bool:
        return self.record is not None and self.reason is None


def select_as_of(
    candidates: Sequence[Any],
    *,
    cutoff: datetime,
    timestamp_attr: str,
    max_age: timedelta,
    missing_reason: Exclusion,
    stale_reason: Exclusion,
) -> Selection:
    """The latest candidate whose timestamp is at or before ``cutoff``.

    Strictly ``<=``. A record stamped exactly at the cutoff was available at the
    cutoff; one stamped a microsecond later was not, and no tolerance is applied
    to close that gap - a tolerance is just leakage with a friendly name.

    Ties are broken toward the LAST element, so callers must pass candidates in a
    stable order. Two records sharing a timestamp are genuinely ambiguous and the
    resolution has to be deterministic rather than incidental.
    """
    cutoff = ensure_utc(cutoff)
    eligible: list[tuple[datetime, int, Any]] = []
    for index, candidate in enumerate(candidates):
        raw = getattr(candidate, timestamp_attr, None)
        if raw is None:
            # No usable timestamp is not "assume it was available". Fail closed.
            continue
        stamp = ensure_utc(raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw)))
        if stamp <= cutoff:
            eligible.append((stamp, index, candidate))

    if not eligible:
        return Selection(None, None, missing_reason, considered=len(candidates))

    stamp, _index, chosen = max(eligible, key=lambda item: (item[0], item[1]))
    age = (cutoff - stamp).total_seconds()
    if age > max_age.total_seconds():
        return Selection(None, age, stale_reason, considered=len(candidates))
    return Selection(chosen, age, None, considered=len(candidates))


def select_forecast_as_of(candidates: Sequence[Any], *, cutoff: datetime) -> Selection:
    """Latest forecast whose ``available_at`` is at or before ``cutoff``."""
    return select_as_of(
        candidates,
        cutoff=cutoff,
        timestamp_attr="available_at",
        max_age=MAX_FORECAST_AGE,
        missing_reason=Exclusion.NO_FORECAST_AVAILABLE,
        stale_reason=Exclusion.FORECAST_TOO_STALE,
    )


def select_orderbook_as_of(candidates: Sequence[Any], *, cutoff: datetime) -> Selection:
    """Latest archived book whose ``available_at`` is at or before ``cutoff``.

    Never interpolates and never reaches forward. A missing book at ``t`` is a
    missing observation, not a gap to be filled from ``t + delta`` - filling it
    would import the very price movement the research is trying to measure.
    """
    return select_as_of(
        candidates,
        cutoff=cutoff,
        timestamp_attr="available_at",
        max_age=MAX_ORDERBOOK_AGE,
        missing_reason=Exclusion.NO_ORDERBOOK,
        stale_reason=Exclusion.ORDERBOOK_TOO_STALE,
    )
