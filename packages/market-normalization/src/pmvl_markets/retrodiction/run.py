"""Top-level entry point: sample settled markets, replay the models, report.

Nothing here is written to the database. That is deliberate for now - a
retrodiction result is a measurement taken against live endpoints at a moment in
time, and persisting it beside the snapshot-derived tables would make it one query
away from being pooled into a figure that claims the backtest's guarantees. It is
returned, printed and written to a file the reader chooses.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy.orm import Session

from pmvl_shared.enums import Platform
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import iso, utcnow

from ..providers.kalshi import KalshiProvider
from ..providers.polymarket import PolymarketProvider
from .harness import (
    DEFAULT_LEAD_TIMES,
    ProviderPriceHistory,
    RetrodictionHarness,
    RetrodictionResult,
)
from .sampling import SamplingCriteria, describe_sample, load_settled_markets

log = get_logger(__name__)


async def run_retrodiction(
    session: Session,
    *,
    criteria: SamplingCriteria | None = None,
    lead_times: Sequence[timedelta] = DEFAULT_LEAD_TIMES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sample, replay, and return the full report."""
    criteria = criteria or SamplingCriteria()
    now = now or utcnow()

    settled = load_settled_markets(session, criteria=criteria, now=now)
    sample = describe_sample(settled, criteria)

    if not settled:
        return {
            "sample": sample,
            "result": RetrodictionResult(generated_at=now).as_report(),
            "note": (
                "No settled markets matched the criteria. This is the expected "
                "result until `pmvl settle` has recorded resolutions for markets "
                "the platform has ingested."
            ),
        }

    platforms = {item.market.platform for item in settled}
    providers: dict[Platform, Any] = {}
    if Platform.KALSHI in platforms:
        providers[Platform.KALSHI] = KalshiProvider()
    if Platform.POLYMARKET in platforms:
        providers[Platform.POLYMARKET] = PolymarketProvider()

    harness = RetrodictionHarness(
        ProviderPriceHistory(providers), lead_times=lead_times
    )
    try:
        result = await harness.run(settled)
    finally:
        await harness.aclose()
        for provider in providers.values():
            await provider.aclose()

    report = result.as_report()
    return {
        "sample": sample,
        "result": report,
        "interpretation": _interpret(report),
        "generated_at": iso(now),
    }


def _interpret(report: dict[str, Any]) -> str:
    """One sentence a reader can act on, including when the answer is bad news.

    Written here rather than in the UI so the CLI, the API and any page all say the
    same thing about the same number. The negative branch is the important one: a
    model that loses to the market has to be as easy to read as one that wins.
    """
    n = report.get("n_scored_against_market") or 0
    improvement = report.get("brier_improvement_vs_market")

    if n == 0:
        return (
            "No forecast could be scored against a market price. Nothing is claimed."
        )
    if improvement is None:
        return f"{n} forecasts made, but no Brier comparison was possible."

    sample_caveat = (
        " The sample is small enough that this could easily reverse."
        if n < 100
        else ""
    )
    if improvement > 0:
        return (
            f"Over {n} forecasts the independent estimate beat the market's own "
            f"price by {improvement:.4f} Brier.{sample_caveat}"
        )
    if improvement < 0:
        return (
            f"Over {n} forecasts the independent estimate was WORSE than the "
            f"market's own price by {abs(improvement):.4f} Brier. On this evidence "
            f"the model adds no information the price did not already contain."
            f"{sample_caveat}"
        )
    return f"Over {n} forecasts the model and the market scored identically."
