"""Appending research-universe books to the archive as they are collected.

This is the hook that stops the loss. The product snapshot builder keeps only the
**latest book per market**, and the pipeline re-seeds each run from the published
artefact - so every earlier capture is destroyed hourly and permanently. A book
time series therefore cannot exist in the product database at all, no matter how
often the collector runs.

Writing to the archive at collection time is the only moment the full ladder is
in memory and not yet subject to that prune. Doing it anywhere later would mean
reading it back from a store that has already thrown it away.

Why this is a separate module from ``ingest.store``
---------------------------------------------------
So the ingest path keeps one job. ``store_orderbooks`` writes the *product*
tables; this writes the *research* archive; and the archive being a different
file with a different retention contract is the entire safety argument. Mixing
them would put a prune-able and an un-prune-able write in one function, one
refactor away from sharing a retention policy.

Failures here never fail collection. A research archive that cannot be written is
a research problem; dropping the market data collection that feeds the product
because of it would be a strictly worse outcome. Failures are counted and
reported by the health check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from pmvl_shared.enums import DataProvenance, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.research_archive import ResearchArchive
from pmvl_shared.schemas import NormalizedMarket, OrderBook
from pmvl_shared.timeutil import ensure_utc, utcnow

from ..normalize.weather_stations import is_research_market, parse_settlement_station

log = get_logger(__name__)

COLLECTOR_VERSION = "archiver-v1.0.0"


@dataclass
class ArchiveReport:
    """What one archiving pass did, including what it declined to do."""

    books_archived: int = 0
    levels_archived: int = 0
    trades_archived: int = 0
    books_duplicate: int = 0
    markets_seen: int = 0
    skipped_not_research: int = 0
    errors: list[str] = field(default_factory=list)
    #: window anchor -> books, so a caller can see whether a high-frequency
    #: window actually produced high-frequency captures.
    by_window: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "books_archived": self.books_archived,
            "levels_archived": self.levels_archived,
            "trades_archived": self.trades_archived,
            "books_duplicate": self.books_duplicate,
            "markets_seen": self.markets_seen,
            "skipped_not_research": self.skipped_not_research,
            "errors": list(self.errors[:10]),
            "by_window": dict(sorted(self.by_window.items())),
        }


def archive_books(
    archive: ResearchArchive,
    books: Mapping[str, OrderBook],
    markets: Mapping[str, NormalizedMarket],
    *,
    available_at: datetime | None = None,
    window_anchor: str = "",
    poll_latency_ms: int | None = None,
) -> ArchiveReport:
    """Append every research-universe book, with its full ladder.

    ``available_at`` defaults to now rather than to the book's ``observed_at``.
    They are different instants - the venue's state was read at ``observed_at``
    and became usable to this system once the response was parsed - and equating
    them would grant the parse duration back as look-ahead on every capture.
    """
    report = ArchiveReport()
    available_at = ensure_utc(available_at or utcnow())

    eligible: list[tuple[str, OrderBook, NormalizedMarket]] = []
    for key, book in books.items():
        market = markets.get(key)
        if market is None:
            continue
        report.markets_seen += 1
        if not is_research_market(market.series_ticker, market.category.value):
            report.skipped_not_research += 1
            continue
        eligible.append((key, book, market))

    if not eligible:
        return report

    try:
        with archive.connect() as con:
            for _key, book, market in eligible:
                mapping = parse_settlement_station(
                    market.settlement_rules_raw, series_ticker=market.series_ticker
                )
                levels = _ladder(book)
                try:
                    book_id = archive.archive_book(
                        con,
                        platform=market.platform.value,
                        platform_market_id=market.platform_market_id,
                        observed_at=book.observed_at,
                        available_at=available_at,
                        levels=levels,
                        series_ticker=market.series_ticker or "",
                        # The station the contract settles on, never a grid cell.
                        # Empty when the parser refused, so an unmapped market is
                        # visibly unmapped in the archive rather than silently
                        # attributed to somewhere plausible.
                        settlement_station_id=mapping.station_id,
                        source_timestamp=book.source_timestamp,
                        poll_latency_ms=poll_latency_ms,
                        market_status=market.status.value,
                        last_trade_at=None,
                        tick_size=market.tick_size,
                        fee_type=market.fee_type,
                        fee_rate=market.fee_rate,
                        expected_resolution_time=market.expected_resolution_time,
                        best={
                            "yes_bid": book.best_bid(Side.YES),
                            "yes_ask": book.best_ask(Side.YES),
                            "no_bid": book.best_bid(Side.NO),
                            "no_ask": book.best_ask(Side.NO),
                        },
                        collector_version=COLLECTOR_VERSION,
                        provenance=DataProvenance.LIVE.value,
                    )
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(
                        f"{market.platform_market_id}: {type(exc).__name__}: {exc}"
                    )
                    continue

                if book_id is None:
                    # Same market, same observed_at. A duplicate poll, not an
                    # error - and counting it separately is what makes a stuck
                    # collector visible as duplicates rather than as growth.
                    report.books_duplicate += 1
                    continue
                report.books_archived += 1
                report.levels_archived += len(levels)
                if window_anchor:
                    report.by_window[window_anchor] = (
                        report.by_window.get(window_anchor, 0) + 1
                    )
    except Exception as exc:  # noqa: BLE001
        # Never fail collection because the archive is unavailable. The product
        # data still needs to be gathered, and a silent research gap is reported
        # rather than converted into a total outage.
        report.errors.append(f"archive unavailable: {type(exc).__name__}: {exc}")
        log.error("research archive write failed: %s", exc)

    return report


def archive_trades(
    archive: ResearchArchive,
    trades_by_market: Mapping[str, list[Any]],
    markets: Mapping[str, NormalizedMarket],
    *,
    available_at: datetime | None = None,
) -> ArchiveReport:
    """Append every research-universe trade print, in full.

    The product database caps trades at 20 per market to stay inside the
    snapshot's size ceiling. That cap is right for a bounded artefact and removes
    the only record that a price ever traded - so the prints are archived here,
    uncapped, at collection time.

    Archived as raw prints only. Nothing in this phase reads them, and nothing
    may: a fill model built on them is research, and research is not this phase.
    """
    report = ArchiveReport()
    available_at = ensure_utc(available_at or utcnow())

    eligible: list[tuple[NormalizedMarket, list[Any]]] = []
    for key, trades in trades_by_market.items():
        market = markets.get(key)
        if market is None or not trades:
            continue
        report.markets_seen += 1
        if not is_research_market(market.series_ticker, market.category.value):
            report.skipped_not_research += 1
            continue
        eligible.append((market, trades))

    if not eligible:
        return report

    try:
        with archive.connect() as con:
            for market, trades in eligible:
                payload = [
                    {
                        "platform_trade_id": t.platform_trade_id,
                        "traded_at": t.traded_at,
                        "price": t.price,
                        "size": t.size,
                        "taker_side": getattr(t, "taker_side", "") or "",
                    }
                    for t in trades
                ]
                try:
                    report.trades_archived += archive.archive_trades(
                        con,
                        platform=market.platform.value,
                        platform_market_id=market.platform_market_id,
                        trades=payload,
                        available_at=available_at,
                        provenance=DataProvenance.LIVE.value,
                    )
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(
                        f"{market.platform_market_id}: {type(exc).__name__}: {exc}"
                    )
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"archive unavailable: {type(exc).__name__}: {exc}")
        log.error("research archive trade write failed: %s", exc)
    return report


def _ladder(book: OrderBook) -> list[tuple[str, bool, int, Decimal, Decimal]]:
    """Every level of both sides, in the venue's own order.

    Not truncated. ``store_orderbooks`` caps the product tables at
    ``orderbook_depth`` levels, which is right for a bounded artefact and wrong
    here: capacity and slippage need the ladder the venue actually returned, and
    a truncated one silently understates depth at exactly the sizes that matter.
    """
    out: list[tuple[str, bool, int, Decimal, Decimal]] = []
    for side, is_ask, levels in (
        (Side.YES, False, book.yes_bids),
        (Side.YES, True, book.yes_asks),
        (Side.NO, False, book.no_bids),
        (Side.NO, True, book.no_asks),
    ):
        for index, level in enumerate(levels):
            out.append((side.value, is_ask, index, level.price, level.size))
    return out
