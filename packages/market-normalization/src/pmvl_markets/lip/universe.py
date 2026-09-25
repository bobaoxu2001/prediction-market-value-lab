"""Which (market, program) pairs the study records - decided without looking at a price.

Admission is the one place an analyst could steer a result: admit the markets that
turned out well. So the rule reads nothing that could carry an outcome. Its inputs are
the program's published terms, the market's static metadata (status, close time,
price grid) and the clock - no book, trade, price, volume or settlement - and its
order is a hash with a fixed salt (pre-registration 2.4). A pair once admitted stays
admitted to the end of its program, however it behaves.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .grid import is_linear_cent
from .model import ProgramPeriod
from .params import FAST_INFO_PATTERNS, RECORDING, RecordingParams
from .programs import family_of, rate_per_hour
from .version import SAMPLE_SALT

__all__ = [
    "AdmissionEvent",
    "AdmissionState",
    "AdmittedPair",
    "fast_info_class",
    "market_eligible",
    "program_eligible",
    "rank_key",
]


def fast_info_class(series: str) -> str | None:
    """X1-X4 of section 2.1, or None. First matching class wins."""
    for klass, pattern, exception in FAST_INFO_PATTERNS:
        if pattern.search(series) and not (exception and exception in series):
            return klass
    return None


def program_eligible(p: ProgramPeriod, params: RecordingParams = RECORDING) -> tuple[bool, str]:
    if p.end <= p.start:
        return False, "duration"
    if (p.end_ts - p.start_ts) < params.min_program_seconds:
        return False, "duration"
    if rate_per_hour(p) < params.min_rate_per_hour:
        return False, "rate"
    klass = fast_info_class(p.series)
    if klass:
        return False, klass
    return True, ""


def market_eligible(meta: dict[str, Any], at: datetime,
                    params: RecordingParams = RECORDING) -> tuple[bool, str]:
    if str(meta.get("status", "")).lower() not in ("open", "active"):
        return False, "status"
    if not is_linear_cent(meta):
        return False, "grid"
    close = meta.get("close_time")
    if not close:
        return False, "close_time"
    close_at = datetime.fromisoformat(str(close).replace("Z", "+00:00"))
    if close_at < at + timedelta(seconds=params.min_seconds_to_close):
        return False, "close_time"
    return True, ""


def rank_key(p: ProgramPeriod) -> str:
    return hashlib.sha256(f"{SAMPLE_SALT}|{p.market_ticker}|{p.id}".encode()).hexdigest()


@dataclass(frozen=True)
class AdmissionEvent:
    at: str
    program_id: str
    market: str
    series: str
    family: str
    decision: str  # admitted | rejected | deferred
    reason: str


@dataclass
class AdmittedPair:
    program_id: str
    market: str
    family: str
    admitted_at: float
    start: float
    end: float
    target: str

    def live(self, t: float) -> bool:
        return t < self.end


@dataclass
class AdmissionState:
    """Every pair admitted so far and every candidate already decided. Persisted daily."""

    admitted: dict[str, AdmittedPair] = field(default_factory=dict)
    decided: set[str] = field(default_factory=set)
    known_series: set[str] = field(default_factory=set)
    params: RecordingParams = RECORDING

    def refresh(
        self,
        programs: Iterable[ProgramPeriod],
        market_meta: dict[str, dict[str, Any]],
        at: datetime,
        *,
        window_end: datetime,
    ) -> list[AdmissionEvent]:
        """Decide every undecided candidate that is live now or starts before window_end.

        Deliberately takes no book, trade or price argument: admission cannot see one.
        A candidate whose market metadata has not been fetched yet is deferred, not
        decided, so a slow metadata lookup cannot turn into a rejection.
        """
        now = at.timestamp()
        programs = list(programs)
        self.known_series.update(p.series for p in programs)
        stamp = at.astimezone(timezone.utc).isoformat()
        events: list[AdmissionEvent] = []
        eligible: list[tuple[ProgramPeriod, str]] = []
        for p in programs:
            if p.id in self.decided:
                continue
            if p.end_ts <= now or p.start > window_end:
                continue
            fam = family_of(p.series, self.known_series)
            ok, reason = program_eligible(p, self.params)
            if ok:
                meta = market_meta.get(p.market_ticker)
                if meta is None:
                    events.append(AdmissionEvent(stamp, p.id, p.market_ticker, p.series, fam,
                                                 "deferred", "no_metadata"))
                    continue
                ok, reason = market_eligible(meta, at, self.params)
            if not ok:
                self.decided.add(p.id)
                events.append(AdmissionEvent(stamp, p.id, p.market_ticker, p.series, fam,
                                             "rejected", reason))
                continue
            eligible.append((p, fam))

        live = {k: v for k, v in self.admitted.items() if v.live(now)}
        for p, fam in sorted(eligible, key=lambda x: rank_key(x[0])):
            self.decided.add(p.id)
            if any(v.market == p.market_ticker for v in live.values()):
                reason = "overlap"
            elif sum(1 for v in live.values() if v.family == fam) >= self.params.family_cap:
                reason = "cap_family"
            elif len(live) >= self.params.total_cap:
                reason = "cap_total"
            else:
                pair = AdmittedPair(p.id, p.market_ticker, fam, now, p.start_ts, p.end_ts,
                                    str(p.target))
                self.admitted[p.id] = pair
                live[p.id] = pair
                events.append(AdmissionEvent(stamp, p.id, p.market_ticker, p.series, fam,
                                             "admitted", ""))
                continue
            events.append(AdmissionEvent(stamp, p.id, p.market_ticker, p.series, fam,
                                         "rejected", reason))
        return events

    def live_pairs(self, t: float) -> list[AdmittedPair]:
        return sorted((v for v in self.admitted.values() if v.live(t)),
                      key=lambda v: (v.admitted_at, v.program_id))

    def admitted_seconds(self, pair: AdmittedPair, window: tuple[float, float]) -> tuple[float, float]:
        """[max(start, admission), end) intersected with a day's quoting window."""
        lo = max(pair.start, pair.admitted_at, window[0])
        hi = min(pair.end, window[1])
        return (lo, hi) if hi > lo else (lo, lo)

    def to_json(self) -> dict[str, Any]:
        return {
            "admitted": {k: asdict(v) for k, v in self.admitted.items()},
            "decided": sorted(self.decided),
            "known_series": sorted(self.known_series),
        }

    @staticmethod
    def from_json(body: dict[str, Any]) -> "AdmissionState":
        return AdmissionState(
            admitted={k: AdmittedPair(**v) for k, v in (body.get("admitted") or {}).items()},
            decided=set(body.get("decided") or []),
            known_series=set(body.get("known_series") or []),
        )
