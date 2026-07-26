"""Run the API and the Next.js dev server together.

Used by `make dev` and by the editor's preview launcher so a single command brings
up the whole stack.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def main() -> int:
    api = subprocess.Popen(
        [str(ROOT / ".venv/bin/uvicorn"), "pmvl_api.main:app", "--port", "8000"],
        cwd=ROOT,
    )
    web = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=ROOT / "apps/web",
        env={**os.environ, "NEXT_PUBLIC_API_BASE": "http://localhost:8000"},
    )

    def shutdown(*_args: object) -> None:
        for process in (web, api):
            if process.poll() is None:
                process.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        return web.wait()
    finally:
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
