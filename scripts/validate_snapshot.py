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
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(SNAPSHOT.relative_to(ROOT))],
        cwd=ROOT, capture_output=True, text=True,
    )
    if tracked.returncode != 0:
        problems.append("snapshot is not tracked by git, so a deploy bundle will omit it")

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
    except Exception as exc:  # noqa: BLE001
        problems.append(f"API could not serve the snapshot: {type(exc).__name__}: {exc}")

    # Quote coherence: whatever the API serves as a market's price must match the
    # order book it also serves for that market. Three different prices for one
    # contract on one page is not a crash, so nothing else here would catch it.
    try:
        from pmvl_shared.db import session_scope
        from pmvl_markets.db_models import Market

        from pmvl_api.quotes import coherent_quote

        with session_scope() as session:
            checked = incoherent = 0
            for market in session.scalars(select_markets_with_books()).all():
                quote = coherent_quote(session, market)
                if quote.source != "orderbook":
                    continue
                checked += 1
                response = client.get(f"/markets/{market.id}")
                if response.status_code != 200:
                    continue
                payload = response.json()["data"]
                served = payload["market"].get("best_yes_ask")
                book = (payload.get("orderbook") or {}).get("yes_asks") or []
                if not book:
                    continue
                if served is not None and str(served) != str(book[0]["price"]):
                    incoherent += 1
                    if incoherent <= 3:
                        problems.append(
                            f"market {market.id}: served ask {served} != book ask "
                            f"{book[0]['price']}"
                        )
            if incoherent:
                problems.append(
                    f"{incoherent} of {checked} markets serve a price that disagrees "
                    "with their own order book"
                )
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

    if problems:
        print("SNAPSHOT VALIDATION FAILED:")
        for problem in problems:
            print(f"   {problem}")
        return 1

    print(f"snapshot OK ({size / 1e6:.1f} MB, rollback journal, serves the API)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
