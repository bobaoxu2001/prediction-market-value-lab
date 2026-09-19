"""The forward recorder: one request per event per cycle, and the failures too.

What this does not do is as important as what it does. It never assembles an event
from more than one response, never retries a failed capture into a per-market fetch,
never drops an event because its books were empty, and never skips a cycle because
nothing interesting happened. The denominator has to be honest or the numerator means
nothing, and the denominator is "cycles attempted", not "cycles that produced a
result I liked".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from pmvl_shared.enums import Platform
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D
from pmvl_shared.timeutil import iso, parse_ts, utcnow

from ..providers.kalshi import MAX_BATCH_TICKERS, KalshiProvider
from .schema import (
    STRATEGY_VERSION,
    CaptureMode,
    EventSnapshot,
    RejectionReason,
    SnapshotRejection,
    TradeRecord,
)
from .writer import AppendOnlyLog

log = get_logger(__name__)

__all__ = ["RecorderConfig", "RecorderStats", "SynchronizedRecorder", "resolve_event_legs"]


@dataclass
class RecorderConfig:
    event_tickers: tuple[str, ...]
    out_dir: Path
    interval_seconds: float = 5.0
    duration_seconds: float = 300.0
    depth: int = 25
    #: Tickers whose prints are pulled each cycle. Empty means "every two-sided leg
    #: of every captured event", capped by ``max_trade_watch``.
    trade_watch: tuple[str, ...] = ()
    max_trade_watch: int = 24
    record_trades: bool = True
    #: Ceiling on the share of each cycle spent pulling prints. The tape is worth
    #: having but it is not what the cadence is for: a hundred trade requests on a
    #: busy evening take longer than the whole interval, and the book capture - the
    #: thing the study actually measures - starves. Observed: sessions that opened
    #: with 950 snapshots in half an hour ended with eight, while the trade count
    #: went *up*.
    trade_budget_fraction: float = 0.5


@dataclass
class RecorderStats:
    cycles: int = 0
    snapshots: int = 0
    rejections: int = 0
    trades: int = 0
    #: Cycles in which the tape pull hit its time budget and stopped early. A high
    #: count means the watchlist is wider than the cadence can carry, which is worth
    #: knowing before it shows up as a session with eight snapshots in it.
    trade_budget_exhausted: int = 0
    legs_captured: int = 0
    two_sided_legs: int = 0
    skew_bounds_ms: list[float] = field(default_factory=list)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        skews = sorted(self.skew_bounds_ms)
        return {
            "cycles": self.cycles,
            "snapshots": self.snapshots,
            "rejections": self.rejections,
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "trades": self.trades,
            "trade_budget_exhausted_cycles": self.trade_budget_exhausted,
            "legs_captured": self.legs_captured,
            "two_sided_legs": self.two_sided_legs,
            "skew_bound_ms_p50": _pct(skews, 0.50),
            "skew_bound_ms_p95": _pct(skews, 0.95),
            "skew_bound_ms_max": skews[-1] if skews else None,
        }


def _pct(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(q * (len(sorted_values) - 1) + 0.5))
    return round(sorted_values[idx], 1)


async def resolve_event_legs(
    provider: KalshiProvider, event_ticker: str
) -> tuple[list[str], list[Any]]:
    """Every open market in the event, in ticker order, with its normalized record.

    Ordered so that a capture of the same event on two different days requests the
    same set in the same sequence; an unordered set would make two logs of the same
    event hard to compare for reasons that have nothing to do with the market.

    The normalized records come back too, and are written alongside the books. A
    recording of prices without the rules that settle them cannot be replayed into
    any structural claim later - the strike ladder would have to be re-fetched, and
    by then the venue may have relisted, corrected or expired the rungs, so the
    replay would be against different markets wearing the same tickers.
    """
    data = await provider._client.get_json(
        "/markets",
        params={"status": "open", "event_ticker": event_ticker, "limit": 1000},
        use_cache=False,
        allow_404=True,
    )
    rows = (data or {}).get("markets") or []
    rows.sort(key=lambda r: str(r.get("ticker")))
    tickers = [str(r.get("ticker")) for r in rows if r.get("ticker")]
    markets = []
    for row in rows:
        try:
            normalized = provider._normalize_market(row)
        except Exception as exc:  # a malformed row must not abort the capture
            log.debug("could not normalize %s: %s", row.get("ticker"), exc)
            continue
        if normalized is not None:
            markets.append(normalized)
    return tickers, markets


class SynchronizedRecorder:
    """Drives capture cycles and writes them down. Owns no analysis."""

    def __init__(
        self,
        provider: KalshiProvider,
        config: RecorderConfig,
        *,
        clock: Any = utcnow,
    ) -> None:
        self._provider = provider
        self._config = config
        self._clock = clock
        self.stats = RecorderStats()
        self._legs: dict[str, list[str]] = {}
        self._seen_trades: set[str] = set()
        self._trade_floor: dict[str, datetime] = {}
        #: Where the last cycle ran out of trade budget. The next one resumes there,
        #: so a watchlist too long to finish in one cycle is still covered over
        #: several - rather than the first N tickers being polled forever and the
        #: rest never at all.
        self._trade_cursor = 0

        config.out_dir.mkdir(parents=True, exist_ok=True)
        self.books_log = AppendOnlyLog(config.out_dir / "snapshots.jsonl")
        self.trades_log = AppendOnlyLog(config.out_dir / "trades.jsonl")
        self.markets_log = AppendOnlyLog(config.out_dir / "markets.jsonl")

    async def prepare(self) -> dict[str, list[str]]:
        """Resolve each event's legs once, before the clock starts.

        Resolving inside the loop would put a second request between the cycle's
        start and its capture, which is both slower and a way for the leg set to
        change mid-run without anything recording that it did.

        Idempotent, because ``run`` calls it and a caller that wants to inspect the
        leg set first will call it too; resolving twice would write the same
        rejections twice and inflate the refusal count.
        """
        if self._legs:
            return dict(self._legs)
        for event in self._config.event_tickers:
            legs, markets = await resolve_event_legs(self._provider, event)
            self._legs[event] = legs
            for market in markets:
                self.markets_log.append(
                    {
                        "kind": "market",
                        "strategy_version": STRATEGY_VERSION,
                        "event_ticker": event,
                        "resolved_at": iso(self._clock()),
                        "market": market.model_dump(mode="json"),
                    }
                )
            if not legs:
                self._reject(event, RejectionReason.NO_MARKETS, "event has no open markets")
            elif len(legs) > MAX_BATCH_TICKERS:
                self._reject(
                    event,
                    RejectionReason.TOO_MANY_LEGS,
                    f"{len(legs)} legs exceeds the {MAX_BATCH_TICKERS}-ticker batch "
                    "maximum, so no simultaneous read of this event exists",
                    requested=len(legs),
                )
        return dict(self._legs)

    @property
    def capturable_events(self) -> list[str]:
        return [
            e for e, legs in self._legs.items() if legs and len(legs) <= MAX_BATCH_TICKERS
        ]

    async def run(self) -> RecorderStats:
        await self.prepare()
        started = self._clock()
        deadline = started + timedelta(seconds=self._config.duration_seconds)
        while self._clock() <= deadline:
            cycle_start = self._clock()
            await self.capture_once()
            self.stats.cycles += 1
            elapsed = (self._clock() - cycle_start).total_seconds()
            remaining = self._config.interval_seconds - elapsed
            if remaining > 0 and self._clock() + timedelta(seconds=remaining) <= deadline:
                await asyncio.sleep(remaining)
            elif self._clock() > deadline:
                break
        return self.stats

    async def capture_once(self) -> list[EventSnapshot]:
        await self.prepare()
        snapshots: list[EventSnapshot] = []
        for event in self.capturable_events:
            snap = await self._capture_event(event)
            if snap is not None:
                snapshots.append(snap)
        if self._config.record_trades:
            await self._capture_trades(snapshots)
        return snapshots

    async def _capture_event(self, event: str) -> EventSnapshot | None:
        legs = self._legs[event]
        capture = await self._provider.get_orderbooks_synchronized(
            legs, depth=self._config.depth
        )
        if not capture.ok:
            reason = (
                RejectionReason.TOO_MANY_LEGS
                if "batch maximum" in capture.detail
                else RejectionReason.UNREQUESTED_TICKER
                if "unrequested" in capture.detail
                else RejectionReason.REQUEST_FAILED
            )
            self._reject(
                event, reason, capture.detail,
                requested=len(legs), returned=len(capture.books),
            )
            return None

        snapshot = EventSnapshot(
            event_ticker=event,
            platform=Platform.KALSHI,
            request_sent_at=capture.sent_at,
            response_received_at=capture.received_at,
            capture_mode=CaptureMode.SINGLE_REQUEST,
            books=capture.books,
            requested_tickers=capture.requested,
            missing_tickers=capture.missing,
            sequence=self.books_log.count,
        )
        self.books_log.append(snapshot.as_dict())
        self.stats.snapshots += 1
        self.stats.legs_captured += len(capture.books)
        self.stats.two_sided_legs += snapshot.two_sided_legs
        self.stats.skew_bounds_ms.append(float(snapshot.skew_bound_ms))
        return snapshot

    def _watchlist(self, snapshots: Sequence[EventSnapshot]) -> list[str]:
        """Which legs' prints to pull, spread evenly across the recorded events.

        Round-robin rather than first-N-sorted. Taking the first N of a sorted list
        hands the entire trade budget to whichever event sorts first - in practice
        forty strikes of one commodity ladder - and the other seven events are then
        recorded with quotes but no tape, which makes them unusable for fill
        measurement for a reason that is an artefact of alphabetical order.
        """
        if self._config.trade_watch:
            return list(self._config.trade_watch)[: self._config.max_trade_watch]

        per_event: list[list[str]] = []
        for snap in sorted(snapshots, key=lambda s: s.event_ticker):
            legs = [
                ticker
                for ticker, book in sorted(snap.books.items())
                if book.yes_asks and book.no_asks
            ]
            if legs:
                per_event.append(legs)

        out: list[str] = []
        depth = max((len(legs) for legs in per_event), default=0)
        for index in range(depth):
            for legs in per_event:
                if index < len(legs):
                    out.append(legs[index])
                    if len(out) >= self._config.max_trade_watch:
                        return out
        return out

    async def _capture_trades(self, snapshots: Sequence[EventSnapshot]) -> None:
        """Pull prints for the watched legs.

        Trades are fetched per ticker rather than from the global feed. The global
        feed runs at well over a hundred prints a second across the exchange, so
        filtering it down to a dozen tickers would mean paginating through most of
        the exchange's tape to find them - and missing prints is not a neutral error
        here, it biases fill measurement downward.
        """
        watchlist = self._watchlist(snapshots)
        if not watchlist:
            return
        budget = self._config.interval_seconds * self._config.trade_budget_fraction
        started = time.monotonic()
        rotated = watchlist[self._trade_cursor % len(watchlist):] + watchlist[
            : self._trade_cursor % len(watchlist)
        ]

        polled = 0
        for ticker in rotated:
            if time.monotonic() - started >= budget:
                break
            polled += 1
            floor = self._trade_floor.get(ticker)
            params: dict[str, Any] = {"ticker": ticker, "limit": 200}
            if floor is not None:
                params["min_ts"] = int(floor.timestamp())
            try:
                data = await self._provider._client.get_json(
                    "/markets/trades", params=params, use_cache=False, allow_404=True
                )
            except Exception as exc:  # provider errors are recorded, not raised
                log.debug("trade fetch failed for %s: %s", ticker, exc)
                continue
            rows = (data or {}).get("trades") or []
            newest = floor
            for row in rows:
                trade = _parse_trade(ticker, row)
                if trade is None or trade.trade_id in self._seen_trades:
                    continue
                self._seen_trades.add(trade.trade_id)
                self.trades_log.append(trade.as_dict())
                self.stats.trades += 1
                if newest is None or trade.traded_at > newest:
                    newest = trade.traded_at
            if newest is not None:
                #: Step back a second: ``min_ts`` is inclusive at second resolution
                #: and prints inside the same second would otherwise be lost.
                self._trade_floor[ticker] = newest - timedelta(seconds=1)

        self._trade_cursor = (self._trade_cursor + polled) % max(1, len(watchlist))
        if polled < len(rotated):
            self.stats.trade_budget_exhausted += 1

    def _reject(
        self,
        event: str,
        reason: RejectionReason,
        detail: str,
        *,
        requested: int = 0,
        returned: int = 0,
    ) -> None:
        rejection = SnapshotRejection(
            event_ticker=event,
            attempted_at=self._clock(),
            reason=reason,
            detail=detail,
            requested_legs=requested,
            returned_legs=returned,
        )
        self.books_log.append(rejection.as_dict())
        self.stats.rejections += 1
        self.stats.rejection_reasons[reason.value] = (
            self.stats.rejection_reasons.get(reason.value, 0) + 1
        )


def _parse_trade(ticker: str, row: dict[str, Any]) -> TradeRecord | None:
    traded_at = parse_ts(row.get("created_time"))
    trade_id = row.get("trade_id")
    price = row.get("yes_price_dollars")
    size = row.get("count_fp")
    if not (traded_at and trade_id and price is not None and size is not None):
        return None
    return TradeRecord(
        platform_market_id=ticker,
        trade_id=str(trade_id),
        traded_at=traded_at,
        yes_price=D(price),
        size=D(size),
        taker_side=str(row.get("taker_side") or ""),
        is_block_trade=bool(row.get("is_block_trade")),
    )
