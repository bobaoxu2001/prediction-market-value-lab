"""A recorded LIP day, read back into the shared types - chain verified first.

One pass over the book stream keeps, per market, the send and receive times of every
request that covered it and an index into that market's distinct book states. A day
of 200 markets at 1 Hz is about nine million observations but far fewer distinct
books, so states are stored once and a market's ``BookSeries`` is built only when the
simulator asks for that market.

"Unchanged" in a book line is an observation: the request covered the market and its
book equalled the last one written. A market absent from a line was not observed at
that time, and gets no snapshot.
"""

from __future__ import annotations

import json
from array import array
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from ..recording.writer import read_log
from .model import (
    BookSeries,
    BookSide,
    BookSnapshot,
    MarketDay,
    StatusPoint,
    StatusSeries,
    TapeCoverage,
    TradeSeries,
)
from .programs import ProgramRegistry, family_of
from .universe import AdmissionState, AdmittedPair

__all__ = ["DayData", "load_day", "tape_coverage"]


def _ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


@dataclass
class _MarketBooks:
    sent: array = field(default_factory=lambda: array("d"))
    recv: array = field(default_factory=lambda: array("d"))
    state: array = field(default_factory=lambda: array("i"))
    lane: list[str] = field(default_factory=list)


@dataclass
class DayData:
    """Everything one recorded day holds, indexed for per-market replay."""

    day_dir: Path
    study: dict[str, Any]
    health: dict[str, Any]
    pairs: list[AdmittedPair]
    registry: ProgramRegistry
    meta: dict[str, dict[str, Any]]
    fees: dict[str, dict[str, Any]]
    window: tuple[float, float]
    coverage: TapeCoverage
    _books: dict[str, _MarketBooks]
    _states: list[tuple[BookSide, BookSide]]
    _trades: dict[str, list[tuple[float, int, float, bool, bool, str]]]
    _status: dict[str, list[StatusPoint]]
    settlements: dict[str, Decimal] = field(default_factory=dict)
    lane_ticks: dict[str, int] = field(default_factory=dict)
    #: close times as the status lane last saw them, for markets with no metadata row
    status_close: dict[str, str] = field(default_factory=dict)
    #: per-ticker tape coverage when the per-ticker tape (TAPE_PT) was recorded
    coverage_by_ticker: dict[str, TapeCoverage] = field(default_factory=dict)
    clock_offset: float = 0.0
    clock_eps: float = 0.25

    @property
    def counted(self) -> bool:
        return bool(self.health.get("counted_day"))

    def book_series(self, market: str) -> BookSeries:
        mb = self._books.get(market)
        if mb is None:
            return BookSeries(market, [])
        snaps = [BookSnapshot(mb.sent[i], mb.recv[i], *self._states[mb.state[i]], lane=mb.lane[i])
                 for i in range(len(mb.recv))]
        return BookSeries(market, snaps)

    def trade_series(self, market: str) -> TradeSeries:
        rows = sorted(self._trades.get(market, []))
        if not rows:
            return TradeSeries.empty(market)
        return TradeSeries(
            market,
            np.array([r[0] for r in rows], dtype=np.float64),
            np.array([r[1] for r in rows], dtype=np.int16),
            np.array([r[2] for r in rows], dtype=np.float64),
            np.array([r[3] for r in rows], dtype=bool),
            np.array([r[4] for r in rows], dtype=bool),
            [r[5] for r in rows],
        )

    def market_day(self, pair: AdmittedPair) -> MarketDay:
        # the program as published that day, or - if it has left the listing - the terms
        # it was admitted under: a withdrawn program is still replayed (and pays nothing)
        program = self.registry.programs.get(pair.program_id) or pair.program()
        lo = max(pair.start, pair.admitted_at, self.window[0])
        hi = min(pair.end, self.window[1])
        meta = self.meta.get(pair.market, {})
        close_raw = meta.get("close_time") or self.status_close.get(pair.market)
        if close_raw is None:
            raise ValueError(f"{pair.market}: no close time recorded that day - the close stop cannot bind")
        fee = self.fees.get(program.series, {})
        return MarketDay(
            market=pair.market, family=pair.family, program=program,
            admitted_from=lo, admitted_until=max(lo, hi), close_time=_ts(close_raw),
            fee_type=str(fee.get("fee_type") or ""),
            fee_multiplier=Decimal(str(fee.get("fee_multiplier") or 1)),
            books=self.book_series(pair.market), trades=self.trade_series(pair.market),
            coverage=self.coverage_by_ticker.get(pair.market, self.coverage),
            status=StatusSeries(list(self._status.get(pair.market, []))),
            settlement_yes=self.settlements.get(pair.market),
            clock_offset=self.clock_offset, clock_eps=self.clock_eps,
        )


