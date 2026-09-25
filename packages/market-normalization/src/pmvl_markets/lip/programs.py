"""Liquidity programs as the venue publishes them, and what changed between listings.

The payout a program makes depends on terms the venue can edit, and on whether it is
ever paid at all. So programs are not read once: every listing is diffed against the
last, and the registry remembers when each program was first seen, whether its terms
moved, and when it first showed ``paid_out``. The pessimistic envelope counts a
payout only from a program seen paid with its terms unchanged (pre-registration 1,
step 12); the registry is what makes that checkable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from .model import ProgramPeriod

__all__ = [
    "ProgramDiff",
    "ProgramRegistry",
    "US_STATE_CODES",
    "family_of",
    "n_total_seconds",
    "program_from_api",
    "rate_per_hour",
    "series_of",
]

CENTI_CENTS_PER_DOLLAR = Decimal(10_000)

#: Two-letter codes whose suffix folds a series into its national family (2.3).
US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def series_of(market_ticker: str) -> str:
    return market_ticker.split("-")[0]


def program_from_api(row: dict[str, Any]) -> ProgramPeriod:
    """One ``/incentive_programs`` row. ``period_reward`` is in centi-cents."""
    cap_raw = row.get("max_reward_per_account")
    return ProgramPeriod(
        id=str(row["id"]),
        market_ticker=str(row["market_ticker"]),
        series=series_of(str(row["market_ticker"])),
        start=_ts(row["start_date"]),
        end=_ts(row["end_date"]),
        reward=Decimal(int(row["period_reward"])) / CENTI_CENTS_PER_DOLLAR,
        target=Decimal(str(row.get("target_size_fp") or "0")),
        discount=Decimal(int(row.get("discount_factor_bps") or 0)) / CENTI_CENTS_PER_DOLLAR,
        cap=None if cap_raw is None else Decimal(int(cap_raw)) / CENTI_CENTS_PER_DOLLAR,
        paid_out=bool(row.get("paid_out")),
    )


def n_total_seconds(p: ProgramPeriod) -> int:
    """Snapshot seconds in the period: ceil(end) - floor(start) on microsecond stamps."""
    return math.ceil(p.end_ts) - math.floor(p.start_ts)


def rate_per_hour(p: ProgramPeriod) -> Decimal:
    seconds = Decimal(str(p.end_ts - p.start_ts))
    if seconds <= 0:
        return Decimal(0)
    return p.reward / (seconds / Decimal(3600))


def family_of(series: str, known_series: Iterable[str]) -> str:
    """The series, folded to its national family when it is a state variant (2.3).

    ``KXAAAGASDMI`` folds to ``KXAAAGASD`` because a series by that name has had a
    program; a ticker that merely ends in two capitals that happen to spell a state
    does not fold unless the remainder is itself a known series.
    """
    known = known_series if isinstance(known_series, (set, frozenset)) else set(known_series)
    if len(series) > 2 and series[-2:] in US_STATE_CODES and series[:-2] in known:
        return series[:-2]
    return series


def _terms(p: ProgramPeriod) -> tuple[Decimal, Decimal, Decimal, Decimal | None]:
    return (p.reward, p.target, p.discount, p.cap)


@dataclass
class ProgramDiff:
    added: list[str] = field(default_factory=list)
    #: listed as active before, absent now, and before its end: withdrawn early
    withdrawn: list[str] = field(default_factory=list)
    terms_changed: list[str] = field(default_factory=list)
    newly_paid: list[str] = field(default_factory=list)


@dataclass
class ProgramRegistry:
    """Every program ever listed, with the history the payout check needs."""

    programs: dict[str, ProgramPeriod] = field(default_factory=dict)
    first_seen_at: dict[str, float] = field(default_factory=dict)
    first_terms: dict[str, tuple] = field(default_factory=dict)
    terms_changed: set[str] = field(default_factory=set)
    paid_out_seen_at: dict[str, float] = field(default_factory=dict)
    withdrawn_at: dict[str, float] = field(default_factory=dict)
    _last_active: set[str] = field(default_factory=set)

    def observe(self, rows: Iterable[dict[str, Any]], at: float, *, listing: str) -> ProgramDiff:
        """Record one listing. ``listing`` is the status that was asked for.

        Withdrawal is only inferred from a *complete* ``active`` listing: a program that
        was active, is missing now, and has not reached its end.
        """
        diff = ProgramDiff()
        seen: set[str] = set()
        for row in rows:
            if row.get("incentive_type", "liquidity") != "liquidity":
                continue
            p = program_from_api(row)
            seen.add(p.id)
            if p.id not in self.programs:
                diff.added.append(p.id)
                self.first_seen_at[p.id] = at
                self.first_terms[p.id] = _terms(p)
            elif _terms(p) != self.first_terms[p.id] and p.id not in self.terms_changed:
                self.terms_changed.add(p.id)
                diff.terms_changed.append(p.id)
            if p.paid_out and p.id not in self.paid_out_seen_at:
                self.paid_out_seen_at[p.id] = at
                diff.newly_paid.append(p.id)
            self.programs[p.id] = p
        if listing == "active":
            for pid in self._last_active - seen:
                p = self.programs[pid]
                if at < p.end_ts and pid not in self.withdrawn_at:
                    self.withdrawn_at[pid] = at
                    diff.withdrawn.append(pid)
            self._last_active = seen
        return diff

    def verified(self, program_id: str) -> bool:
        """Seen paid, with the terms it was first seen under (the LOW envelope's test)."""
        return program_id in self.paid_out_seen_at and program_id not in self.terms_changed

    def withdrawn(self, program_id: str) -> bool:
        """Vanished from the active list before its end and never shown paid."""
        return program_id in self.withdrawn_at and program_id not in self.paid_out_seen_at

    def to_json(self) -> dict[str, Any]:
        return {
            "first_seen_at": self.first_seen_at,
            "first_terms": {k: [str(x) if x is not None else None for x in v]
                            for k, v in self.first_terms.items()},
            "terms_changed": sorted(self.terms_changed),
            "paid_out_seen_at": self.paid_out_seen_at,
            "withdrawn_at": self.withdrawn_at,
        }
