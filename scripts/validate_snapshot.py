"""Validate the committed database snapshot.

Three separate failures have taken this deployment down, and none of them was caught
by a green build:

1. the snapshot was missing from the function bundle (gitignored),
2. the snapshot was in WAL mode, which cannot be opened read-only on a serverless
   mount because SQLite wants to create a -shm sidecar,
3. the snapshot was present and openable but could not answer the API's queries.

This checks all three, plus the size ceiling, so CI fails instead of production.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "pmvl-snapshot.db"

#: GitHub warns above 50MB and Vercel function bundles are capped well below that.
MAX_BYTES = 40 * 1024 * 1024


def _restore_rollback_journal() -> None:
    """Put the file back into rollback-journal mode and drop any sidecars."""
    try:
        from pmvl_shared.db import get_engine

        get_engine().dispose()
    except Exception:  # noqa: BLE001
        pass
    con = sqlite3.connect(SNAPSHOT)
    con.execute("PRAGMA journal_mode=DELETE")
    con.commit()
    con.close()
    for suffix in ("-wal", "-shm"):
        sidecar = SNAPSHOT.with_name(SNAPSHOT.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def select_markets_with_books():  # noqa: ANN201
    """Markets that have at least one captured order book."""
    from sqlalchemy import select

    from pmvl_markets.db_models import Market, OrderbookSnapshot

    return (
        select(Market)
        .join(OrderbookSnapshot, OrderbookSnapshot.market_id == Market.id)
        .distinct()
        .limit(120)
    )


def main() -> int:
    # Overridable so a CANDIDATE artefact can be validated in place before it is
    # promoted. Validation that could only run against the published path would
    # have to publish first and check afterwards, which is the wrong order.
    import argparse

    global SNAPSHOT
    parser = argparse.ArgumentParser(description="Validate a snapshot artifact")
    parser.add_argument("--snapshot", default=None)
    args = parser.parse_args()
    if args.snapshot:
        SNAPSHOT = Path(args.snapshot)

    problems: list[str] = []

    if not SNAPSHOT.exists():
        print(f"FAIL: {SNAPSHOT} is missing. The API cannot start without it.")
        return 1

    size = SNAPSHOT.stat().st_size
    if size > MAX_BYTES:
        problems.append(f"snapshot is {size / 1e6:.1f} MB, above the {MAX_BYTES / 1e6:.0f} MB ceiling")

    header = SNAPSHOT.open("rb").read(20)
    if len(header) < 20:
        problems.append("snapshot header is truncated")
    elif header[18] == 2 or header[19] == 2:
        problems.append(
            "snapshot is in WAL mode; it cannot be opened read-only on a serverless "
            "mount (SQLite would need to create a -shm sidecar)"
        )

    # It must be tracked, or the deploy bundle will not contain it.
    #
    # Only meaningful for the PUBLISHED artefact. A candidate under validation
    # lives in a temporary directory by design - it is not tracked precisely
    # because it has not been promoted yet, and failing it here would make the
    # validation gate unusable for the thing it is supposed to gate.
    if _is_published_path():
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(SNAPSHOT.relative_to(ROOT))],
            cwd=ROOT, capture_output=True, text=True,
        )
        if tracked.returncode != 0:
            problems.append(
                "snapshot is not tracked by git, so a deploy bundle will omit it"
            )

    # It must open read-only exactly as the serverless function opens it.
    try:
        uri = f"file:{SNAPSHOT}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.execute("SELECT COUNT(*) FROM recommendation_snapshots").fetchone()
        con.close()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"snapshot cannot be opened read-only: {exc}")

    # It must actually answer the queries the API makes.
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///file:{SNAPSHOT}?mode=ro&uri=true"
    os.environ["PMVL_SNAPSHOT_MODE"] = "1"
    for package_dir in (
        "packages/shared/src", "packages/market-normalization/src", "services/api/src",
    ):
        sys.path.insert(0, str(ROOT / package_dir))
    try:
        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        client = TestClient(app)
        for route, key in (
            ("/health", None),
            ("/system", "data"),
            ("/backtest?mode=demo", "data"),
            ("/track-record?mode=demo&limit=3", "data"),
            ("/case-study?mode=demo", "data"),
            ("/arbitrage?view=actionable", None),
            ("/markets?limit=3", "data"),
        ):
            response = client.get(route)
            if response.status_code != 200:
                problems.append(f"{route} -> HTTP {response.status_code}")
            elif key and not response.json().get(key):
                problems.append(f"{route} -> 200 but '{key}' is empty")

        # The snapshot's timestamps must reach the deployment already separated.
        # Collapsing them produced a banner that reported the single freshest
        # observation as the capture time for all 1850 markets.
        timing = client.get("/system").json().get("data", {}).get("snapshot_timing")
        if not timing:
            problems.append("/system -> no snapshot_timing block")
        else:
            for field in (
                "market_ingest_started_at",
                "market_ingest_finished_at",
                "freshest_quote_observed_at",
                "oldest_quote_observed_at",
                "median_quote_observed_at",
                "arbitrage_scan_at",
            ):
                if field not in timing:
                    problems.append(f"/system snapshot_timing -> missing {field}")
            freshest = timing.get("freshest_quote_observed_at")
            oldest = timing.get("oldest_quote_observed_at")
            if freshest and oldest and freshest == oldest:
                problems.append(
                    "/system snapshot_timing -> freshest equals oldest quote; the "
                    "spread that makes a single capture time misleading is missing"
                )
            # Never approximated from a value that happens to be recorded.
            for absent in ("snapshot_artifact_built_at", "deployment_created_at"):
                if timing.get(absent) is not None:
                    problems.append(
                        f"/system snapshot_timing -> {absent} is set, but nothing "
                        "records it; it must stay null with a stated reason"
                    )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"API could not serve the snapshot: {type(exc).__name__}: {exc}")

    # Quote coherence. Every displayed price, the spread, the depth and the
    # timestamp must come from ONE observation. Checking only the first YES ask
    # would miss a response that mixes a book ask with a summary bid, which reads
    # as a plausible spread and is entirely fictional.
    try:
        from decimal import Decimal

        from pmvl_shared.db import session_scope
        from pmvl_markets.db_models import Market

        from pmvl_api.quotes import coherent_quote

        def dec(value):
            return None if value is None else Decimal(str(value))

        with session_scope() as session:
            checked = 0
            failures: list[str] = []
            for market in session.scalars(select_markets_with_books()).all():
                resolved = coherent_quote(session, market)
                response = client.get(f"/markets/{market.id}")
                if response.status_code != 200:
                    continue
                payload = response.json()["data"]
                served, book = payload["market"], payload.get("orderbook") or {}
                served_quote = payload.get("quote") or {}
                checked += 1

                def top(side: str, kind: str, source=book):
                    levels = source.get(f"{side}_{kind}") or []
                    return dec(levels[0]["price"]) if levels else None

                # 1. The response must say where its numbers came from.
                source = served_quote.get("source")
                if source not in ("orderbook", "venue_summary", "none"):
                    failures.append(f"market {market.id}: quote source {source!r}")
                    continue

                if source == "orderbook":
                    # 2. Every side must match the book it claims to come from.
                    for side, kind, field in (
                        ("yes", "asks", "best_yes_ask"),
                        ("yes", "bids", "best_yes_bid"),
                        ("no", "asks", "best_no_ask"),
                        ("no", "bids", "best_no_bid"),
                    ):
                        expected, actual = top(side, kind), dec(served.get(field))
                        if expected is not None and actual is not None and expected != actual:
                            failures.append(
                                f"market {market.id}: {field} {actual} != book {expected}"
                            )
                    # 3. A market claiming order-book pricing while a newer book
                    #    exists unused would be a resolver bug.
                    if resolved.source != "orderbook":
                        failures.append(
                            f"market {market.id}: served orderbook pricing but the "
                            f"resolver chose {resolved.source}"
                        )
                elif source == "venue_summary" and resolved.source == "orderbook":
                    failures.append(
                        f"market {market.id}: served a venue summary while a usable "
                        "order book exists"
                    )

                # 4. Spread must be derived from the displayed bid and ask, not
                #    carried over from a different observation.
                bid, ask, spread = (
                    dec(served.get("best_yes_bid")),
                    dec(served.get("best_yes_ask")),
                    dec(served.get("spread")),
                )
                if None not in (bid, ask, spread) and (ask - bid) != spread:
                    failures.append(
                        f"market {market.id}: spread {spread} != ask {ask} - bid {bid}"
                    )
                # A spread with a side missing has nothing to measure from. This
                # case used to be skipped because the check required all three to
                # be present, which is exactly how the venue-summary path shipped
                # "YES BID -, YES ASK 0.1c, SPREAD 0.1c".
                if spread is not None and (bid is None or ask is None):
                    failures.append(
                        f"market {market.id}: spread {spread} shown with "
                        f"bid={bid} ask={ask}; one side is missing"
                    )

                # 5. The timestamp must belong to the source that supplied the prices.
                if source == "orderbook":
                    if served.get("quote_observed_at") != (book.get("observed_at")):
                        failures.append(
                            f"market {market.id}: quote timestamp does not match the "
                            "order book it was priced from"
                        )

                # 6. Depth must be labelled for the definition it uses.
                if source == "orderbook" and "yes_ask_depth_usd" not in served:
                    failures.append(f"market {market.id}: ask depth is not named explicitly")

            problems.extend(failures[:5])
            if len(failures) > 5:
                problems.append(f"...and {len(failures) - 5} further quote-coherence failures")
            if not failures:
                print(f"   quote coherence: {checked} markets consistent")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"quote-coherence check failed: {type(exc).__name__}: {exc}")

    # If an arbitrage scan is recorded, its demotion histogram must be servable.
    # The histogram is the only thing that distinguishes "these venues genuinely do
    # not list equivalent contracts" from "the matcher is broken", and it lived in a
    # table the snapshot builder was deleting wholesale.
    try:
        response = client.get("/arbitrage")
        if response.status_code == 200:
            body = response.json()
            has_scan = bool(body.get("batch_id"))
            diagnostics = body.get("matching_diagnostics")
            if has_scan and not diagnostics:
                problems.append(
                    "an arbitrage scan is recorded but matching_diagnostics is null; "
                    "the demotion histogram did not survive into the snapshot"
                )
            elif diagnostics and not diagnostics.get("pairs_examined"):
                problems.append("matching_diagnostics present but pairs_examined is 0")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"diagnostics check failed: {type(exc).__name__}: {exc}")

    # Opening the snapshot through SQLAlchemy sets journal_mode=WAL, so simply
    # RUNNING this validator used to leave the artefact unopenable read-only - the
    # check corrupting the thing it checks. Restore it, then re-assert from the file
    # header so a genuine WAL commit is still caught above.
    _restore_rollback_journal()
    header = SNAPSHOT.open("rb").read(20)
    if header[18] == 2 or header[19] == 2:
        problems.append("snapshot is still in WAL mode after the restore attempt")

    # The manifest is promoted here and nowhere else. An artefact whose manifest
    # still reads PENDING has not been checked, and `verify_artifact` refuses it -
    # which is what turns "never deploy an unvalidated snapshot" from a convention
    # into something a deploy step can enforce.
    #
    # The checksum is recomputed AFTER the journal-mode restore above, because that
    # rewrites the file. Hashing before it would record a digest of bytes that no
    # longer exist, and every later verification would fail for the wrong reason.
    _finalise_manifest(problems)

    if problems:
        print("SNAPSHOT VALIDATION FAILED:")
        for problem in problems:
            print(f"   {problem}")
        return 1

    print(f"snapshot OK ({size / 1e6:.1f} MB, rollback journal, serves the API)")
    return 0


def _is_published_path() -> bool:
    """Whether SNAPSHOT points at the committed artefact rather than a candidate."""
    return SNAPSHOT == ROOT / "data" / "pmvl-snapshot.db"


def _finalise_manifest(problems: list[str]) -> None:
    """Record the verdict on the manifest, or say plainly that there is none."""
    # Derived from the artefact under validation, not hardcoded: a candidate is
    # named candidate.db and its manifest is candidate.manifest.json.
    manifest_path = SNAPSHOT.with_suffix(".manifest.json")
    if not manifest_path.exists():
        # Not a validation failure on its own: an artefact built before manifests
        # existed is still serviceable. But the absence is stated rather than
        # passed over, because a deploy that checks the manifest needs to know.
        print("   note: no manifest found; run scripts/build_snapshot.py to create one")
        return

    sys.path.insert(0, str(ROOT / "packages/shared/src"))
    from pmvl_shared.manifest import (
        ReleaseStatus,
        ValidationStatus,
        provenance_problems,
        sha256_of,
    )

    data = json.loads(manifest_path.read_text())

    # A newly generated artefact must be attributable. The committed rollback
    # snapshot predates commit and parser recording, so it carries a documented
    # exemption rather than being retro-labelled with a fabricated SHA.
    legacy = bool(data.get("legacy_provenance_exemption"))
    provenance = provenance_problems(data, legacy_exempt=legacy)
    if provenance:
        problems.extend(provenance)
    elif legacy:
        print("   note: legacy provenance exemption applies to this artifact")

    passed = not problems
    data["validation_status"] = (
        ValidationStatus.PASSED if passed else ValidationStatus.FAILED
    )
    data["release_status"] = ReleaseStatus.PUBLISHED if passed else ReleaseStatus.HELD
    data["validation_failures"] = problems[:20]
    data["sha256"] = sha256_of(SNAPSHOT)
    data["file_size_bytes"] = SNAPSHOT.stat().st_size
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"   manifest: {data['validation_status']} / {data['release_status']}")


if __name__ == "__main__":
    raise SystemExit(main())
