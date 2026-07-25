"""Value candidate construction and Top-N ranking.

Gates a market must clear to become a recommendation
----------------------------------------------------
1. Open, accepting orders, and inside a resolution horizon.
2. A real executable ask exists on the chosen side, with depth behind it.
3. The fair-probability estimate has an **independent prior** - it is not just a
   restatement of this market's own price.
4. ``conservative_net_ev = fair_probability_low - total_executable_cost > threshold``.

Gate 3 is what stops the system inventing edge, and gate 4 is what stops a wide,
uncertain estimate from producing one.

Ranking
-------
Ranking on ROI alone is actively harmful: a 1c contract with $3 of depth and a
model error of two cents shows a 200% ROI and would top every list forever. The
composite score therefore multiplies the conservative edge by factors for capacity,
liquidity, spread, confidence, freshness and settlement proximity - and every raw
input is preserved on the record so a reader can audit the score rather than trust it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from pmvl_shared.config import get_settings
from pmvl_shared.enums import DataProvenance, MarketStatus, Platform, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import (
    FairProbability,
    NormalizedMarket,
    OrderBook,
    ValueCandidate,
)
from pmvl_shared.timeutil import Horizon, age_seconds, hours_until, horizons_for, utcnow

from ..pricing.execution import (
    STANDARD_SIZES,
    build_cost_breakdown,
    expected_profit_per_100_usd,
    fractional_kelly,
    max_profitable_size,
    net_ev,
    net_roi,
    price_at_sizes,
)
from ..pricing.orderbook import depth_usd, effective_spread, executable_quote

log = get_logger(__name__)

#: Reference size used to establish the headline entry price on a card.
REFERENCE_SIZE = Decimal("100")


@dataclass
class RankingConfig:
    top_n: int = 10
    min_conservative_net_ev: Decimal = Decimal("0.005")
    min_depth_usd: Decimal = Decimal("25")
    max_spread: Decimal = Decimal("0.15")
    min_confidence: Decimal = Decimal("0.15")
    require_independent_prior: bool = True

    @classmethod
    def from_settings(cls) -> "RankingConfig":
        s = get_settings()
        return cls(
            top_n=s.top_n,
            min_conservative_net_ev=s.min_conservative_net_ev,
            min_depth_usd=s.min_orderbook_notional_usd,
        )


def win_probability_for_side(fair: FairProbability, side: Side) -> tuple[Decimal, Decimal]:
    """(mean, conservative lower bound) of winning, for the chosen side.

    For NO, both the mean and the *bound* must be mirrored: the conservative case for
    a NO buyer is the case where YES is more likely than estimated, i.e.
    ``1 - fair_probability_high``. Using ``1 - low`` would be the optimistic bound and
    would systematically overstate NO-side edge.
    """
    if side == Side.YES:
        return fair.fair_probability_mean, fair.fair_probability_low
    return ONE - fair.fair_probability_mean, ONE - fair.fair_probability_high


def build_candidate(
    market: NormalizedMarket,
    book: OrderBook,
    fair: FairProbability,
    side: Side,
    horizon: Horizon,
    *,
    config: RankingConfig | None = None,
    now: datetime | None = None,
    market_id: int | None = None,
) -> ValueCandidate | None:
    """Price one side of one market. Returns ``None`` when it cannot be traded."""
    config = config or RankingConfig.from_settings()
    now = now or utcnow()

    quote = executable_quote(book, side, REFERENCE_SIZE)
    if quote is None or quote.filled_size <= 0:
        return None

    cost = build_cost_breakdown(market, book, side, quote.filled_size, quote=quote)
    if cost is None:
        return None
    total_cost = cost.total_cost

    mean_p, conservative_p = win_probability_for_side(fair, side)
    ev = net_ev(mean_p, total_cost)
    conservative_ev = net_ev(conservative_p, total_cost)

    side_depth = depth_usd(book, side)
    spread = effective_spread(book, side)
    cap_size, _cap_profit = max_profitable_size(market, book, side, conservative_p)

    risk_flags = collect_risk_flags(
        market, book, fair, side, spread=spread, depth=side_depth, now=now
    )

    candidate = ValueCandidate(
        market_id=market_id,
        platform=market.platform,
        platform_market_id=market.platform_market_id,
        title=market.title,
        side=side,
        horizon=horizon,
        entry_price=quote.average_price,
        executable_size=quote.filled_size,
        cost=cost,
        total_cost_per_contract=quantize_usd(total_cost),
        fair=fair,
        net_ev_per_contract=ev,
        conservative_net_ev=conservative_ev,
        net_roi=net_roi(mean_p, total_cost),
        sized_quotes=price_at_sizes(market, book, side, mean_p, sizes=STANDARD_SIZES),
        expected_profit_per_100_usd=expected_profit_per_100_usd(ev, total_cost),
        fractional_kelly=fractional_kelly(conservative_p, total_cost),
        recommended_position_cap=cap_size,
        spread=spread,
        liquidity_usd=side_depth,
        expected_resolution_time=market.expected_resolution_time,
        risk_flags=risk_flags,
        provenance=market.provenance,
    )
    candidate.composite_score = composite_score(candidate, now=now)
    return candidate


def collect_risk_flags(
    market: NormalizedMarket,
    book: OrderBook,
    fair: FairProbability,
    side: Side,
    *,
    spread: Decimal | None,
    depth: Decimal,
    now: datetime,
) -> list[str]:
    """Every reason a reader should discount this candidate, stated explicitly."""
    flags: list[str] = []
    settings = get_settings()

    if not fair.has_independent_prior:
        flags.append("no_independent_prior")
    if fair.model_confidence < Decimal("0.3"):
        flags.append("low_model_confidence")
    if fair.evidence_quality <= 0:
        flags.append("no_research_evidence")

    quote_age = age_seconds(book.observed_at, now=now)
    if quote_age is not None and quote_age > settings.max_quote_age_seconds:
        flags.append("stale_quote")

    if spread is not None and spread > Decimal("0.05"):
        flags.append("wide_spread")
    if depth < settings.min_orderbook_notional_usd:
        flags.append("thin_liquidity")
    if (market.volume_24h or ZERO) < settings.min_volume_24h_usd:
        flags.append("low_volume")

    entry = book.best_ask(side)
    if entry is not None and (entry < Decimal("0.03") or entry > Decimal("0.97")):
        flags.append("extreme_price")

    hours = hours_until(market.expected_resolution_time, now=now)
    if hours is not None and hours < 1:
        flags.append("imminent_settlement")
    if market.expected_resolution_time and market.close_time:
        if market.expected_resolution_time < market.close_time:
            # Kalshi routinely expects to settle before the market's outer close.
            flags.append("settles_before_close")

    if market.platform == Platform.POLYMARKET:
        flags.append("uma_oracle_settlement")
    if market.status != MarketStatus.OPEN or not market.accepting_orders:
        flags.append("not_accepting_orders")
    if market.provenance != DataProvenance.LIVE:
        flags.append(f"provenance_{market.provenance.value}")

    return flags


def composite_score(candidate: ValueCandidate, *, now: datetime | None = None) -> Decimal:
    """Rank score. Multiplicative so any single fatal weakness collapses it.

    Base is the conservative edge scaled by realisable capacity - an edge you can only
    take $5 of is worth less than the same edge at $500 - then discounted by liquidity,
    spread, confidence, staleness and settlement proximity.
    """
    now = now or utcnow()
    if candidate.conservative_net_ev <= 0:
        return ZERO

    # Capacity: total conservative profit actually available, log-damped so a very
    # deep market does not dominate purely on size.
    capacity_profit = candidate.conservative_net_ev * min(
        candidate.executable_size, candidate.recommended_position_cap or candidate.executable_size
    )
    capacity = D(str(math.log1p(max(0.0, float(capacity_profit)))))

    liquidity = _saturating(candidate.liquidity_usd or ZERO, D(500))
    spread_factor = (
        ONE - min(ONE, safe_div(candidate.spread or ZERO, Decimal("0.10")))
        if candidate.spread is not None
        else Decimal("0.5")
    )
    confidence = candidate.fair.model_confidence

    freshness = ONE
    if candidate.fair.data_freshness_seconds:
        age_hours = candidate.fair.data_freshness_seconds / 3600.0
        freshness = D(str(1.0 / (1.0 + age_hours / 12.0)))

    # Sooner resolution is better: capital turns over faster and there is less time
    # for the thesis to be invalidated.
    hours = hours_until(candidate.expected_resolution_time, now=now) or 720.0
    horizon_factor = D(str(1.0 / (1.0 + max(0.0, hours) / 168.0)))

    penalty = ONE
    if "no_independent_prior" in candidate.risk_flags:
        penalty *= Decimal("0.05")
    if "stale_quote" in candidate.risk_flags:
        penalty *= Decimal("0.3")
    if "thin_liquidity" in candidate.risk_flags:
        penalty *= Decimal("0.4")
    if "extreme_price" in candidate.risk_flags:
        penalty *= Decimal("0.5")

    score = (
        capacity
        * (Decimal("0.4") + Decimal("0.6") * liquidity)
        * (Decimal("0.4") + Decimal("0.6") * spread_factor)
        * (Decimal("0.3") + Decimal("0.7") * confidence)
        * freshness
        * (Decimal("0.5") + Decimal("0.5") * horizon_factor)
        * penalty
    )
    return quantize_usd(max(ZERO, score))


def _saturating(value: Decimal, scale: Decimal) -> Decimal:
    """Map [0, inf) to [0, 1) with a soft knee at ``scale``."""
    if value <= 0:
        return ZERO
    return safe_div(value, value + scale)


def passes_gates(candidate: ValueCandidate, config: RankingConfig) -> tuple[bool, str]:
    """Apply the hard admission gates. Returns ``(passed, reason_if_rejected)``."""
    if config.require_independent_prior and not candidate.fair.has_independent_prior:
        return False, "no independent prior: estimate is derived from this market's own price"
    if candidate.conservative_net_ev <= config.min_conservative_net_ev:
        return False, (
            f"conservative net EV {candidate.conservative_net_ev} does not clear "
            f"{config.min_conservative_net_ev}"
        )
    if (candidate.liquidity_usd or ZERO) < config.min_depth_usd:
        return False, f"depth {candidate.liquidity_usd} below {config.min_depth_usd}"
    if candidate.spread is not None and candidate.spread > config.max_spread:
        return False, f"spread {candidate.spread} exceeds {config.max_spread}"
    if candidate.fair.model_confidence < config.min_confidence:
        return False, f"model confidence {candidate.fair.model_confidence} too low"
    if candidate.executable_size <= 0:
        return False, "no executable size"
    return True, ""


def rank_candidates(
    candidates: Sequence[ValueCandidate],
    horizon: Horizon,
    *,
    config: RankingConfig | None = None,
) -> list[ValueCandidate]:
    """Filter to one horizon, apply gates, and return the Top-N by composite score.

    At most one side per market survives: YES and NO on the same market are opposite
    expressions of the same view, and publishing both would double-count the position.
    """
    config = config or RankingConfig.from_settings()
    eligible: dict[str, ValueCandidate] = {}

    for candidate in candidates:
        if candidate.horizon != horizon:
            continue
        ok, reason = passes_gates(candidate, config)
        if not ok:
            log.debug("rejected %s: %s", candidate.platform_market_id, reason)
            continue
        key = f"{candidate.platform.value}:{candidate.platform_market_id}"
        best = eligible.get(key)
        if best is None or candidate.composite_score > best.composite_score:
            eligible[key] = candidate

    ranked = sorted(eligible.values(), key=lambda c: c.composite_score, reverse=True)
    return ranked[: config.top_n]


def horizon_of(market: NormalizedMarket, *, now: datetime | None = None) -> Horizon | None:
    horizons = horizons_for(market.expected_resolution_time, now=now)
    return horizons[0] if horizons else None
