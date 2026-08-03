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
