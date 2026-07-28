"""Stale-quote detection.

When one venue has repriced and another has not, the lagging book still shows a quote
that *looks* attractive. Two things must be said plainly about such a quote:

1. It is the most likely kind of "arbitrage" a scanner will surface, and
2. it is the least likely to actually fill - the resting order is usually pulled
   before a retail order arrives, and a scan that reports it as executable is
   reporting a race it will lose.

This module therefore never labels anything ``EXECUTABLE``. It reports the timing
evidence - which venue moved, by how much, how old the lagging quote is - so a reader
can judge the race for themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.timeutil import humanize_seconds
from pmvl_shared.enums import ArbitrageKind, ArbitrageLabel, RuleCompatibility, Side
from pmvl_shared.money import ZERO, quantize_usd
from pmvl_shared.schemas import ArbitrageResult, ArbLeg, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds, utcnow

#: Minimum move on the leading venue to count as "the market has repriced".
MIN_DIVERGENCE = Decimal("0.04")


@dataclass
class QuoteObservation:
    """A market's mid now, and what it was at a previous observation."""

    market: NormalizedMarket
    book: OrderBook
    current_mid: Decimal
    previous_mid: Decimal | None = None
    previous_at: datetime | None = None
    market_id: int | None = None

    @property
    def move(self) -> Decimal | None:
        if self.previous_mid is None:
            return None
        return self.current_mid - self.previous_mid


def detect_stale_quote(
    leader: QuoteObservation,
    laggard: QuoteObservation,
    *,
    match_confidence: Decimal = ZERO,
    rule_compatibility: RuleCompatibility = RuleCompatibility.EQUIVALENT,
) -> ArbitrageResult | None:
    """Report a lagging quote on ``laggard`` after ``leader`` has repriced.

    Requires evidence of an actual *move* on the leader, not merely a price
    difference. A persistent gap between two venues is a rule or liquidity difference;
    only a recent move on one side makes the other side's quote genuinely stale.
    """
    settings = get_settings()

    move = leader.move
    if move is None or abs(move) < MIN_DIVERGENCE:
        return None

    laggard_move = laggard.move
    # The laggard must NOT have followed. If it moved similarly, both venues repriced
    # and there is nothing stale about either.
    if laggard_move is not None and abs(laggard_move) >= abs(move) * Decimal("0.5"):
        return None

    divergence = leader.current_mid - laggard.current_mid
    if abs(divergence) < MIN_DIVERGENCE:
        return None

    # Buy the side the laggard is still under-pricing relative to the leader.
    side = Side.YES if divergence > 0 else Side.NO
    entry = laggard.book.best_ask(side)
    if entry is None:
        return None

    available = sum((l.size for l in laggard.book.asks(side)), ZERO)
    laggard_age = age_seconds(laggard.book.observed_at)
    leader_age = age_seconds(leader.book.observed_at)

    risk_flags = [
        f"{leader.market.platform.value} moved {move} since its previous observation "
        f"while {laggard.market.platform.value} did not",
        "resting quotes behind a repriced market are usually cancelled before a "
        "retail order arrives; treat this as a race, not a locked-in edge",
    ]
    if laggard_age is not None:
        risk_flags.append(f"lagging book snapshot is {humanize_seconds(laggard_age)} old")
    if laggard_age is not None and laggard_age > settings.max_quote_age_seconds:
        risk_flags.append(
            "the lagging quote is older than the freshness limit, so it may already "
            "be gone"
        )
    if rule_compatibility != RuleCompatibility.IDENTICAL:
        risk_flags.append(
            "the two markets are not an exact rule match, so part of the divergence "
            "may be a genuine difference in what settles, not staleness"
        )

    return ArbitrageResult(
        kind=ArbitrageKind.STALE_QUOTE,
        label=ArbitrageLabel.STALE_QUOTE,
        title=(
            f"Stale quote: {laggard.market.platform.value} lagging "
            f"{leader.market.platform.value} - {laggard.market.title[:70]}"
        ),
        legs=[
            ArbLeg(
                platform=laggard.market.platform,
                platform_market_id=laggard.market.platform_market_id,
                market_id=laggard.market_id,
                title=laggard.market.title,
                side=side,
                price=entry,
                size_available=available,
            ),
            ArbLeg(
                platform=leader.market.platform,
                platform_market_id=leader.market.platform_market_id,
                market_id=leader.market_id,
                title=leader.market.title,
                side=side,
                price=leader.current_mid,
                size_available=ZERO,
            ),
        ],
        gross_edge_per_set=quantize_usd(abs(divergence)),
        # No cost model is published for a quote we do not believe is reachable.
        total_cost_per_set=ZERO,
        net_profit_per_set=ZERO,
        max_executable_sets=ZERO,
        max_net_profit=ZERO,
        capital_required=ZERO,
        net_roi=ZERO,
        rule_compatibility=rule_compatibility,
        risk_flags=risk_flags,
        quote_age_seconds=int(laggard_age) if laggard_age is not None else None,
        expected_resolution_time=laggard.market.expected_resolution_time,
        cost_breakdown={
            "leader_move": str(move),
            "laggard_move": str(laggard_move) if laggard_move is not None else None,
            "divergence": str(divergence),
            "leader_quote_age_s": int(leader_age) if leader_age is not None else None,
            "laggard_quote_age_s": int(laggard_age) if laggard_age is not None else None,
            "match_confidence": str(match_confidence),
        },
        provenance=laggard.market.provenance,
    )
