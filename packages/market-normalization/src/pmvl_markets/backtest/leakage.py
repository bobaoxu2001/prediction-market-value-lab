"""A backtest may only see what the recommendation could see.

Every leakage bug has the same shape and the same symptom: the strategy looks
better than it was, and nothing in the output says why. The failure is silent by
construction, because using more information than was available never raises.

The rule is one line:

    an input is admissible only if it was observed at or before the moment the
    recommendation was published

and the reason it needs enforcing in code rather than discipline is that the
tempting query is always the wrong one. ``SELECT * FROM orderbook_snapshots WHERE
market_id = ?`` returns the newest book, which is exactly the book the
recommendation did not have.

Nine vectors are guarded here. They are not variations on one mistake: revised
economic data and a later orderbook leak through different code paths, and a
later *rule version* leaks through a path that looks like reading a constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from pmvl_shared.timeutil import ensure_utc


class LeakageError(AssertionError):
    """An input was used that postdates the recommendation it informs."""


@dataclass(frozen=True)
class Observation:
    """Anything a backtest might read, with the time it became knowable."""

    kind: str
    observed_at: datetime | None
    value: Any = None

    def known_by(self, cutoff: datetime) -> bool:
        """Whether this was available at ``cutoff``.

        An observation with no timestamp is NOT admissible. Absent provenance is
        not evidence of freshness, and treating it as admissible is how a
        backfilled row with no `observed_at` silently becomes a look-ahead.
        """
        observed = ensure_utc(self.observed_at)
        return observed is not None and observed <= ensure_utc(cutoff)


def admissible(
    observations: Iterable[Observation], *, published_at: datetime
) -> list[Observation]:
    """The subset a recommendation published at ``published_at`` could have used."""
    return [o for o in observations if o.known_by(published_at)]


def assert_no_leakage(
    observations: Sequence[Observation], *, published_at: datetime
) -> None:
    """Raise if any observation postdates publication.

    Used where a caller believes it has already filtered: a passing assertion is
    cheap, and the alternative is a silently optimistic backtest.
    """
    cutoff = ensure_utc(published_at)
    leaked = [
        o
        for o in observations
        if ensure_utc(o.observed_at) is None or ensure_utc(o.observed_at) > cutoff
    ]
    if leaked:
        detail = ", ".join(
            f"{o.kind}@{o.observed_at.isoformat() if o.observed_at else 'no timestamp'}"
            for o in leaked[:5]
        )
        raise LeakageError(
            f"{len(leaked)} observation(s) postdate the recommendation published at "
            f"{cutoff.isoformat()}: {detail}"
        )


def latest_admissible(
    observations: Sequence[Observation], *, published_at: datetime
) -> Observation | None:
    """The newest admissible observation, which is what a replay should use.

    Not the newest observation. That distinction is the entire bug class.
    """
    candidates = admissible(observations, published_at=published_at)
    if not candidates:
        return None
    return max(candidates, key=lambda o: ensure_utc(o.observed_at))


#: Frozen fields on a published recommendation. Re-deriving any of these from
#: current data changes what the record says was decided, so they are compared
#: rather than recomputed.
IMMUTABLE_PUBLICATION_FIELDS: tuple[str, ...] = (
    "entry_price_at_recommendation",
    "total_cost_at_recommendation",
    "executable_size",
    "fair_probability",
    "fair_probability_low",
    "fair_probability_high",
    "independent_probability_at_publication",
    "market_informed_probability_at_publication",
    "conservative_probability_at_publication",
    "expected_value",
    "conservative_net_ev",
    "model_version",
    "parser_version",
    "rule_version_id",
    "orderbook_snapshot",
    "evidence_snapshot",
    "risk_flags",
    "input_freshness",
    "input_data_cutoff",
)


def assert_publication_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Raise if any frozen field moved between two reads of the same record.

    A newer model may produce a research comparison; it may not overwrite the
    forecast that was actually published, because the track record's only value
    is that it cannot be revised after the outcome is known.
    """
    changed = [
        field
        for field in IMMUTABLE_PUBLICATION_FIELDS
        if field in before and before[field] != after.get(field)
    ]
    if changed:
        raise LeakageError(
            "published recommendation was modified after the fact: "
            + ", ".join(changed)
        )
