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


def main(argv: list[str] | None = None) -> int:
    # Paths are overridable so the pipeline can build a CANDIDATE in a temporary
    # directory. Building straight onto the published path would mean a failed
    # validation had already replaced the artefact it was meant to gate.
    import argparse

    global SOURCE, TARGET, MANIFEST
    parser = argparse.ArgumentParser(description="Build a read-only snapshot")
    parser.add_argument("--source", default=None)
    parser.add_argument("--target", default=None)
    args = parser.parse_args(argv)
    if args.source:
        SOURCE = Path(args.source)
    if args.target:
        TARGET = Path(args.target)
        MANIFEST = TARGET.with_suffix(".manifest.json")

    if not SOURCE.exists():
        print(f"missing {SOURCE}; run `make ingest && make rank` first", file=sys.stderr)
        return 1

    if TARGET.exists():
        TARGET.unlink()

    # SQLite's backup API rather than shutil.copy2.
    #
    # A raw file copy takes the main .db and leaves the -wal behind, so every
    # transaction committed since the last automatic checkpoint is missing from
    # the copy. That produced a published candidate containing `settle` still
    # marked RUNNING while the run had finished it successfully - and which writes
    # survived depended on how many pages happened to be written afterwards.
    #
    # backup() reads through SQLite itself, so it sees committed data wherever it
    # physically lives, and the destination is a single self-contained file with
    # no sidecars. The pipeline also finalises the source first; this is the
    # second line of defence, and the one that holds even if a future caller
    # forgets.
    source = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    destination = sqlite3.connect(TARGET)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()

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

    # A prediction on a market that has already settled is never recomputed:
    # scoring only visits open markets, so the row survives every later run
    # unchanged, still carrying whatever the model said the last time the market
    # was live. After the touch-formula and band-parsing fixes, the only estimates
    # left in the artefact that disagreed wildly with their market were exactly
    # these - "reach $67,500" at 0.94 against a market at 0.0045, computed by a
    # formula that no longer exists in the tree the manifest names.
    #
    # They cannot be acted on and they are not evidence of anything: the model
    # that produced them is gone. Predictions a recommendation points at are kept
    # regardless, because the track record and case study resolve through
    # `recommendations.prediction_id` and that audit trail must not lose its
    # subject.
    #
    # `settled`/`closed` are named rather than `status != 'open'`: the vocabulary
    # also contains `paused` and `unknown`, and a market whose status could not be
    # read is not one whose estimate should be silently discarded.
    cur.execute(
        """
        DELETE FROM model_predictions
        WHERE market_id IN (
                SELECT id FROM markets WHERE status IN ('settled', 'closed')
            )
          AND id NOT IN (
              SELECT prediction_id FROM recommendations WHERE prediction_id IS NOT NULL
          )
        """
    )

    # What a market needs to earn a place in the artefact.
    #
    # This used to be "has a book, a prediction, a recommendation or a
    # settlement". Books are capped at a few hundred per cycle, and predictions
    # only exist where a book does, so the rule reduced ~8,000 ingested markets to
    # ~340 and the public browser showed 4% of either venue. Someone looking up a
    # contract they hold almost never found it, which is a poor reason to lose a
    # reader.
    #
    # An open market carrying a quote can now render something useful without a
    # book: execution cost needs no probability, and fees, tick rounding, transfer
    # and capital cost are exact from the market row alone. Depth impact is the
    # only component that needs a ladder, and its absence is already reported
    # rather than assumed to be zero. So the quote itself is the admission test.
    #
    # Bounded three ways: open only, quoted only, and traded in the last 24 hours.
    #
    # The volume floor is what makes this fit. Of 11,849 open quoted markets on the
    # run that sized this, 9,450 - eighty per cent - had no 24h volume at all, and
    # carrying them cost about 21 MB against a 40 MB ceiling. A quoted contract
    # nobody has traded today is not one a reader is looking up; it is a venue
    # leaving a board open. Keeping the 2,386 that did trade takes the browsable
    # universe from ~340 to ~2,400 for roughly 5 MB.
    #
    # `volume_24h` is a Money column, stored as TEXT. Comparing it to a number
    # without the cast is not a tighter filter, it is no filter: SQLite orders
    # every TEXT value above every INTEGER, so `volume_24h >= 5000` selected all
    # 11,849 rows including the zero-volume ones.
    cur.execute(
        """
        DELETE FROM markets WHERE id NOT IN (
            SELECT market_id FROM orderbook_snapshots
            UNION SELECT market_id FROM model_predictions
            UNION SELECT market_id FROM recommendations
            UNION SELECT market_id FROM settlements WHERE market_id IS NOT NULL
            UNION SELECT id FROM markets
                WHERE status = 'open'
                  AND accepting_orders = 1
                  AND (best_yes_ask IS NOT NULL OR best_no_ask IS NOT NULL)
                  AND CAST(COALESCE(volume_24h, '0') AS REAL) > 0
        )
        """
    )
    cur.execute("DELETE FROM outcomes WHERE market_id NOT IN (SELECT id FROM markets)")
    cur.execute("DELETE FROM market_rules WHERE market_id NOT IN (SELECT id FROM markets)")
    cur.execute("DELETE FROM trades WHERE market_id NOT IN (SELECT id FROM markets)")
    cur.execute("DELETE FROM price_snapshots WHERE market_id NOT IN (SELECT id FROM markets)")
    # Rule versions are recorded for the ENTIRE ingested universe - one row per
    # market ever seen - while the prune above keeps roughly a quarter of it. This
    # cascade was missing, so every run left another ~6,000 rule versions pointing
    # at markets that are not in the artefact. They are the widest rows in the
    # schema and nothing can read them: no endpoint can query a market the
    # snapshot does not contain. By 2026-07-31 they were 9.75 MB of a 10.15 MB
    # table, and the artefact crossed the size ceiling and stopped validating.
    cur.execute(
        "DELETE FROM market_rule_versions WHERE market_id NOT IN (SELECT id FROM markets)"
    )

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
            # `fetchone()` returns None for an empty result, and subscripting that
            # raises TypeError - which the sqlite3.Error guard below never caught.
            # A run whose scoring produced nothing therefore crashed the builder
            # here rather than writing a manifest saying so.
            try:
                row = con.execute(sql).fetchone()
            except _sqlite3.Error:
                return None
            return row[0] if row else None

        schema_version = scalar("SELECT version_num FROM alembic_version") or "unknown"
        model_version = scalar(
            "SELECT model_version FROM model_predictions "
            "ORDER BY created_at DESC LIMIT 1"
        ) or "unknown"
        jobs = {}
        try:
            for name, status in con.execute(
                """
                WITH ranked AS (
                    SELECT
                        job_name,
                        status,
                        ROW_NUMBER() OVER (
                            PARTITION BY job_name
                            ORDER BY started_at DESC, id DESC
                        ) AS position
                    FROM job_runs
                )
                SELECT job_name, status
                FROM ranked
                WHERE position = 1
                ORDER BY job_name
                """
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

    from pmvl_markets.matching.rule_history import PARSER_VERSION

    commit = code_version()
    # A deterministic id: same commit + same source cutoff => same id. A wall-clock
    # component would make two artefacts built from identical inputs look
    # different, which defeats the point of an id you can compare.
    snapshot_id = f"{commit}-{(freshest or 'no-cutoff')}".replace(" ", "T")[:64]
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        code_commit_sha=commit,
        schema_version=str(schema_version),
        model_version=str(model_version),
        parser_version=PARSER_VERSION,
        source_data_cutoff=parse_ts(freshest),
        freshest_quote_observed_at=parse_ts(freshest),
        oldest_quote_observed_at=parse_ts(oldest),
        row_counts={t: n for t, n in counts.items() if n},
        job_statuses=jobs,
        file_size_bytes=size,
        sha256=sha256_of(TARGET),
        artifact_format="sqlite",
        artifact_encoding="raw",
        uncompressed_sha256=sha256_of(TARGET),
        uncompressed_size_bytes=size,
        schema_revision=str(schema_version),
        source_commit_sha=commit,
    )
    manifest.write(MANIFEST)


if __name__ == "__main__":
    raise SystemExit(main())
