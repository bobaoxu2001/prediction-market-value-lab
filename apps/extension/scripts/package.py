"""Create the public, deterministic developer-mode beta archive."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path


EXTENSION_DIR = Path(__file__).resolve().parents[1]
OUTPUT = EXTENSION_DIR.parent / "web" / "public" / "downloads" / "pmvl-entry-cost-beta.zip"
FILES = (
    "manifest.json",
    "onboarding.html",
    "onboarding.css",
    "overlay.css",
    "icons/icon16.png",
    "icons/icon32.png",
    "icons/icon48.png",
    "icons/icon128.png",
    "dist/background.js",
    "dist/content.js",
    "README.md",
)


def main() -> None:
    missing = [name for name in FILES if not (EXTENSION_DIR / name).is_file()]
    if missing:
        raise SystemExit(f"refusing to package missing files: {', '.join(missing)}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            source = EXTENSION_DIR / name
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 13, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())

    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")
    print(f"sha256 {digest}")


if __name__ == "__main__":
    main()
