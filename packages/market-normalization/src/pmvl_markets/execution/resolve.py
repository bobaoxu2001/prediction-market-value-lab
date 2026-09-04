"""Resolve a research decision onto exactly one executable Kalshi contract.

The execution layer must never guess which contract the model meant. Every
ambiguity is a refusal, because the failure mode of guessing is buying the wrong
temperature bucket, the wrong station, or the wrong day - all of which look like
valid orders and none of which express the model's view.

Refuse, never substitute
------------------------
There is no "closest match" path in this module. If the requested market is
missing, closed, expired, or matches more than one contract, the resolver returns
a refusal with a reason. Substituting a neighbouring strike is the single most
dangerous convenience an execution layer can offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Iterable, Sequence

from pmvl_shared.enums import MarketStatus, Platform, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from .models import Outcome

log = get_logger(__name__)

__all__ = [
    "ResolutionStatus",
    "MarketResolution",
    "resolve_market",
    "orientation_for",
]


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CLOSED = "closed"
    EXPIRED = "expired"
    NOT_ACCEPTING_ORDERS = "not_accepting_orders"
    WRONG_PLATFORM = "wrong_platform"
    WRONG_DATE = "wrong_date"
    WRONG_STATION = "wrong_station"


@dataclass(frozen=True, slots=True)
class MarketResolution:
    status: ResolutionStatus
    market: NormalizedMarket | None = None
    ticker: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED and self.market is not None


def orientation_for(side: Side) -> Outcome:
    """Bridge the research layer's ``Side`` to the execution layer's ``Outcome``.

    Trivial by design. It exists as a named function so that the conversion
    happens in one auditable place rather than being open-coded - an inverted
    conversion scattered across call sites is nearly impossible to find.
    """
    return Outcome.from_side(side)


def resolve_market(
    candidates: Iterable[NormalizedMarket],
    *,
    ticker: str,
    expected_station: str = "",
    expected_target_date: date | None = None,
    now: datetime | None = None,
    close_buffer: timedelta = timedelta(minutes=1),
) -> MarketResolution:
    """Find the one open Kalshi contract matching ``ticker``.

    ``expected_station`` and ``expected_target_date`` are cross-checks, not search
    keys: the ticker selects the market and these confirm it is the market the
    caller believed it was asking for. A mismatch refuses rather than searching
    for something that would match.
    """
    now = now or utcnow()
    if not ticker:
        return MarketResolution(ResolutionStatus.NOT_FOUND, reason="empty ticker")

    matches: Sequence[NormalizedMarket] = [
        m for m in candidates if m.platform_market_id == ticker
    ]

    if not matches:
        return MarketResolution(
            ResolutionStatus.NOT_FOUND, ticker=ticker, reason=f"no market {ticker}"
        )
    if len(matches) > 1:
        return MarketResolution(
            ResolutionStatus.AMBIGUOUS,
            ticker=ticker,
            reason=f"{len(matches)} markets share ticker {ticker}",
        )

    market = matches[0]

    if market.platform is not Platform.KALSHI:
        return MarketResolution(
            ResolutionStatus.WRONG_PLATFORM,
            market=market,
            ticker=ticker,
            reason=f"{ticker} is a {market.platform.value} market",
        )

    if expected_station:
        actual = (market.settlement_source or "") + " " + (market.title or "")
        if expected_station.upper() not in actual.upper():
            return MarketResolution(
                ResolutionStatus.WRONG_STATION,
                market=market,
                ticker=ticker,
                reason=(
                    f"{ticker} does not reference expected station "
                    f"{expected_station}"
                ),
            )

    if expected_target_date is not None:
        close = market.close_time
        if close is None or close.date() < expected_target_date:
            return MarketResolution(
                ResolutionStatus.WRONG_DATE,
                market=market,
                ticker=ticker,
                reason=(
                    f"{ticker} closes {close.isoformat() if close else 'unknown'}, "
                    f"expected target date {expected_target_date.isoformat()}"
                ),
            )

    if market.status is not MarketStatus.OPEN:
        return MarketResolution(
            ResolutionStatus.CLOSED,
            market=market,
            ticker=ticker,
            reason=f"{ticker} status is {market.status.value}",
        )

    if not market.accepting_orders:
        return MarketResolution(
            ResolutionStatus.NOT_ACCEPTING_ORDERS,
            market=market,
            ticker=ticker,
            reason=f"{ticker} is not accepting orders",
        )

    if market.close_time is not None and market.close_time - close_buffer <= now:
        return MarketResolution(
            ResolutionStatus.EXPIRED,
            market=market,
            ticker=ticker,
            reason=(
                f"{ticker} closes at {market.close_time.isoformat()}, "
                f"within the {close_buffer} buffer"
            ),
        )

    return MarketResolution(
        ResolutionStatus.RESOLVED, market=market, ticker=ticker
    )
