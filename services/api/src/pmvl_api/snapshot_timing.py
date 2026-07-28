"""The distinct timestamps a frozen snapshot has, kept distinct.

A snapshot deployment has at least five different times, and they are hours to
weeks apart:

``market_ingest_started_at`` / ``market_ingest_finished_at``
    When the ingest job ran. This is when the pipeline *asked* the venues for
    data, not when the venues observed it.
``freshest_quote_observed_at``
    The single most recent ``markets.quote_observed_at`` in the database. It
    describes **one** market, not the dataset.
``oldest_quote_observed_at`` / ``median_quote_observed_at``
    What the rest of the dataset actually looks like.
``arbitrage_scan_at``
    When the cross-platform scan ran over whatever was in the database then.

Collapsing these into one "snapshot timestamp" produced a real misstatement: the
banner reported the freshest quote as though every quote were captured then. On
the deployed artefact that is 12 markets out of 1850, while 1516 are more than a
day older and the oldest is six weeks old. A reader told "quotes captured Jul 28
08:07" would reasonably believe the prices in front of them were minutes old.

Two timestamps a reader might expect are deliberately absent rather than
approximated:

``snapshot_artifact_built_at``
    Not recorded anywhere. The ``snapshot`` job name belongs to the daily
    recommendation snapshot used by the track record, not to building this file,
    and a committed binary has no trustworthy mtime after checkout.
``deployment_created_at``
    Vercel exposes no creation time to the running process; only commit SHA,
    ref, URL and environment. It is readable from the Vercel API from outside.

Both are reported as ``None`` next to a note saying why. Guessing either one -
from file mtime, from the ingest time, or from the freshest quote - would be
inventing provenance, which is the failure this module exists to prevent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmvl_markets.db_models import JobRun, Market

#: Explicitly unavailable, with the reason. Reported so a reader can tell "not
#: recorded" apart from "recorded as null", which look identical in JSON.
UNAVAILABLE_NOTES: dict[str, str] = {
    "snapshot_artifact_built_at": (
        "no job writes the artefact build time, and a committed binary's mtime "
        "reflects checkout, not build. Not approximated from anything else."
    ),
    "deployment_created_at": (
        "not available to the running process; Vercel exposes only commit SHA, "
        "ref, URL and environment. Readable from the Vercel API externally."
    ),
}

#: The one sentence that has to travel with any capture timestamp.
QUOTE_SPREAD_NOTE = (
    "Quotes were captured per market over a range of times, not all at once. "
    "'freshest' is the single most recent observation in the database; most "
    "markets are older. Every market page shows its own quote age."
)


def _job_times(session: Session, job_name: str) -> tuple[datetime | None, datetime | None]:
    """Start and finish of the most recent run of ``job_name``."""
    row = session.scalar(
        select(JobRun)
        .where(JobRun.job_name == job_name)
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )
    return (row.started_at, row.finished_at) if row else (None, None)


def _median_quote_observed_at(session: Session) -> datetime | None:
    """Median of ``markets.quote_observed_at``.

    The median rather than the mean: quote ages are heavily skewed by a long
    tail of markets last seen weeks ago, and a mean would be dragged into a
    region where no actual observation sits.
    """
    total = session.scalar(
        select(func.count()).select_from(Market).where(Market.quote_observed_at.is_not(None))
    )
    if not total:
        return None
    return session.scalar(
        select(Market.quote_observed_at)
        .where(Market.quote_observed_at.is_not(None))
        .order_by(Market.quote_observed_at)
        .offset(total // 2)
        .limit(1)
    )


def snapshot_timing(session: Session) -> dict[str, Any]:
    """Every distinct time in the snapshot, each under its own name."""
    ingest_started, ingest_finished = _job_times(session, "ingest")
    arbitrage_started, _ = _job_times(session, "arbitrage")

    quote_filter = Market.quote_observed_at.is_not(None)
    freshest = session.scalar(select(func.max(Market.quote_observed_at)).where(quote_filter))
    oldest = session.scalar(select(func.min(Market.quote_observed_at)).where(quote_filter))

    return {
        "market_ingest_started_at": ingest_started,
        "market_ingest_finished_at": ingest_finished,
        "freshest_quote_observed_at": freshest,
        "oldest_quote_observed_at": oldest,
        "median_quote_observed_at": _median_quote_observed_at(session),
        "arbitrage_scan_at": arbitrage_started,
        "snapshot_artifact_built_at": None,
        "deployment_created_at": None,
        "unavailable": UNAVAILABLE_NOTES,
        "note": QUOTE_SPREAD_NOTE,
    }
