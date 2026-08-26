"""The research archive: history the product pipeline is not allowed to delete.

The problem this exists to fix
------------------------------
The deployment snapshot is a **product** artefact with a hard 40 MiB ceiling, and
the pipeline is a stateless recompute: each run seeds its operational database
from the last *published* snapshot. So whatever the snapshot builder prunes is
not merely absent from the artefact - it is absent from the next run's input, and
from every run after that.

The builder prunes, by design:

* every order book except the **latest per market** (``build_snapshot.py``), so
  no book time series can survive more than one cycle;
* ``raw_payload`` on every book;
* trades beyond 20 per market and price snapshots beyond 12 (the retention
  policy added in the previous phase - which correctly bounded the product and,
  unnoticed, made the research position worse).

Each of those is right for a product artefact that must fit a function bundle and
wrong for a research archive that must not lose anything. The two requirements
are genuinely opposed, and the resolution is not a compromise retention setting -
it is a second store.

What this is
------------
A separate SQLite file the product pipeline never opens. Append-only. No
retention policy, no ceiling, gitignored, backed up on its own terms. It holds
only the research universe, so it grows with what is being studied rather than
with the whole venue.

Being a different file is the entire safety argument: the snapshot builder
cannot prune what it does not have a connection to, and no future edit to the
retention policy can reach across.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .timeutil import ensure_utc

#: Categories whose full microstructure history is preserved.
#:
#: Deliberately narrow. Archiving every category would reproduce the size problem
#: in a second place; the archive is for the universe under active study, and
#: widening it is a decision with a storage cost that should be made explicitly.
DEFAULT_RESEARCH_CATEGORIES: tuple[str, ...] = ("weather",)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One captured book. `observed_at` is when the venue's state was read;
-- `available_at` is when this system could first have used it. They differ by
-- the poll and parse time, and a markout computed against the wrong one is a
-- markout against a book the decision did not have.
CREATE TABLE IF NOT EXISTS archived_books (
    id                   INTEGER PRIMARY KEY,
    platform             TEXT NOT NULL,
    platform_market_id   TEXT NOT NULL,
    series_ticker        TEXT NOT NULL DEFAULT '',
    settlement_station_id TEXT NOT NULL DEFAULT '',
    observed_at          TEXT NOT NULL,
    available_at         TEXT NOT NULL,
    source_timestamp     TEXT,
    poll_latency_ms      INTEGER,
    market_status        TEXT NOT NULL DEFAULT '',
    last_trade_at        TEXT,
    tick_size            TEXT NOT NULL DEFAULT '',
    fee_type             TEXT NOT NULL DEFAULT '',
    fee_rate             TEXT NOT NULL DEFAULT '',
    expected_resolution_time TEXT,
    best_yes_bid         TEXT, best_yes_ask TEXT,
    best_no_bid          TEXT, best_no_ask  TEXT,
    raw_payload_ref      TEXT NOT NULL DEFAULT '',
    collector_version    TEXT NOT NULL DEFAULT '',
    provenance           TEXT NOT NULL DEFAULT 'live',
    UNIQUE (platform, platform_market_id, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_books_market_time
    ON archived_books (platform, platform_market_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_books_available ON archived_books (available_at);

-- The FULL ladder, every level the venue returned. Not top-of-book: capacity,
-- slippage and every execution question need the ladder, and it cannot be
-- reconstructed from a best bid and ask.
CREATE TABLE IF NOT EXISTS archived_levels (
    book_id     INTEGER NOT NULL REFERENCES archived_books(id) ON DELETE CASCADE,
    side        TEXT NOT NULL,
    is_ask      INTEGER NOT NULL,
    level_index INTEGER NOT NULL,
    price       TEXT NOT NULL,
    size        TEXT NOT NULL,
    PRIMARY KEY (book_id, side, is_ask, level_index)
);

-- Public prints, retained in full. A maker fill cannot be simulated without
-- traded volume through the level, so pruning these to a display window removes
-- the only evidence a resting order could ever have been filled.
CREATE TABLE IF NOT EXISTS archived_trades (
    id                 INTEGER PRIMARY KEY,
    platform           TEXT NOT NULL,
    platform_market_id TEXT NOT NULL,
    platform_trade_id  TEXT NOT NULL,
    traded_at          TEXT NOT NULL,
    available_at       TEXT NOT NULL,
    price              TEXT NOT NULL,
    size               TEXT NOT NULL,
    taker_side         TEXT NOT NULL DEFAULT '',
    provenance         TEXT NOT NULL DEFAULT 'live',
    UNIQUE (platform, platform_trade_id)
);
CREATE INDEX IF NOT EXISTS ix_trades_market_time
    ON archived_trades (platform, platform_market_id, traded_at);

-- Daily collection-health snapshots, append-only.
--
-- The CLI report answers "is collection healthy now". This answers "was it
-- healthy on the 14th", which is the question that matters when a research
-- result turns out to rest on a week with a silent gap. A later good day must
-- not be able to erase the evidence of an earlier bad one, so this is INSERT
-- only: a second snapshot for the same day is a new row, and both survive.
CREATE TABLE IF NOT EXISTS health_snapshots (
    id            INTEGER PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    -- Key scalars, promoted out of the payload so a trend is one query.
    observations_per_station_day REAL,
    coverage_actual              REAL,
    published_at_coverage        REAL,
    poll_success_rate            REAL,
    cache_hits                   INTEGER,
    archived_books               INTEGER,
    archived_trades              INTEGER,
    best_window_books            INTEGER,
    mapped_markets               INTEGER,
    unmapped_markets             INTEGER,
    payload_failures             INTEGER,
    all_gates_passing            INTEGER NOT NULL DEFAULT 0,
    -- The whole report, so a field nobody promoted is still recoverable.
    payload_json  TEXT NOT NULL,
    collector_version TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_health_date ON health_snapshots (snapshot_date);
"""


