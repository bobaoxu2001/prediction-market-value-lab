"""The snapshot builder must not leave rows behind for markets it pruned.

``build_snapshot.py`` reduces the operational database to the markets that can
actually render, then cascades that decision to the child tables. Every table it
forgets keeps its rows forever: the pruned market never comes back, so nothing
ever deletes them, and the artefact grows by one run's worth of orphans every
hour until it crosses the size ceiling and validation fails.

That is exactly what happened to ``market_rule_versions``. It was absent from the
cascade, its rows are the largest in the schema, and one is written per market
ever ingested - roughly four times as many markets as the snapshot keeps. By
2026-07-31 the published artefact carried 7,515 orphaned rule versions against
253 live ones, 9.75 MB of the 10.15 MB that table occupied, and the scheduled
pipeline had begun failing with "snapshot is 42.9 MB, above the 42 MB ceiling"
while all nine jobs reported SUCCESS.

These tests assert the cascade as a property of the builder rather than of that
one table, so the next child table to be added is covered too.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_snapshot.py"

#: Child tables keyed by market that the builder must reduce to the surviving
#: market set. A table here with no cascade is an unbounded leak.
MARKET_SCOPED_TABLES = (
    "outcomes",
    "market_rules",
    "trades",
    "price_snapshots",
    "market_rule_versions",
)

RENDERABLE_MARKET_ID = 1
PRUNED_MARKET_ID = 2


def _create_schema(path: Path) -> None:
    from sqlalchemy import create_engine

    from pmvl_shared.db import models  # noqa: F401  (registers the mappers)
    from pmvl_shared.db.base import Base

    engine = create_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()


def _insert(cur: sqlite3.Cursor, table: str, **values: object) -> None:
    """Insert a row, filling in whatever else the schema insists on.

    These tables carry dozens of NOT NULL columns that are irrelevant here.
    Deriving the filler from the schema keeps the test about retention, and stops
    it breaking every time an unrelated column is added.
    """
    now = datetime.now(timezone.utc).isoformat(sep=" ")
    filler: dict[str, object] = {}
    for _, name, column_type, not_null, default, _pk in cur.execute(
        f"PRAGMA table_info({table})"
    ):
        if name in values or not not_null or default is not None or name == "id":
            continue
        filler[name] = now if column_type == "DATETIME" else "0"

    row = {**filler, **values}
    columns = ", ".join(f'"{name}"' for name in row)
    placeholders = ", ".join("?" for _ in row)
    cur.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values())
    )


def _seed(path: Path) -> None:
    """One market that survives the prune and one that does not.

    Both are ingested and both get a rule version, which is what the real
    pipeline does: rules are recorded for the entire ingested universe, while
    only a quarter of it is renderable.
    """
    con = sqlite3.connect(path)
    cur = con.cursor()

    for market_id, platform_market_id in (
        (RENDERABLE_MARKET_ID, "keeps-a-prediction"),
        (PRUNED_MARKET_ID, "ingested-but-not-renderable"),
    ):
        _insert(
            cur,
            "markets",
            id=market_id,
            platform="kalshi",
            platform_market_id=platform_market_id,
        )
        # A rule version per ingested market, exactly as rule_history records it.
        # These rows are the largest in the schema, which is why forgetting them
        # dominated the artefact's growth.
        _insert(
            cur,
            "market_rule_versions",
            market_id=market_id,
            version=1,
            raw_rules="x" * 2048,
            rule_hash=f"hash-{market_id}",
        )

    # Only the first market is renderable, so only it survives the market prune.
    _insert(cur, "model_predictions", market_id=RENDERABLE_MARKET_ID)

    con.commit()
    con.close()


@pytest.fixture()
def built_snapshot(tmp_path: Path) -> Path:
    source = tmp_path / "operational.db"
    target = tmp_path / "candidate.db"
    _create_schema(source)
    _seed(source)

    result = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--source",
            str(source),
            "--target",
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return target


def _market_ids(path: Path, table: str) -> set[int]:
    con = sqlite3.connect(path)
    try:
        return {row[0] for row in con.execute(f'SELECT market_id FROM "{table}"')}
    finally:
        con.close()


class TestPrunedMarketsLeaveNothingBehind:
    def test_the_unrenderable_market_is_pruned(self, built_snapshot: Path) -> None:
        """The precondition. If this stops holding, the cascade tests are vacuous."""
        con = sqlite3.connect(built_snapshot)
        try:
            surviving = {row[0] for row in con.execute("SELECT id FROM markets")}
        finally:
            con.close()
        assert surviving == {RENDERABLE_MARKET_ID}

    @pytest.mark.parametrize("table", MARKET_SCOPED_TABLES)
    def test_no_child_row_outlives_its_market(
        self, built_snapshot: Path, table: str
    ) -> None:
        """This is the regression. ``market_rule_versions`` failed it, and every
        hourly run added another ~8,000 orphans to the published artefact."""
        orphans = _market_ids(built_snapshot, table) - {RENDERABLE_MARKET_ID}
        assert not orphans, (
            f"{table} kept rows for markets the builder pruned: {sorted(orphans)}. "
            "These are unreachable - no endpoint can query a market that is not in "
            "the snapshot - and nothing will ever delete them."
        )

    def test_the_surviving_market_keeps_its_rules(
        self, built_snapshot: Path
    ) -> None:
        """The cascade must delete orphans only. Dropping the live rule version
        would be a data loss dressed up as a size fix."""
        assert _market_ids(built_snapshot, "market_rule_versions") == {
            RENDERABLE_MARKET_ID
        }


class TestTheCascadeIsDeclaredForEveryMarketScopedTable:
    def test_the_builder_deletes_from_each_one(self) -> None:
        """A child table added later without a cascade reintroduces the leak
        silently: the artefact just grows a little faster each release."""
        source = BUILDER.read_text()
        for table in MARKET_SCOPED_TABLES:
            assert (
                f"DELETE FROM {table} WHERE market_id NOT IN" in source
            ), f"{table} is not cascaded to the surviving market set"


# --------------------------------------------------------------------------
# Browsable markets: what earns a place without an order book.
# --------------------------------------------------------------------------

#: Ids for the browsability fixture, one per case under test.
TRADED_QUOTED_ID = 10
UNTRADED_QUOTED_ID = 11
UNQUOTED_ID = 12
CLOSED_ID = 13
BOOKLESS_BUT_SETTLED_ID = 14


def _seed_browsable(path: Path) -> None:
    """Markets that differ only in the fields the admission rule reads."""
    con = sqlite3.connect(path)
    cur = con.cursor()

    def market(market_id: int, **overrides: object) -> None:
        base: dict[str, object] = {
            "id": market_id,
            "platform": "kalshi",
            "platform_market_id": f"m-{market_id}",
            "status": "open",
            "accepting_orders": 1,
            "best_yes_ask": "0.34",
            # Money columns are TEXT in SQLite. Stored as the pipeline stores them.
            "volume_24h": "1500.00",
        }
        _insert(cur, "markets", **{**base, **overrides})

    market(TRADED_QUOTED_ID)
    # "0.00", not "0" - the quantised form the Money column actually stores, and
    # the one that exposes the comparison bug. Under TEXT affinity SQLite pushes
    # the integer literal to TEXT and compares lexically: "0" > "0" is false, so a
    # fixture written as "0" is dropped by accident and proves nothing, while the
    # real "0.00" is lexically greater than "0" and survives an uncast filter.
    market(UNTRADED_QUOTED_ID, volume_24h="0.00")
    market(UNQUOTED_ID, best_yes_ask=None, best_no_ask=None)
    market(CLOSED_ID, status="settled")

    # No book, no prediction, no quote - but it settled, so the old rule keeps it
    # and the widened rule must not drop it.
    market(BOOKLESS_BUT_SETTLED_ID, status="settled", best_yes_ask=None)
    _insert(cur, "settlements", market_id=BOOKLESS_BUT_SETTLED_ID)

    # The builder reads a model version out of this table to stamp the manifest,
    # so it needs at least one row. Attached here rather than to the traded
    # market, which must survive on its quote and volume alone or this fixture
    # would prove nothing.
    _insert(cur, "model_predictions", market_id=BOOKLESS_BUT_SETTLED_ID)

    con.commit()
    con.close()


@pytest.fixture()
def browsable_snapshot(tmp_path: Path) -> Path:
    source = tmp_path / "operational.db"
    target = tmp_path / "candidate.db"
    _create_schema(source)
    _seed_browsable(source)

    result = subprocess.run(
        [sys.executable, str(BUILDER), "--source", str(source), "--target", str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return target


def _surviving_markets(path: Path) -> set[int]:
    con = sqlite3.connect(path)
    try:
        return {row[0] for row in con.execute("SELECT id FROM markets")}
    finally:
        con.close()


class TestBrowsableMarketRetention:
    """A quoted, traded market is worth carrying even with no order book.

    Execution cost needs no probability and no ladder: fees, tick rounding,
    transfer and capital cost are exact from the market row, and the missing
    depth component is reported as unknown rather than assumed zero. Requiring a
    book reduced ~8,000 ingested markets to ~340, so the public browser showed
    4% of either venue and a reader looking up their own contract rarely found it.
    """

    def test_a_traded_quoted_market_survives_without_a_book(
        self, browsable_snapshot: Path
    ) -> None:
        assert TRADED_QUOTED_ID in _surviving_markets(browsable_snapshot)

    def test_an_untraded_market_is_dropped(self, browsable_snapshot: Path) -> None:
        """The floor that makes the widening affordable.

        Eighty per cent of quoted open markets had no 24h volume on the run that
        sized this, and carrying them cost ~21 MB against a 40 MB ceiling.
        """
        assert UNTRADED_QUOTED_ID not in _surviving_markets(browsable_snapshot)

    def test_an_unquoted_market_is_dropped(self, browsable_snapshot: Path) -> None:
        # Nothing can be priced without an ask from some source.
        assert UNQUOTED_ID not in _surviving_markets(browsable_snapshot)

    def test_a_closed_market_with_no_history_is_dropped(
        self, browsable_snapshot: Path
    ) -> None:
        assert CLOSED_ID not in _surviving_markets(browsable_snapshot)

    def test_the_previous_admission_rule_still_holds(
        self, browsable_snapshot: Path
    ) -> None:
        # Widening must add markets, never remove ones that already qualified.
        assert BOOKLESS_BUT_SETTLED_ID in _surviving_markets(browsable_snapshot)

    def test_the_volume_floor_compares_numerically_not_lexically(
        self, browsable_snapshot: Path
    ) -> None:
        """The trap that makes the floor silently vacuous.

        `volume_24h` is a Money column and lands in SQLite as TEXT. SQLite orders
        every TEXT value above every INTEGER, so an uncast `volume_24h > 0`
        matches a row whose volume is the string '0' — and the filter admits the
        entire quoted universe while appearing to bound it.
        """
        survivors = _surviving_markets(browsable_snapshot)
        assert UNTRADED_QUOTED_ID not in survivors, (
            "a market with volume_24h='0' survived, which means the comparison "
            "ran against TEXT and the floor is not filtering anything"
        )


# --------------------------------------------------------------------------
# Stale predictions on settled markets.
# --------------------------------------------------------------------------

SETTLED_WITH_STALE_PREDICTION_ID = 20
SETTLED_WITH_RECOMMENDED_PREDICTION_ID = 21
OPEN_WITH_PREDICTION_ID = 22


def _seed_settled_predictions(path: Path) -> None:
    con = sqlite3.connect(path)
    cur = con.cursor()

    def market(market_id: int, status: str) -> None:
        _insert(
            cur,
            "markets",
            id=market_id,
            platform="kalshi",
            platform_market_id=f"s-{market_id}",
            status=status,
            accepting_orders=1 if status == "open" else 0,
            best_yes_ask="0.34",
            volume_24h="1500.00",
        )

    market(SETTLED_WITH_STALE_PREDICTION_ID, "settled")
    market(SETTLED_WITH_RECOMMENDED_PREDICTION_ID, "settled")
    market(OPEN_WITH_PREDICTION_ID, "open")

    for market_id in (
        SETTLED_WITH_STALE_PREDICTION_ID,
        SETTLED_WITH_RECOMMENDED_PREDICTION_ID,
        OPEN_WITH_PREDICTION_ID,
    ):
        _insert(cur, "model_predictions", id=market_id * 10, market_id=market_id)
        # Keep the settled markets in the artefact under the pre-existing rule.
        # `platform_market_id` is spelled out because settlements are unique on
        # (platform, platform_market_id) and the schema filler would give every
        # row the same one.
        if market_id != OPEN_WITH_PREDICTION_ID:
            _insert(
                cur,
                "settlements",
                market_id=market_id,
                platform="kalshi",
                platform_market_id=f"s-{market_id}",
            )

    # Only one of them is the subject of a published recommendation.
    _insert(
        cur,
        "recommendations",
        market_id=SETTLED_WITH_RECOMMENDED_PREDICTION_ID,
        prediction_id=SETTLED_WITH_RECOMMENDED_PREDICTION_ID * 10,
    )

    con.commit()
    con.close()


@pytest.fixture()
def settled_prediction_snapshot(tmp_path: Path) -> Path:
    source = tmp_path / "operational.db"
    target = tmp_path / "candidate.db"
    _create_schema(source)
    _seed_settled_predictions(source)
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--source", str(source), "--target", str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return target


class TestStalePredictionsOnSettledMarkets:
    """A settled market's prediction is never recomputed, so it never changes.

    Scoring visits open markets only. A row written while the market was live
    survives every later run carrying whatever the model said then — including
    after the model that said it has been corrected and removed. Those rows were
    the only estimates left in the artefact that disagreed wildly with their own
    market, and they cannot be acted on by anyone.
    """

    def test_a_stale_settled_prediction_is_dropped(
        self, settled_prediction_snapshot: Path
    ) -> None:
        assert SETTLED_WITH_STALE_PREDICTION_ID not in _market_ids(
            settled_prediction_snapshot, "model_predictions"
        )

    def test_a_recommended_prediction_is_kept(
        self, settled_prediction_snapshot: Path
    ) -> None:
        """The track record resolves through `recommendations.prediction_id`.

        Dropping the row a published recommendation points at would leave the
        case study and track record describing a settled call with no estimate
        behind it.
        """
        assert SETTLED_WITH_RECOMMENDED_PREDICTION_ID in _market_ids(
            settled_prediction_snapshot, "model_predictions"
        )

    def test_an_open_market_keeps_its_prediction(
        self, settled_prediction_snapshot: Path
    ) -> None:
        assert OPEN_WITH_PREDICTION_ID in _market_ids(
            settled_prediction_snapshot, "model_predictions"
        )