def tape_coverage(polls: list[dict[str, Any]], audits: list[dict[str, Any]],
                  global_ids: dict[str, set[str]]) -> TapeCoverage:
    """Complete polls cover [min_ts, sent_at]; overlapping ones merge into intervals.

    An audit mismatch - a print the per-market read saw, dated inside a complete
    interval, that the global feed never delivered - marks [min_ts, at] as not covered.
    """
    spans = sorted((float(p["min_ts"]), float(p["sent_at"])) for p in polls if p.get("complete"))
    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    probe = TapeCoverage(merged)
    mismatches: list[tuple[float, float]] = []
    for audit in audits:
        seen = global_ids.get(audit["ticker"], set())
        for tid, created in audit.get("prints") or []:
            if not created:
                continue
            t = _ts(created)
            if tid not in seen and probe.covered(t, t):
                mismatches.append((float(audit["min_ts"]), float(audit["at"])))
                break
    return TapeCoverage(merged, mismatches)


def _levels(rows: list[list[float]]) -> BookSide:
    return BookSide.of([(int(p), float(q)) for p, q in rows])


def load_day(day_dir: Path, *, settlements_path: Path | None = None,
             state_path: Path | None = None) -> DayData:
    from .store import read_checked, stream_ends

    day_dir = Path(day_dir)
    ends = stream_ends(day_dir)
    study = json.loads((day_dir / "study.json").read_text())
    health = json.loads((day_dir / "health.json").read_text()) if (day_dir / "health.json").exists() else {}
    window = (_ts(study["window"]["window_start"]), _ts(study["window"]["window_end"]))

    registry = ProgramRegistry()
    for body in read_checked(day_dir, "programs", ends):
        registry.observe(body.get("rows") or [], float(body["at"]), listing="history")
    meta: dict[str, dict[str, Any]] = {}
    for body in read_checked(day_dir, "markets", ends):
        meta[body["ticker"]] = body
    fees = {b["series"]: b for b in read_checked(day_dir, "series", ends)}

    admitted: dict[str, AdmittedPair] = {}
    known = set(registry_series(registry))
    for body in read_checked(day_dir, "universe", ends):
        if body.get("decision") != "admitted":
            continue
        p = registry.programs.get(body["program_id"])
        if p is None:
            continue
        admitted[p.id] = AdmittedPair(p.id, p.market_ticker, body.get("family") or family_of(p.series, known),
                                      _ts(body["at"]), p.start_ts, p.end_ts, str(p.target))
    if state_path and Path(state_path).exists():
        # pairs admitted on an earlier day keep their place (2.4 item 6) - including one
        # whose program has since left the listing: withdrawn is not a reason to drop it
        prior = AdmissionState.from_json(json.loads(Path(state_path).read_text()))
        for k, v in prior.admitted.items():
            if k not in admitted and v.end > window[0] and v.admitted_at < window[1]:
                admitted[k] = v

    states: list[tuple[BookSide, BookSide]] = []
    state_index: dict[tuple, int] = {}
    current: dict[str, int] = {}
    books: dict[str, _MarketBooks] = {}
    lane_ticks: dict[str, int] = {}
    for body in read_checked(day_dir, "books", ends):
        if not body.get("ok"):
            continue
        lane = body.get("lane", "")
        if window[0] <= body["received_at"] < window[1]:
            lane_ticks[lane] = lane_ticks.get(lane, 0) + 1
        for ticker, m in (body.get("markets") or {}).items():
            key = (json.dumps(m["yes"]), json.dumps(m["no"]))
            idx = state_index.get(key)
            if idx is None:
                idx = len(states)
                states.append((_levels(m["yes"]), _levels(m["no"])))
                state_index[key] = idx
            current[ticker] = idx
            _append(books, ticker, body, idx, lane)
        for ticker in body.get("unchanged") or []:
            if ticker in current:
                _append(books, ticker, body, current[ticker], lane)

    trades: dict[str, list[tuple[float, int, float, bool, bool, str]]] = {}
    global_ids: dict[str, set[str]] = {}
    for body in read_checked(day_dir, "trades", ends):
        for r in body.get("prints") or []:
            try:
                row = (_ts(r["created_time"]), int(round(float(r["yes_price_dollars"]) * 100)),
                       float(r["count_fp"]), str(r.get("taker_side")) == "yes",
                       bool(r.get("is_block_trade")), str(r["trade_id"]))
            except (KeyError, TypeError, ValueError):
                continue
            trades.setdefault(r["ticker"], []).append(row)
            global_ids.setdefault(r["ticker"], set()).add(row[5])
    polls = list(read_checked(day_dir, "tape_polls", ends))
    audits = list(read_checked(day_dir, "audit", ends))
    coverage = tape_coverage([p for p in polls if not p.get("ticker")], audits, global_ids)
    coverage_by_ticker: dict[str, TapeCoverage] = {}
    for ticker in sorted({p["ticker"] for p in polls if p.get("ticker")}):
        mine = [p for p in polls if p.get("ticker") == ticker]
        coverage_by_ticker[ticker] = tape_coverage(mine, [a for a in audits if a["ticker"] == ticker], global_ids)

    status: dict[str, list[StatusPoint]] = {}
    status_close: dict[str, str] = {}
    for body in read_checked(day_dir, "status", ends):
        ex = body.get("exchange") or {}
        active = ex.get("trading_active")
        exchange = None if active is None or body.get("exchange_error") else bool(active)
        markets = body.get("markets") or {}
        for ticker, m in markets.items():
            if m.get("close_time"):
                status_close[ticker] = m["close_time"]
        chunks = body.get("chunks")
        if chunks is None:  # recorded before responses were stamped: the poll's start
            chunks = [{"requested": body.get("requested") or list(markets), "received_at": body["at"]}]
        ex_at = float(body.get("exchange_received_at") or body["at"])
        for chunk in chunks:
            # a point is known only once both its market answer and the exchange flag are in
            at = max(float(chunk["received_at"]), ex_at)
            for ticker in chunk["requested"]:
                m = markets.get(ticker)
                # a requested market the poll did not answer for is *unknown*, not open
                status.setdefault(ticker, []).append(
                    StatusPoint(at, str(m.get("status") or "") if m else "", exchange))

    offsets = sorted(float(b["offset_s"]) for b in read_checked(day_dir, "clock", ends) if b.get("offset_s") is not None)
    clock_offset = offsets[len(offsets) // 2] if offsets else 0.0
    #: without any clock sample the uncertainty is not known to be small
    clock_eps = abs(clock_offset) + 0.05 if offsets else 0.25

    settlements: dict[str, Decimal] = {}
    if settlements_path and Path(settlements_path).exists():
        for entry in read_log(Path(settlements_path)):
            b = entry.body
            if b.get("status") in ("finalized", "settled") and b.get("settlement_value_dollars") not in (None, ""):
                settlements[b["ticker"]] = Decimal(str(b["settlement_value_dollars"]))

    return DayData(day_dir=day_dir, study=study, health=health, pairs=list(admitted.values()),
                   registry=registry, meta=meta, fees=fees, window=window, coverage=coverage,
                   _books=books, _states=states, _trades=trades, _status=status,
                   settlements=settlements, lane_ticks=lane_ticks, status_close=status_close,
                   coverage_by_ticker=coverage_by_ticker, clock_offset=clock_offset, clock_eps=clock_eps)


def registry_series(registry: ProgramRegistry) -> set[str]:
    return {p.series for p in registry.programs.values()}


def _append(books: dict[str, _MarketBooks], ticker: str, body: dict[str, Any], idx: int, lane: str) -> None:
    mb = books.setdefault(ticker, _MarketBooks())
    mb.sent.append(float(body["sent_at"]))
    mb.recv.append(float(body["received_at"]))
    mb.state.append(idx)
    mb.lane.append(lane)
