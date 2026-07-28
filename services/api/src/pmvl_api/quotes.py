"""One coherent quote per market.

A market carries two independent observations of its own price:

* ``Market.best_yes_ask`` and friends, copied from the venue's market-summary payload
  when metadata was last ingested, timestamped ``quote_observed_at``;
* the latest ``OrderbookSnapshot``, refreshed on its own, much faster cadence.

They are captured at different times, so they drift. On the current snapshot ten
markets disagree by more than a cent, with the summary two days behind the book - one
Bitcoin strike showed a 75c summary ask against a 65c book ask. The detail page
rendered the summary under "Current quotes" and the book underneath, and the model's
market-implied probability came from a third path, so a reader saw three different
prices for one contract and no way to tell which to believe.

This module picks ONE source per market and derives everything from it: bid, ask,
spread, depth and the timestamp shown next to them. Mixing sources within a single
view is the failure mode, so the returned payload always names where its numbers came
from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pmvl_shared.money import ONE, ZERO

from pmvl_markets.db_models import Market, OrderbookLevel, OrderbookSnapshot


@dataclass
class CoherentQuote:
    """Bid, ask, spread and depth that all came from the same observation."""

    source: str  # "orderbook" | "venue_summary" | "none"
    observed_at: datetime | None
    best_yes_bid: Decimal | None = None
    best_yes_ask: Decimal | None = None
    best_no_bid: Decimal | None = None
    best_no_ask: Decimal | None = None
    spread: Decimal | None = None
    yes_depth_usd: Decimal | None = None
    no_depth_usd: Decimal | None = None
    #: True when the venue summary disagrees materially with the book. Kept visible
    #: rather than silently discarded: it means metadata ingest has fallen behind.
    summary_disagrees: bool = False
    summary_ask: Decimal | None = None

    @property
    def no_ask_depth_usd_value(self) -> Decimal | None:
        return self.no_depth_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "observed_at": self.observed_at,
            "best_yes_bid": self.best_yes_bid,
            "best_yes_ask": self.best_yes_ask,
            "best_no_bid": self.best_no_bid,
            "best_no_ask": self.best_no_ask,
            "spread": self.spread,
            # Ask-side depth: what a BUYER of that side can lift at the displayed
            # prices. Not the same as OrderbookSnapshot's stored depth total.
            "yes_ask_depth_usd": self.yes_depth_usd,
            "no_ask_depth_usd": self.no_ask_depth_usd_value,
            "yes_depth_usd": self.yes_depth_usd,
            "no_depth_usd": self.no_depth_usd,
            "summary_disagrees": self.summary_disagrees,
            "summary_ask": self.summary_ask,
            "note": (
                "Every price here is derived from one observation. "
                + (
                    "Source: the latest order book."
                    if self.source == "orderbook"
                    else "Source: the venue's market summary; no order book was captured."
                    if self.source == "venue_summary"
                    else "No usable quote."
                )
            ),
        }


#: A cent of drift is normal between two observations; more means the summary is stale.
_MATERIAL_DRIFT = Decimal("0.01")


def _summary_quote(market: Market) -> CoherentQuote:
    return CoherentQuote(
        source="venue_summary" if market.best_yes_ask is not None else "none",
        observed_at=market.quote_observed_at,
        best_yes_bid=market.best_yes_bid,
        best_yes_ask=market.best_yes_ask,
        best_no_bid=market.best_no_bid,
        best_no_ask=market.best_no_ask,
        spread=market.spread,
        yes_depth_usd=market.orderbook_depth_usd,
        summary_ask=market.best_yes_ask,
    )


def coherent_quote(session: Session, market: Market) -> CoherentQuote:
    """The single quote a view should render for ``market``.

    Prefers the order book, because it is refreshed far more often and is the thing
    an order would actually execute against. Falls back to the venue summary only
    when no book was captured, and says so.
    """
    snapshot = session.scalar(
        select(OrderbookSnapshot)
        .where(OrderbookSnapshot.market_id == market.id)
        .order_by(OrderbookSnapshot.observed_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return _summary_quote(market)

    levels = list(
        session.scalars(
            select(OrderbookLevel)
            .where(OrderbookLevel.snapshot_id == snapshot.id)
            .order_by(OrderbookLevel.level_index)
        )
    )
    if not levels:
        return _summary_quote(market)

    def best(side: str, is_ask: bool) -> Decimal | None:
        matching = [l.price for l in levels if l.side == side and l.is_ask is is_ask]
        if not matching:
            return None
        return min(matching) if is_ask else max(matching)

    def depth(side: str) -> Decimal:
        return sum(
            (l.price * l.size for l in levels if l.side == side and l.is_ask),
            ZERO,
        )

    yes_ask, yes_bid = best("yes", True), best("yes", False)
    no_ask, no_bid = best("no", True), best("no", False)
    spread = (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None

    disagrees = (
        market.best_yes_ask is not None
        and yes_ask is not None
        and abs(market.best_yes_ask - yes_ask) > _MATERIAL_DRIFT
    )

    return CoherentQuote(
        source="orderbook",
        observed_at=snapshot.observed_at,
        best_yes_bid=yes_bid,
        best_yes_ask=yes_ask,
        best_no_bid=no_bid,
        best_no_ask=no_ask,
        spread=spread,
        yes_depth_usd=depth("yes"),
        no_depth_usd=depth("no"),
        summary_disagrees=disagrees,
        summary_ask=market.best_yes_ask,
    )
