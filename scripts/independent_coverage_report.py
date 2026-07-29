"""How much of the market universe can produce an independent estimate at all.

The live pipeline produced zero recommendations. That is the gate working: without
an independent estimate, eligibility has no answer and fails closed rather than
recommending against a price the model partly copied.

Safe is not the same as useful. If almost no market can ever produce an
independent estimate, the product is permanently empty and the honest response is
to say so and name which models would change it - not to loosen the gate, which
would restore exactly the circularity the gate exists to prevent.

This measures rather than guesses. Run against any operational database:

    python scripts/independent_coverage_report.py --database <url> --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "packages/shared/src"),
    str(ROOT / "packages/market-normalization/src"),
]

#: The diagnostic a market carries when nothing independent could score it. Named
#: so the reason is greppable and reportable rather than an absence.
INDEPENDENT_MODEL_UNAVAILABLE = "independent_model_unavailable"


def build_report(session) -> dict[str, Any]:  # noqa: ANN001
    from sqlalchemy import select

    from pmvl_markets.db_models import Market, ModelPrediction
    from pmvl_markets.probability.independence import classify

    markets = list(session.scalars(select(Market)))
    latest: dict[int, ModelPrediction] = {}
    for prediction in session.scalars(
        select(ModelPrediction).order_by(ModelPrediction.created_at)
    ):
        latest[prediction.market_id] = prediction

    by_category: dict[str, Counter] = defaultdict(Counter)
    by_platform: dict[str, Counter] = defaultdict(Counter)
    by_horizon: dict[str, Counter] = defaultdict(Counter)
    component_hits: Counter = Counter()
    totals: Counter = Counter()

    for market in markets:
        prediction = latest.get(market.id)
        if prediction is None:
            bucket = "no_model_estimate"
        elif prediction.independent_probability is not None:
            bucket = "independent"
        else:
            bucket = "market_informed_only"

        totals[bucket] += 1
        by_category[market.category or "unknown"][bucket] += 1
        by_platform[market.platform][bucket] += 1
        by_horizon[_horizon(market)][bucket] += 1

        if prediction is not None and prediction.components:
            names = [
                c.get("name")
                for c in prediction.components
                if isinstance(c, dict) and c.get("probability") is not None
            ]
            for name in classify([n for n in names if n]).independent_names:
                component_hits[name] += 1

    total = len(markets) or 1
    # The ratio that answers the question. Dividing by every market conflates two
    # different facts - "the scorer has not reached this market yet" and "the
    # scorer reached it and found nothing independent" - and only the second says
    # anything about whether the gate is the binding constraint.
    scored = totals["independent"] + totals["market_informed_only"]
    return {
        "markets_examined": len(markets),
        "markets_scored": scored,
        "share_of_scored_with_independent_estimate": (
            round(totals["independent"] / scored, 4) if scored else None
        ),
        "share_with_independent_estimate": round(totals["independent"] / total, 4),
        "share_market_informed_only": round(totals["market_informed_only"] / total, 4),
        "share_with_no_model_estimate": round(totals["no_model_estimate"] / total, 4),
        # The number that decides whether the gate is the binding constraint.
        "share_blocked_specifically_by_independence": round(
            totals["market_informed_only"] / total, 4
        ),
        "counts": dict(totals),
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
        "by_platform": {k: dict(v) for k, v in sorted(by_platform.items())},
        "by_horizon": {k: dict(v) for k, v in sorted(by_horizon.items())},
        "independent_components_that_fired": dict(component_hits.most_common()),
        "diagnostic_reason": INDEPENDENT_MODEL_UNAVAILABLE,
        "caveat": (
            "share_with_* denominators are ALL markets, including ones the scorer "
            "has not reached. Use share_of_scored_* to judge the gate itself."
        ),
        "interpretation": (
            "A market in 'market_informed_only' has an estimate that used the "
            "target price, so it cannot demonstrate an edge against that price. "
            "Raising this share means adding genuinely independent models, not "
            "relaxing the gate - a cross-platform quote is not independent "
            "evidence, because the two venues are arbitraged against each other."
        ),
    }


def _horizon(market) -> str:  # noqa: ANN001
    from pmvl_shared.timeutil import hours_until

    hours = hours_until(market.expected_resolution_time)
    if hours is None:
        return "unknown"
    if hours <= 24:
        return "24h"
    if hours <= 24 * 7:
        return "7d"
    if hours <= 24 * 30:
        return "30d"
    return "beyond_30d"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None, help="SQLAlchemy URL")
    parser.add_argument("--json", default=None, help="write the report here")
    args = parser.parse_args(argv)

    import os

    if args.database:
        os.environ["DATABASE_URL"] = args.database

    from pmvl_shared.db import session_scope

    with session_scope() as session:
        report = build_report(session)

    print(json.dumps(report, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