class ArchiveError(RuntimeError):
    """The archive refused a write."""


@dataclass(frozen=True)
class ArchiveStats:
    books: int
    levels: int
    trades: int
    markets: int
    earliest: str | None
    latest: str | None
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "books": self.books,
            "levels": self.levels,
            "trades": self.trades,
            "markets": self.markets,
            "earliest": self.earliest,
            "latest": self.latest,
            "size_bytes": self.size_bytes,
        }


class ResearchArchive:
    """Append-only microstructure history for the research universe.

    There is no ``delete``, no ``prune`` and no retention policy, and that is the
    design rather than an omission. Everything else in this repository that holds
    market data is bounded by a product constraint; this is the one store whose
    job is to still have the data in a year.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self._path)
        try:
            con.execute("PRAGMA foreign_keys = ON")
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO archive_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            yield con
            con.commit()
        finally:
            con.close()

    def archive_book(
        self,
        con: sqlite3.Connection,
        *,
        platform: str,
        platform_market_id: str,
        observed_at: datetime,
        available_at: datetime,
        levels: Sequence[tuple[str, bool, int, Decimal, Decimal]],
        series_ticker: str = "",
        settlement_station_id: str = "",
        source_timestamp: datetime | None = None,
        poll_latency_ms: int | None = None,
        market_status: str = "",
        last_trade_at: datetime | None = None,
        tick_size: Decimal | None = None,
        fee_type: str = "",
        fee_rate: Decimal | None = None,
        expected_resolution_time: datetime | None = None,
        best: dict[str, Decimal | None] | None = None,
        raw_payload_ref: str = "",
        collector_version: str = "",
        provenance: str = "live",
    ) -> int | None:
        """Append one book with its full ladder. Returns the row id, or None.

        ``None`` means the exact capture was already archived - the same market
        at the same instant. That is a duplicate poll, not an error, and the
        uniqueness constraint is what makes a re-run of a collector idempotent
        rather than a source of double-counted books.
        """
        if provenance != "live":
            raise ArchiveError(
                f"refusing to archive {provenance!r} data; the research archive "
                "holds observed venue state only"
            )
        if ensure_utc(available_at) < ensure_utc(observed_at):
            raise ArchiveError(
                "available_at precedes observed_at; a book cannot be usable "
                "before it was read"
            )

        best = best or {}
        cur = con.execute(
            """
            INSERT OR IGNORE INTO archived_books (
                platform, platform_market_id, series_ticker, settlement_station_id,
                observed_at, available_at, source_timestamp, poll_latency_ms,
                market_status, last_trade_at, tick_size, fee_type, fee_rate,
                expected_resolution_time, best_yes_bid, best_yes_ask,
                best_no_bid, best_no_ask, raw_payload_ref, collector_version,
                provenance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                platform, platform_market_id, series_ticker, settlement_station_id,
                _iso(observed_at), _iso(available_at), _iso(source_timestamp),
                poll_latency_ms, market_status, _iso(last_trade_at),
                _text(tick_size), fee_type, _text(fee_rate),
                _iso(expected_resolution_time),
                _text(best.get("yes_bid")), _text(best.get("yes_ask")),
                _text(best.get("no_bid")), _text(best.get("no_ask")),
                raw_payload_ref, collector_version, provenance,
            ),
        )
        if cur.rowcount == 0:
            return None
        book_id = int(cur.lastrowid)
        con.executemany(
            "INSERT OR IGNORE INTO archived_levels "
            "(book_id, side, is_ask, level_index, price, size) VALUES (?,?,?,?,?,?)",
            [
                (book_id, side, 1 if is_ask else 0, index, str(price), str(size))
                for side, is_ask, index, price, size in levels
            ],
        )
        return book_id

    def archive_trades(
        self,
        con: sqlite3.Connection,
        *,
        platform: str,
        platform_market_id: str,
        trades: Iterable[dict[str, Any]],
        available_at: datetime,
        provenance: str = "live",
    ) -> int:
        """Append prints. Returns how many were new."""
        if provenance != "live":
            raise ArchiveError(f"refusing to archive {provenance!r} trades")
        written = 0
        for trade in trades:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO archived_trades (
                    platform, platform_market_id, platform_trade_id, traded_at,
                    available_at, price, size, taker_side, provenance
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    platform, platform_market_id, str(trade["platform_trade_id"]),
                    _iso(trade["traded_at"]), _iso(available_at),
                    str(trade["price"]), str(trade["size"]),
                    str(trade.get("taker_side", "")), provenance,
                ),
            )
            written += cur.rowcount
        return written

    def record_health(
        self, con: sqlite3.Connection, report: dict[str, Any], *, collector_version: str = ""
    ) -> int:
        """Append one health snapshot. Never updates an existing day.

        A second snapshot for a date does not replace the first. Two rows for one
        day is a fact about that day - the collector reported twice - and
        collapsing them would let a recovery erase the outage that preceded it.
        """
        import json as _json

        gates = {g["name"]: g for g in report.get("gates", [])}

        def gate(name: str) -> float | None:
            found = gates.get(name)
            return None if found is None else float(found["actual"])

        def percent(value: Any) -> float | None:
            text = str(value or "").rstrip("%")
            try:
                return float(text) / 100.0
            except ValueError:
                return None

        stations = report.get("station_observations", {})
        sources = report.get("source_observations", {})
        mapping = report.get("settlement_mapping", {})
        archive = report.get("research_archive", {})

        cursor = con.execute(
            """
            INSERT INTO health_snapshots (
                snapshot_date, captured_at, observations_per_station_day,
                coverage_actual, published_at_coverage, poll_success_rate,
                cache_hits, archived_books, archived_trades, best_window_books,
                mapped_markets, unmapped_markets, payload_failures,
                all_gates_passing, payload_json, collector_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(report.get("generated_at", ""))[:10],
                str(report.get("generated_at", "")),
                gate("station observations per station-day"),
                percent(stations.get("coverage")),
                percent(sources.get("published_at_coverage")),
                gate("source poll success rate"),
                int(gate("research cache hits") or 0),
                int(archive.get("books", 0)),
                int(archive.get("trades", 0)),
                int(gate("books per issuance window (best market)") or 0),
                int(mapping.get("mapped", 0)),
                int(mapping.get("unmapped", 0)),
                int(gate("payload integrity failures") or 0),
                1 if report.get("all_gates_passing") else 0,
                _json.dumps(report, sort_keys=True),
                collector_version,
            ),
        )
        return int(cursor.lastrowid)

    def health_history(
        self, con: sqlite3.Connection, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Recent snapshots, newest first. Gaps are visible as absent dates."""
        columns = (
            "snapshot_date", "captured_at", "observations_per_station_day",
            "coverage_actual", "published_at_coverage", "poll_success_rate",
            "cache_hits", "archived_books", "archived_trades",
            "best_window_books", "mapped_markets", "unmapped_markets",
            "payload_failures", "all_gates_passing",
        )
        rows = con.execute(
            f"SELECT {', '.join(columns)} FROM health_snapshots "
            "ORDER BY captured_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def stats(self) -> ArchiveStats:
        if not self._path.exists():
            return ArchiveStats(0, 0, 0, 0, None, None, 0)
        with self.connect() as con:
            books = con.execute("SELECT COUNT(*) FROM archived_books").fetchone()[0]
            levels = con.execute("SELECT COUNT(*) FROM archived_levels").fetchone()[0]
            trades = con.execute("SELECT COUNT(*) FROM archived_trades").fetchone()[0]
            markets = con.execute(
                "SELECT COUNT(DISTINCT platform || ':' || platform_market_id) "
                "FROM archived_books"
            ).fetchone()[0]
            span = con.execute(
                "SELECT MIN(observed_at), MAX(observed_at) FROM archived_books"
            ).fetchone()
        return ArchiveStats(
            books=books, levels=levels, trades=trades, markets=markets,
            earliest=span[0], latest=span[1],
            size_bytes=self._path.stat().st_size,
        )

    def book_series(
        self, con: sqlite3.Connection, *, platform: str, platform_market_id: str
    ) -> list[tuple[int, str, str]]:
        """Every archived capture for one market, oldest first.

        The query the product snapshot could never answer, because it keeps only
        the latest book per market.
        """
        return [
            (row[0], row[1], row[2])
            for row in con.execute(
                "SELECT id, observed_at, available_at FROM archived_books "
                "WHERE platform=? AND platform_market_id=? ORDER BY observed_at",
                (platform, platform_market_id),
            )
        ]


def _iso(value: datetime | None) -> str | None:
    return None if value is None else ensure_utc(value).isoformat()


def _text(value: Decimal | None) -> str:
    return "" if value is None else str(value)


def is_research_universe(
    category: str, *, categories: Sequence[str] = DEFAULT_RESEARCH_CATEGORIES
) -> bool:
    """Whether a market's history is preserved in full."""
    return category in categories


def default_archive_path(root: Path | str = ".") -> Path:
    """Where the archive lives by default.

    Beside the operational database but not inside it, and gitignored: it grows
    without a ceiling by design, and a store whose whole purpose is durability
    does not belong in an artefact bounded at 40 MiB.
    """
    return Path(root) / "data" / "research-archive.db"


def archive_freshness(stats: ArchiveStats) -> str:
    """One line a status command can print without interpreting anything."""
    if stats.books == 0:
        return "research archive is EMPTY - no microstructure history is being preserved"
    return (
        f"research archive: {stats.books:,} books / {stats.levels:,} levels / "
        f"{stats.trades:,} trades across {stats.markets:,} markets, "
        f"{stats.earliest} .. {stats.latest} ({stats.size_bytes / 1e6:.1f} MB)"
    )
