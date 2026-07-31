"""Vercel serverless entrypoint for the read-only research API.

The API never writes, so a deployment ships a pre-built SQLite snapshot inside the
function bundle instead of provisioning a hosted database. The trade-off is that the
deployment is **frozen at build time** - see ``scripts/build_demo_snapshot.py`` and
the ``snapshot_mode`` flag on /system, which the UI renders as a banner.

If the snapshot is missing this module fails **loudly and immediately** with a message
naming the cause. It deliberately does not fall back to the default SQLite path: that
path is under the read-only bundle, so the fallback used to raise a bare OSError from
inside pydantic during import, and every request returned FUNCTION_INVOCATION_FAILED
with nothing in the response explaining why.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent

for package_dir in (
    "packages/shared/src",
    "packages/market-normalization/src",
    "services/api/src",
):
    path = str(ROOT / package_dir)
    if path not in sys.path:
        sys.path.insert(0, path)

SNAPSHOT = ROOT / "data" / "pmvl-snapshot.db"
SNAPSHOT_MANIFEST = ROOT / "data" / "pmvl-snapshot.manifest.json"

if not os.environ.get("DATABASE_URL"):
    try:
        from pmvl_shared.snapshot_artifact import (
            SnapshotArtifactError,
            resolve_snapshot_path,
        )

        resolved_snapshot = resolve_snapshot_path(
            SNAPSHOT_MANIFEST,
            SNAPSHOT,
        )
    except (SnapshotArtifactError, OSError) as exc:
        listing = sorted(p.name for p in (ROOT / "data").glob("*")) if (
            ROOT / "data"
        ).exists() else ["<no data/ directory in bundle>"]
        raise RuntimeError(
            "Deployment configuration error: the read-only database snapshot is "
            "missing, corrupt, or inconsistent with its manifest. Build and validate "
            "it before deploying, and make sure the declared artefact is not "
            "excluded by .gitignore or .vercelignore - the Vercel uploader honours "
            f".gitignore. Cause: {exc}. Contents of data/: {listing}"
        ) from exc
    # mode=ro is explicit: the bundle is read-only, and an accidental write should
    # fail here rather than half-succeed. immutable=1 is safe because resolution
    # already checked that the Snapshot is self-contained and has no WAL/SHM state.
    sqlite_path = quote(str(resolved_snapshot.resolve()), safe="/")
    os.environ["DATABASE_URL"] = (
        f"sqlite+pysqlite:///file:{sqlite_path}?mode=ro&immutable=1&uri=true"
    )

os.environ.setdefault("PMVL_SNAPSHOT_MODE", "1")

from pmvl_api.main import app  # noqa: E402

__all__ = ["app"]
