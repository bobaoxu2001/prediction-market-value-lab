"""A production snapshot must not be a demo-pruned artefact.

The committed gzip is the production Snapshot. ``build_demo_snapshot.py``
deliberately drops market_rules, raw rule text on non-recommendation markets,
and price history. That used to pass ``validate_snapshot.py`` because the API
list/track-record routes still answered. The market-detail page then had no
comparator, threshold, basis, cutoff, full rules, or price history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.validate_snapshot import (
    DEMO_PROFILE,
    PRODUCTION_PROFILE,
    production_coverage_problems,
)

# The same deletes the demo/archive builder applies for display data. Kept here
# as SQL rather than importing that script so the test names the contract, not
# the implementation accident.
_DEMO_PRUNE = (
    "DELETE FROM trades",
    """
    DELETE FROM price_snapshots WHERE market_id NOT IN (
        SELECT market_id FROM recommendations
    )
    """,
    """
    UPDATE markets
       SET description = '',
           settlement_rules_raw = ''
     WHERE id NOT IN (
         SELECT market_id FROM recommendations
         UNION SELECT market_id FROM recommendation_snapshots
     )
    """,
    """
    DELETE FROM market_rules WHERE market_id NOT IN (
        SELECT market_id FROM recommendations
        UNION SELECT market_id FROM recommendation_snapshots
    )
    """,
)


def _production_shaped(path: Path) -> None:
    """Live browsable markets plus the rule/price rows the detail page reads."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE markets (
            id INTEGER PRIMARY KEY,
            provenance TEXT NOT NULL,
            settlement_rules_raw TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE recommendations (
            id INTEGER PRIMARY KEY,
            market_id INTEGER
        );
        CREATE TABLE recommendation_snapshots (
            id INTEGER PRIMARY KEY,
            market_id INTEGER
        );
        CREATE TABLE market_rules (
            id INTEGER PRIMARY KEY,
            market_id INTEGER NOT NULL
        );
        CREATE TABLE price_snapshots (
            id INTEGER PRIMARY KEY,
            market_id INTEGER NOT NULL
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            market_id INTEGER NOT NULL
        );

        INSERT INTO markets (id, provenance, settlement_rules_raw, description)
        VALUES
            (1, 'live', 'Settle on the official print.', 'Live contract A'),
            (2, 'live', 'Settle on the index close.', 'Live contract B'),
            (3, 'demo', 'Demo rules.', 'Demo contract');
        INSERT INTO recommendations (id, market_id) VALUES (1, 3);
        INSERT INTO recommendation_snapshots (id, market_id) VALUES (1, 3);
        INSERT INTO market_rules (id, market_id) VALUES (1, 1), (2, 2);
        INSERT INTO price_snapshots (id, market_id) VALUES (1, 1), (2, 2);
        INSERT INTO trades (id, market_id) VALUES (1, 1);
        """
    )
    con.commit()
    con.close()


def _apply_demo_prune(path: Path) -> None:
    con = sqlite3.connect(path)
    for sql in _DEMO_PRUNE:
        con.execute(sql)
    con.commit()
    con.close()


def test_a_production_snapshot_with_rule_and_price_coverage_passes(
    tmp_path: Path,
) -> None:
    db = tmp_path / "production.db"
    _production_shaped(db)
    assert production_coverage_problems(db) == []
    assert production_coverage_problems(db, profile=PRODUCTION_PROFILE) == []


def test_a_demo_pruned_production_snapshot_fails(tmp_path: Path) -> None:
    """This is the regression: the previous CI accepted market_rules = 0."""
    db = tmp_path / "pruned.db"
    _production_shaped(db)
    _apply_demo_prune(db)
    problems = production_coverage_problems(db)
    assert problems
    joined = " ".join(problems)
    assert "market_rules coverage is empty" in joined
    assert "settlement_rules_raw" in joined
    assert "price_snapshots is empty" in joined


def test_an_intentional_demo_profile_skips_production_coverage(
    tmp_path: Path,
) -> None:
    db = tmp_path / "demo.db"
    _production_shaped(db)
    _apply_demo_prune(db)
    assert production_coverage_problems(db, profile=DEMO_PROFILE) == []


def test_the_committed_production_snapshot_keeps_rule_and_price_coverage() -> None:
    """Lock the repaired artefact: the published gzip must pass this gate."""
    from pmvl_shared.snapshot_artifact import resolve_snapshot_path

    root = Path(__file__).resolve().parents[1]
    manifest = root / "data" / "pmvl-snapshot.manifest.json"
    resolved = resolve_snapshot_path(manifest, root / "data" / "pmvl-snapshot.db")
    assert production_coverage_problems(resolved) == []


def test_a_demo_only_snapshot_has_nothing_for_production_coverage_to_require(
    tmp_path: Path,
) -> None:
    db = tmp_path / "demo-only.db"
    _production_shaped(db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM markets WHERE provenance = 'live'")
    con.execute("DELETE FROM market_rules")
    con.execute("DELETE FROM price_snapshots")
    con.commit()
    con.close()
    assert production_coverage_problems(db) == []
