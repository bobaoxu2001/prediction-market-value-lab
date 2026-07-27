"""Build the deployable database snapshot, deterministically.

Why this exists separately from ``build_snapshot.py``
-----------------------------------------------------
``build_snapshot.py`` prunes an existing ``data/pmvl.db`` and exits if there isn't
one. That makes it useless in a deploy pipeline, which starts from a clean checkout
with no database at all - and a deploy that silently produces no snapshot is exactly
how production went down: the API bundle shipped without its database and every
request returned FUNCTION_INVOCATION_FAILED.

This script always produces a working snapshot from nothing:

1. create the full schema,
2. seed the synthetic demo history (fixed RNG seed, so identical every run),
3. optionally overlay real market data if ``data/pmvl.db`` happens to exist,
4. verify the result actually answers the queries the API makes.

Step 4 matters most. A snapshot that exists but returns nothing looks identical to a
broken deployment from the user's side.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_dir in ("packages/shared/src", "packages/market-normalization/src"):
    sys.path.insert(0, str(ROOT / package_dir))

LIVE_DB = ROOT / "data" / "pmvl.db"
TARGET = ROOT / "data" / "pmvl-snapshot.db"

#: Tables the API reads. Any of these being empty is worth reporting, though only
#: the demo-backed ones are hard requirements.
REQUIRED_NON_EMPTY = (
    "recommendation_snapshots",
    "backtest_runs",
    "model_versions",
)


def _prune(path: Path) -> None:
    """Drop everything the API never reads, then reclaim the space."""
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=DELETE")

    for table in ("markets", "events", "orderbook_snapshots", "settlements"):
        try:
            cur.execute(f"UPDATE {table} SET raw_payload = NULL")
        except sqlite3.OperationalError:
            pass

    cur.execute(
        """
        DELETE FROM orderbook_levels WHERE snapshot_id NOT IN (
            SELECT MAX(id) FROM orderbook_snapshots GROUP BY market_id
        )
        """
    )
    cur.execute(
        """
        DELETE FROM orderbook_snapshots WHERE id NOT IN (
            SELECT MAX(id) FROM orderbook_snapshots GROUP BY market_id
        )
        """
    )
    cur.execute(
        """
        DELETE FROM model_predictions WHERE id NOT IN (
            SELECT MAX(id) FROM model_predictions GROUP BY market_id
        )
        """
    )
    cur.execute(
        """
        DELETE FROM markets WHERE id NOT IN (
            SELECT market_id FROM orderbook_snapshots
            UNION SELECT market_id FROM model_predictions
            UNION SELECT market_id FROM recommendations
            UNION SELECT market_id FROM settlements WHERE market_id IS NOT NULL
        )
        """
    )
    for table, column in (
        ("outcomes", "market_id"),
        ("market_rules", "market_id"),
        ("trades", "market_id"),
        ("price_snapshots", "market_id"),
    ):
        cur.execute(f"DELETE FROM {table} WHERE {column} NOT IN (SELECT id FROM markets)")

    # Aggressive prune so the artefact is small enough to COMMIT.
    #
    # Committing it is what makes a git-triggered Vercel build produce a working
    # bundle. Leaving it gitignored meant every `git push` auto-deployed a snapshot-
    # less build that crashed on import, silently replacing a healthy CLI deploy -
    # which took production down three times.
    #
    # Everything dropped here is either a drill-down the hosted demo does not link to,
    # or depth beyond what the UI renders.
    cur.execute("DELETE FROM trades")
    cur.execute("DELETE FROM price_snapshots WHERE market_id NOT IN "
                "(SELECT market_id FROM recommendations)")
    # Keep only the top 5 levels per side - the detail page renders no more.
    cur.execute(
        """
        DELETE FROM orderbook_levels WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY snapshot_id, side, is_ask ORDER BY level_index
                ) AS rn FROM orderbook_levels
            ) WHERE rn <= 5
        )
        """
    )
    # Per-trade backtest rows back a drill-down endpoint the demo does not link to;
    # the aggregate metrics on the backtest page are stored on backtest_runs.
    cur.execute("DELETE FROM backtest_trades")
    cur.execute("DELETE FROM job_runs")

    # Full settlement-rule text is the single biggest remaining cost (some rules run
    # to thousands of characters). It is rendered only on the market detail page and
    # in the case study, both of which are reached through a recommendation, so it is
    # kept for those markets and dropped elsewhere. The browser list shows titles and
    # prices only.
    cur.execute(
        """
        UPDATE markets
           SET description = '',
               settlement_rules_raw = ''
         WHERE id NOT IN (
             SELECT market_id FROM recommendations
             UNION SELECT market_id FROM recommendation_snapshots
         )
        """
    )
    cur.execute(
        """
        DELETE FROM market_rules WHERE market_id NOT IN (
            SELECT market_id FROM recommendations
            UNION SELECT market_id FROM recommendation_snapshots
        )
        """
    )

    con.commit()
    cur.execute("VACUUM")
    con.commit()
    con.close()


def _force_rollback_journal(path: Path) -> None:
    """Put the database back into rollback-journal mode."""
    from pmvl_shared.db import get_engine

    try:
        get_engine().dispose()
    except Exception:  # noqa: BLE001
        pass
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.commit()
    con.close()
    for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()


def _assert_not_wal(path: Path) -> list[str]:
    """Read the file header directly; byte 18 is 2 for WAL, 1 for rollback journal."""
    header = path.open("rb").read(20)
    if len(header) < 20:
        return ["snapshot header is truncated"]
    if header[18] == 2 or header[19] == 2:
        return [
            "snapshot is in WAL mode; it cannot be opened read-only on a "
            "serverless mount (needs to create a -shm sidecar)"
        ]
    return []


def _verify(path: Path) -> list[str]:
    """Query the snapshot the way the API does. Returns a list of problems."""
    problems: list[str] = []
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///file:{path}?mode=ro&uri=true"
    os.environ["PMVL_SNAPSHOT_MODE"] = "1"

    sys.path.insert(0, str(ROOT / "services/api/src"))
    from pmvl_shared.db import reset_engine

    reset_engine()

    from fastapi.testclient import TestClient

    from pmvl_api.main import app

    client = TestClient(app)
    checks = (
        ("/health", None),
        ("/system", None),
        ("/methodology", None),
        ("/backtest?mode=demo", "data"),
        ("/track-record?mode=demo&limit=5", "data"),
        ("/opportunities?horizon=24h&mode=demo", None),
        ("/opportunities/disagreements?horizon=24h", None),
        ("/arbitrage", None),
        ("/markets?limit=5", None),
    )
    for route, must_be_non_empty in checks:
        response = client.get(route)
        if response.status_code != 200:
            problems.append(f"{route} -> HTTP {response.status_code}")
            continue
        if must_be_non_empty:
            payload = response.json().get(must_be_non_empty)
            if not payload:
                problems.append(f"{route} -> 200 but '{must_be_non_empty}' is empty")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=45, help="days of demo history")
    parser.add_argument("--per-day", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42, help="fixed for reproducibility")
    parser.add_argument(
        "--from-live",
        action="store_true",
        help="overlay real market data from data/pmvl.db when it exists",
    )
    args = parser.parse_args()

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        TARGET.unlink()

    use_live = args.from_live and LIVE_DB.exists()
    if use_live:
        print(f"seeding from live database {LIVE_DB}")
        shutil.copy2(LIVE_DB, TARGET)
    else:
        if args.from_live:
            print(f"note: {LIVE_DB} not found; building a demo-only snapshot")
        print("creating a fresh schema")

    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{TARGET}"
    os.environ["ALLOW_DEMO_DATA"] = "true"

    from pmvl_shared.config import reset_settings_cache
    from pmvl_shared.db import create_all, reset_engine, session_scope

    reset_settings_cache()
    reset_engine()
    create_all()

    from pmvl_markets.demo import seed_demo_history
    from pmvl_markets.value.pipeline import _ensure_model_version

    with session_scope() as session:
        # Register the model version row. Normally the ranking pipeline does this;
        # a demo-only snapshot never runs ranking, and the verification step below
        # treats an empty model_versions table as a broken snapshot.
        _ensure_model_version(session)

    with session_scope() as session:
        report = seed_demo_history(
            session, days=args.days, per_day=args.per_day, seed=args.seed
        )
    print(
        f"demo history: {report.recommendations} recommendations over {report.days} "
        f"days, win rate {report.win_rate:.1%}"
    )

    # The backtest reads settled snapshots, so it has to run after seeding for the
    # /backtest page to have anything to show.
    from pmvl_markets.backtest import run_backtest

    with session_scope() as session:
        runs = run_backtest(session)
    print(f"backtest: {len(runs)} strategy runs")

    # The SQLAlchemy engine holds the file open; pruning uses a raw sqlite3
    # connection and would otherwise hit "database is locked".
    from pmvl_shared.db import get_engine

    get_engine().dispose()

    before = TARGET.stat().st_size
    _prune(TARGET)
    after = TARGET.stat().st_size
    print(f"pruned: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")

    # Verification opens the file through SQLAlchemy, whose connect hook sets
    # journal_mode=WAL. That silently converts the snapshot back to WAL, and a WAL
    # database CANNOT be opened read-only without creating a -shm sidecar - which
    # fails on a read-only serverless mount. So the journal mode is forced back
    # AFTER verification, and asserted below.
    problems = _verify(TARGET)
    _force_rollback_journal(TARGET)
    problems += _assert_not_wal(TARGET)
    if problems:
        print("\nVERIFICATION FAILED - refusing to publish a broken snapshot:")
        for problem in problems:
            print(f"   {problem}")
        return 1

    con = sqlite3.connect(TARGET)
    counts = {
        table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for (table,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    }
    con.close()

    missing = [t for t in REQUIRED_NON_EMPTY if not counts.get(t)]
    if missing:
        print(f"\nVERIFICATION FAILED - empty required tables: {missing}")
        return 1

    print(f"\nsnapshot ready: {TARGET} ({after / 1e6:.1f} MB)")
    for table, n in counts.items():
        if n:
            print(f"   {table:28s} {n:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
