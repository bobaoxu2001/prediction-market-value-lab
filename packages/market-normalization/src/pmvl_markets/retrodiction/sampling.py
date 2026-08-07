"""Choosing which settled markets the harness evaluates.

Sampling is where a historical evaluation is most easily flattered, so the choices
are made here, in one place, with the reason for each attached.

The rule the whole module follows: **the sample must be defined by facts that were
knowable before the outcome.** Category, venue, resolution date and traded volume all
qualify. "Markets the model has an opinion about" does not - filtering on that would
be selecting on the model's own behaviour, and every skip is therefore counted and
reported rather than quietly removing a market from the denominator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.db.models import Market, Settlement
from pmvl_shared.enums import Category, DataProvenance
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ZERO
from pmvl_shared.timeutil import ensure_utc, utcnow

from ..ingest.runner import _market_from_row
from .harness import SettledMarket

log = get_logger(__name__)

#: Categories worth sampling: those with at least one model that can be replayed.
#:
#: Not a hard filter on correctness - a category with no replayable model simply
#: produces skips - but fetching price history for thousands of contracts nothing
#: can score would spend the venues' rate limit to learn nothing.
DEFAULT_CATEGORIES: tuple[Category, ...] = (
    Category.CRYPTO,
    Category.FINANCE,
)


@dataclass(frozen=True)
class SamplingCriteria:
    """The sample definition, recorded so a report can state it verbatim.

    A Brier improvement is meaningless without this. "The model beat the market"
    over 40 high-volume crypto contracts resolving within a week is a different
    claim from the same sentence over every settled market on both venues, and the
    two are indistinguishable unless the criteria travel with the number.
    """

    categories: tuple[Category, ...] = DEFAULT_CATEGORIES
    #: Only markets that settled within this many days. Bounded because the venues'
    #: candlestick history is not retained indefinitely, so older markets mostly
    #: produce "no price history" skips.
    settled_within_days: int = 60
    #: Excludes contracts too thin for their price to mean anything. A market that
    #: traded $12 all week has a "price" that is one person's opinion, and beating
    #: it is not evidence of skill.
    min_total_volume_usd: Decimal = D("1000")
    max_markets: int = 200

    def as_dict(self) -> dict[str, object]:
        return {
            "categories": [c.value for c in self.categories],
            "settled_within_days": self.settled_within_days,
            "min_total_volume_usd": str(self.min_total_volume_usd),
            "max_markets": self.max_markets,
        }


def load_settled_markets(
    session: Session,
    *,
    criteria: SamplingCriteria | None = None,
    now: datetime | None = None,
) -> list[SettledMarket]:
    """Settled markets matching the criteria, newest first.

    Demo rows are excluded unconditionally and not as a configurable filter. The
    demo forecaster is deliberately imperfect and synthetic; scoring it here would
    produce a real-looking Brier for a market that never existed.
    """
    now = now or utcnow()
    criteria = criteria or SamplingCriteria()
    cutoff = now - timedelta(days=criteria.settled_within_days)

    rows = session.execute(
        select(Market, Settlement)
        .join(Settlement, Settlement.market_id == Market.id)
        .where(
            Settlement.provenance == DataProvenance.LIVE,
            Market.provenance == DataProvenance.LIVE,
            Settlement.disputed.is_(False),
            Market.category.in_([c.value for c in criteria.categories]),
        )
    ).all()

    out: list[SettledMarket] = []
    for market_row, settlement in rows:
        settled_at = ensure_utc(settlement.settled_at)
        if settled_at is None or settled_at < cutoff or settled_at > now:
            continue

        market = _market_from_row(market_row)
        if market.expected_resolution_time is None and market.close_time is None:
            continue

        volume = market.total_volume or market.volume_24h or ZERO
        if volume < criteria.min_total_volume_usd:
            continue

        payout = _outcome_value(settlement.yes_payout)
        if payout is None:
            continue

        out.append(
            SettledMarket(market=market, yes_payout=payout, settled_at=settled_at)
        )

    out.sort(key=lambda item: item.settled_at or now, reverse=True)
    return out[: criteria.max_markets]


def _outcome_value(yes_payout: Decimal | None) -> Decimal | None:
    """Normalise a settlement payout into a Brier outcome, or refuse it.

    Only three payouts are scoreable: YES, NO, and an even split. A void market has
    no outcome to have forecast, and folding it in as a 0 (or as a 0.5) would be
    inventing a result the world never produced.
    """
    if yes_payout is None:
        return None
    value = D(yes_payout)
    if value in (ZERO, D("0.5"), D("1")):
        return value
    log.debug("unscoreable settlement payout %s; skipping", value)
    return None


def describe_sample(
    settled: Sequence[SettledMarket], criteria: SamplingCriteria
) -> dict[str, object]:
    """What the sample actually contains, for the report header.

    The base rate is included deliberately. A sample that resolved YES 90% of the
    time makes a constant forecast of 0.9 look excellent, and a reader needs to see
    that before reading any Brier score below it.
    """
    if not settled:
        return {"criteria": criteria.as_dict(), "n_markets": 0}

    yes_rate = float(sum(float(s.yes_payout) for s in settled) / len(settled))
    dates = [s.settled_at for s in settled if s.settled_at]
    by_category: dict[str, int] = {}
    for item in settled:
        key = item.market.category.value
        by_category[key] = by_category.get(key, 0) + 1

    return {
        "criteria": criteria.as_dict(),
        "n_markets": len(settled),
        "yes_base_rate": round(yes_rate, 4),
        "by_category": dict(sorted(by_category.items())),
        "settled_from": min(dates).isoformat() if dates else None,
        "settled_to": max(dates).isoformat() if dates else None,
    }
