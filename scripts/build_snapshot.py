"""Build a slim, read-only database snapshot for a serverless deployment.

The research API is read-only, so a deployment can ship a pre-built database rather
than talk to a hosted one. What it CANNOT do is stay current: the result is frozen at
build time, and the deployed site must say so. See `snapshot_meta` below, which the
/system endpoint surfaces.

Pruning targets the bulk without touching anything the UI reads:

* ``raw_payload`` on markets and events (~56 MB) - kept locally for debugging
  provider field changes, never read by an endpoint.
* Historical orderbook levels beyond the latest snapshot per market - the detail page
  only renders the most recent book.
* Superseded model predictions - only the newest per market is displayed.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "pmvl.db"
TARGET = ROOT / "data" / "pmvl-snapshot.db"
MANIFEST = ROOT / "data" / "pmvl-snapshot.manifest.json"

sys.path[:0] = [
    str(ROOT / "packages/shared/src"),
    str(ROOT / "packages/market-normalization/src"),
]


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}; run `make ingest && make rank` first", file=sys.stderr)
        return 1

    if TARGET.exists():
        TARGET.unlink()
    shutil.copy2(SOURCE, TARGET)
    before = TARGET.stat().st_size

    con = sqlite3.connect(TARGET)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=DELETE")

    # Raw provider payloads: large, and no endpoint reads them.
    cur.execute("UPDATE markets SET raw_payload = NULL")
    cur.execute("UPDATE events SET raw_payload = NULL")
    cur.execute("UPDATE orderbook_snapshots SET raw_payload = NULL")
    cur.execute("UPDATE settlements SET raw_payload = NULL")

    # Only the latest orderbook per market is rendered.
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

    # Only the newest prediction per market is displayed.
    cur.execute(
        """
        DELETE FROM model_predictions WHERE id NOT IN (
            SELECT MAX(id) FROM model_predictions GROUP BY market_id
        )
        """
    )

    # Markets with neither a book nor a prediction cannot render anything useful,
    # and the browser paginates over what remains.
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
    cur.execute("DELETE FROM outcomes WHERE market_id NOT IN (SELECT id FROM markets)")
    cur.execute("DELETE FROM market_rules WHERE market_id NOT IN (SELECT id FROM markets)")
    cur.execute("DELETE FROM trades WHERE market_id NOT IN (SELECT id FROM markets)")
    cur.execute("DELETE FROM price_snapshots WHERE market_id NOT IN (SELECT id FROM markets)")

    con.commit()
    cur.execute("VACUUM")
    con.commit()

    counts = {}
    for (table,) in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        counts[table] = cur.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    con.close()

    after = TARGET.stat().st_size
    _write_manifest(counts, after)

    print(f"snapshot: {before/1e6:.1f} MB -> {after/1e6:.1f} MB  ({TARGET})")
    for table, n in counts.items():
        if n:
            print(f"   {table:28s} {n:>7,}")
    print(f"manifest: {MANIFEST.name}")
    return 0


def _write_manifest(counts: dict[str, int], size: int) -> None:
    """Record what this artefact is, so a deploy can check rather than assume.

    Written with validation_status PENDING and release_status HELD. The validator
    promotes it on success; nothing else may. An artefact whose manifest still
    says PENDING has not been checked, and `verify_artifact` refuses it - which is
    the behaviour that makes "never deploy an unvalidated snapshot" enforceable
    rather than a convention.
    """
    import sqlite3 as _sqlite3

    from pmvl_shared.job_record import code_version
    from pmvl_shared.manifest import SnapshotManifest, sha256_of

    con = _sqlite3.connect(TARGET)
    try:
        def scalar(sql: str):  # noqa: ANN202
            try:
                return con.execute(sql).fetchone()[0]
            except _sqlite3.Error:
                return None

        schema_version = scalar("SELECT version_num FROM alembic_version") or "unknown"
        model_version = scalar(
            "SELECT model_version FROM model_predictions "
            "ORDER BY created_at DESC LIMIT 1"
        ) or "unknown"
        jobs = {}
        try:
            for name, status in con.execute(
                "SELECT job_name, status FROM job_runs "
                "WHERE (job_name, started_at) IN "
                "(SELECT job_name, MAX(started_at) FROM job_runs GROUP BY job_name)"
            ):
                jobs[name] = status
        except _sqlite3.Error:
            pass
        freshest = scalar("SELECT MAX(quote_observed_at) FROM markets")
        oldest = scalar(
            "SELECT MIN(quote_observed_at) FROM markets WHERE quote_observed_at IS NOT NULL"
        )
    finally:
        con.close()

    from pmvl_shared.timeutil import parse_ts

    manifest = SnapshotManifest(
        snapshot_id=f"{code_version()}-{int(TARGET.stat().st_mtime)}",
        code_commit_sha=code_version(),
        schema_version=str(schema_version),
        model_version=str(model_version),
        # The rule parser has no version of its own yet; saying so beats inventing
        # a number that implies reproducibility we cannot offer.
        parser_version="unversioned",
        source_data_cutoff=parse_ts(freshest),
        freshest_quote_observed_at=parse_ts(freshest),
        oldest_quote_observed_at=parse_ts(oldest),
        row_counts={t: n for t, n in counts.items() if n},
        job_statuses=jobs,
        file_size_bytes=size,
        sha256=sha256_of(TARGET),
    )
    manifest.write(MANIFEST)


if __name__ == "__main__":
    raise SystemExit(main())
