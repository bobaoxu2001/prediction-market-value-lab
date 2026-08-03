#!/usr/bin/env python
"""Generate a Founding Pilot report from a validated Snapshot.

    # Today's digest, blocking if the Snapshot is too old to send
    python scripts/generate_pilot_digest.py --out out/

    # A retrospective from an older Snapshot, stamped as historical
    python scripts/generate_pilot_digest.py --historical-sample --out out/

    # The weekly retrospective
    python scripts/generate_pilot_digest.py --weekly --out out/

Writes `.md`, `.html` and `.txt` side by side, plus a `.json` of the gate result
so a send script can decide what happened without re-parsing prose.

The exit code is the interesting part for automation:

    0  a report was produced (with or without candidates), or a historical sample
    3  publication was BLOCKED; a blocked report was still written and should still
       be sent, because a subscriber told nothing cannot distinguish "no
       opportunities" from "the pipeline broke"

A refusal is not an error. Subscribers are told the pipeline was not fresh
enough, which is information they are owed. Exiting non-zero distinguishes it for
a scheduler without suppressing the send.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "shared" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "market-normalization" / "src"))

from pmvl_markets.pilot.digest import MAX_CANDIDATES, build_daily_digest  # noqa: E402
from pmvl_markets.pilot.gate import evaluate  # noqa: E402
from pmvl_markets.pilot.outcomes import build_weekly_review  # noqa: E402
from pmvl_markets.pilot.render import (  # noqa: E402
    subject_line,
    to_html_email,
    to_markdown,
    to_text,
)

DEFAULT_MANIFEST = ROOT / "data" / "pmvl-snapshot.manifest.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--weekly", action="store_true", help="Weekly outcome review")
    parser.add_argument("--horizon", default="7d", choices=("24h", "7d", "30d"))
    parser.add_argument("--limit", type=int, default=MAX_CANDIDATES)
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO instant to evaluate freshness against. Defaults to now.",
    )
    parser.add_argument(
        "--historical-sample",
        action="store_true",
        help=(
            "Produce a retrospective from an older Snapshot, evaluated at its own "
            "cutoff and stamped 'Historical sample — not current market research' in "
            "every format. Never issues current research."
        ),
    )
    parser.add_argument("--name", default=None, help="Basename for the output files")
    return parser.parse_args(argv)


def _resolve_as_of(args: argparse.Namespace) -> datetime | None:
    if args.historical_sample:
        manifest = json.loads(args.manifest.read_text())
        raw = manifest.get("source_data_cutoff") or manifest.get(
            "freshest_quote_observed_at"
        )
        if not raw:
            raise SystemExit("Snapshot declares no cutoff; cannot use --historical-sample.")
        # A hair after the cutoff, so the report is evaluated as of the moment the
        # Snapshot became available rather than the instant of its last observation.
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    if args.as_of:
        return datetime.fromisoformat(args.as_of.replace("Z", "+00:00")).astimezone(timezone.utc)
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    gate = evaluate(
        args.manifest,
        as_of=_resolve_as_of(args),
        historical_mode=args.historical_sample,
    )

    if args.weekly:
        report = build_weekly_review(args.manifest, gate)
    else:
        report = build_daily_digest(
            args.manifest, gate, horizon=args.horizon, limit=args.limit
        )

    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.name or (
        f"{'weekly-review' if args.weekly else 'daily-digest'}-"
        f"{gate.as_of.strftime('%Y-%m-%d')}"
    )

    (args.out / f"{stem}.md").write_text(to_markdown(report))
    (args.out / f"{stem}.html").write_text(to_html_email(report))
    (args.out / f"{stem}.txt").write_text(to_text(report))
    (args.out / f"{stem}.json").write_text(
        json.dumps(
            {
                "kind": report.kind,
                "subject": subject_line(report),
                "headline": report.headline,
                "publication_allowed": gate.publication_allowed,
                "historical_sample": gate.historical_mode,
                "blocked_reason": gate.blocked_reason,
                "actionable_candidates": len(getattr(report, "candidates", []) or []),
                "watchlist_count": len(getattr(report, "watchlist", []) or []),
                "gate": gate.as_dict(),
            },
            indent=2,
        )
        + "\n"
    )

    print(f"{subject_line(report)}")
    print(f"  {report.headline}")
    print(f"  actionable_candidates = {len(getattr(report, 'candidates', []) or [])}")
    print(f"  publication_allowed   = {gate.publication_allowed}")
    print(f"  wrote {stem}.{{md,html,txt,json}} to {args.out}")

    if args.historical_sample:
        # A historical sample is a deliberate retrospective, not an attempt to
        # publish. It exits 0 because nothing went wrong; the warning banner in
        # every rendered format is what keeps it from being mistaken for current
        # research.
        print("  HISTORICAL SAMPLE — stamped in every format, not current research")
        return 0

    if not gate.publication_allowed:
        print("  BLOCKED — current actionable research was NOT generated:")
        for reason in gate.reasons:
            print(f"    - {reason}")
        print("  A blocked report was still written and should still be sent.")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
