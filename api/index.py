"""Vercel serverless entrypoint for the read-only research API.

The API never writes, so a deployment can ship a pre-built SQLite snapshot inside the
bundle instead of talking to a hosted database. The trade-off is that the deployment
is **frozen at build time** - see ``scripts/build_snapshot.py`` and the
``snapshot_mode`` flag on /system, which the UI surfaces so nobody mistakes a
snapshot for live data.

The local packages are pure Python and are added to ``sys.path`` directly rather than
installed, because Vercel's Python runtime installs from requirements.txt only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for package_dir in (
    "packages/shared/src",
    "packages/market-normalization/src",
    "services/api/src",
):
    path = str(ROOT / package_dir)
    if path not in sys.path:
        sys.path.insert(0, path)

snapshot = ROOT / "data" / "pmvl-snapshot.db"
if snapshot.exists() and not os.environ.get("DATABASE_URL"):
    # mode=ro is explicit: the bundle is read-only and an accidental write should
    # fail loudly here rather than corrupt a half-written page.
    os.environ["DATABASE_URL"] = (
        f"sqlite+pysqlite:///file:{snapshot}?mode=ro&uri=true"
    )
os.environ.setdefault("PMVL_SNAPSHOT_MODE", "1")

from pmvl_api.main import app  # noqa: E402

__all__ = ["app"]
