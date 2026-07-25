"""Logical-constraint violations.

Some price relationships are impossible under any probability measure:

* **Monotonicity.** If A implies B then P(A) <= P(B). "BTC above $80k" implies "BTC
  above $70k", so the $80k contract must not trade above the $70k one.
* **Nested time windows.** "happens by March" implies "happens by June".
* **Exhaustive sets.** Mutually exclusive, exhaustive outcomes must sum to 1.

A violation is real information - but it is **not** automatically an arbitrage, and
this module never labels it as one. Turning a monotonicity violation into locked-in
profit requires buying the cheap leg *and* selling the expensive one, and on these
venues "selling" means buying the opposite side, which has its own ask and its own
cost. Whether the full hedge clears after costs is a separate question that
:func:`_price_monotonicity_hedge` actually checks; only if a complete executable
hedge exists does the result rise above
:attr:`ArbitrageLabel.LOGICAL_MISPRICING`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.enums import ArbitrageKind, ArbitrageLabel, RuleCompatibility, Side
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import ArbitrageResult, ArbLeg, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds

from ..pricing.fees import taker_fee
from ..pricing.orderbook import executable_quote

log = get_logger(__name__)

#: Price gap below which a "violation" is just tick noise and spread.
MIN_VIOLATION = Decimal("0.02")


@dataclass
class ThresholdMarket:
    """A market in a monotone family, e.g. one strike of 'BTC above X'."""

    market: NormalizedMarket
    book: OrderBook
    threshold: Decimal
    market_id: int | None = None

    @property
    def yes_ask(self) -> Decimal | None:
        return self.book.best_ask(Side.YES)

    @property
    def yes_bid(self) -> Decimal | None:
        return self.book.best_bid(Side.YES)


def scan_monotonicity(
    family: list[ThresholdMarket],
    *,
    ascending_implies: bool = True,
    family_label: str = "",
) -> list[ArbitrageResult]:
    """Find strikes that violate monotonicity within one threshold family.

    ``ascending_implies=True`` means a *higher* threshold is a *stronger* claim
    (P must be non-increasing in threshold), which is the case for "above X" markets.
    """
    results: list[ArbitrageResult] = []
    if len(family) < 2:
        return results

    ordered = sorted(family, key=lambda t: t.threshold, reverse=not ascending_implies)

    for i in range(len(ordered) - 1):
        weaker = ordered[i]      # easier to satisfy -> should be more expensive
        stronger = ordered[i + 1]

        weak_price = weaker.yes_ask
        strong_price = stronger.yes_ask
        if weak_price is None or strong_price is None:
            continue

        # Violation: the harder-to-satisfy contract costs MORE than the easier one.
        gap = strong_price - weak_price
        if gap <= MIN_VIOLATION:
            continue

        result = _build_logical_result(weaker, stronger, gap, family_label)
        if result is not None:
            results.append(result)

    return results


def _build_logical_result(
    weaker: ThresholdMarket,
    stronger: ThresholdMarket,
    gap: Decimal,
    family_label: str,
) -> ArbitrageResult | None:
    """Describe the violation and test whether a real hedge can be constructed."""
    settings = get_settings()

    hedge = _price_monotonicity_hedge(weaker, stronger)

    risk_flags = [
        f"P(>= {stronger.threshold}) is priced {gap} above P(>= {weaker.threshold}), "
        f"which is impossible: the higher threshold strictly implies the lower one",
    ]

    if hedge is None:
        # No executable hedge: report the mispricing honestly and stop.
        risk_flags.append(
            "no complete executable hedge exists at current depth, so this is a "
            "pricing anomaly to investigate, NOT a locked-in profit"
        )
        return ArbitrageResult(
            kind=ArbitrageKind.LOGICAL_CONSTRAINT,
            label=ArbitrageLabel.LOGICAL_MISPRICING,
            title=(
                f"Monotonicity violation{': ' + family_label if family_label else ''} - "
                f"{stronger.market.title[:70]}"
            ),
            legs=[
                ArbLeg(
                    platform=stronger.market.platform,
                    platform_market_id=stronger.market.platform_market_id,
                    market_id=stronger.market_id,
                    title=stronger.market.title,
                    side=Side.YES,
                    price=stronger.yes_ask or ZERO,
                    size_available=ZERO,
                ),
                ArbLeg(
                    platform=weaker.market.platform,
                    platform_market_id=weaker.market.platform_market_id,
                    market_id=weaker.market_id,
                    title=weaker.market.title,
                    side=Side.YES,
                    price=weaker.yes_ask or ZERO,
                    size_available=ZERO,
                ),
            ],
            gross_edge_per_set=quantize_usd(gap),
            total_cost_per_set=ZERO,
            net_profit_per_set=ZERO,
            max_executable_sets=ZERO,
            max_net_profit=ZERO,
            capital_required=ZERO,
            net_roi=ZERO,
            rule_compatibility=RuleCompatibility.EQUIVALENT,
            risk_flags=risk_flags,
            expected_resolution_time=stronger.market.expected_resolution_time,
            cost_breakdown={"observed_gap": str(gap), "hedgeable": False},
            provenance=stronger.market.provenance,
        )

    sets, gross_cost, fees, legs = hedge

    slippage = (
        weaker.market.tick_size + stronger.market.tick_size
    ) * Decimal(settings.slippage_ticks) * sets
    total_cost = gross_cost + fees + slippage

    # The hedge - long the weaker (easier) claim, long NO on the stronger claim -
    # pays $1 in every state except the one where the stronger claim is true and the
    # weaker one is false, which is impossible by implication. So payout is $1/set.
    payout = sets * ONE
    net_profit = payout - total_cost
    if net_profit <= 0:
        risk_flags.append(
            "a hedge exists but does not clear its costs; not actionable"
        )
        label = ArbitrageLabel.LOGICAL_MISPRICING
        net_profit = ZERO
    else:
        label = ArbitrageLabel.EXECUTABLE
        risk_flags.append(
            "hedge is long the weaker claim and long NO on the stronger claim; "
            "by implication the uncovered state cannot occur"
        )

    ages = [age_seconds(weaker.book.observed_at), age_seconds(stronger.book.observed_at)]
    worst_age = max([a for a in ages if a is not None], default=None)
    if worst_age is not None and worst_age > settings.max_quote_age_seconds:
        label = ArbitrageLabel.STALE_QUOTE
        risk_flags.append(f"oldest quote is {worst_age:.0f}s old")
    if total_cost < settings.min_orderbook_notional_usd:
        label = ArbitrageLabel.INSUFFICIENT_LIQUIDITY

    per_set_total = safe_div(total_cost, sets)
    return ArbitrageResult(
        kind=ArbitrageKind.LOGICAL_CONSTRAINT,
        label=label,
        title=(
            f"Monotonicity hedge{': ' + family_label if family_label else ''} - "
            f"{stronger.market.title[:70]}"
        ),
        legs=legs,
        gross_edge_per_set=quantize_usd(ONE - safe_div(gross_cost, sets)),
        total_cost_per_set=quantize_usd(per_set_total),
        net_profit_per_set=quantize_usd(ONE - per_set_total),
        max_executable_sets=sets,
        max_net_profit=quantize_usd(net_profit),
        capital_required=quantize_usd(total_cost),
        net_roi=quantize_usd(safe_div(net_profit, total_cost)),
        rule_compatibility=RuleCompatibility.EQUIVALENT,
        risk_flags=risk_flags,
        quote_age_seconds=int(worst_age) if worst_age is not None else None,
        expected_resolution_time=stronger.market.expected_resolution_time,
        cost_breakdown={
            "observed_gap": str(gap),
            "gross_cost": str(quantize_usd(gross_cost)),
            "fees": str(quantize_usd(fees)),
            "slippage": str(quantize_usd(slippage)),
            "hedgeable": True,
        },
        provenance=stronger.market.provenance,
    )


def _price_monotonicity_hedge(
    weaker: ThresholdMarket, stronger: ThresholdMarket
) -> tuple[Decimal, Decimal, Decimal, list[ArbLeg]] | None:
    """Price the hedge: long YES(weaker) + long NO(stronger).

    Since ``stronger implies weaker``, the state (stronger true, weaker false) is
    impossible. The remaining states each pay exactly $1:

    - weaker true, stronger true  -> YES(weaker) pays $1
    - weaker true, stronger false -> YES(weaker) $1 + NO(stronger) $1 = $2
    - weaker false, stronger false-> NO(stronger) pays $1

    The worst case is $1, so the pair is riskless if it costs less than $1. Using the
    worst case rather than the average is the conservative choice.
    """
    yes_weak = weaker.book.best_ask(Side.YES)
    no_strong = stronger.book.best_ask(Side.NO)
    if yes_weak is None or no_strong is None:
        return None
    if yes_weak + no_strong >= ONE:
        return None

    capacity = min(
        sum((l.size for l in weaker.book.asks(Side.YES)), ZERO),
        sum((l.size for l in stronger.book.asks(Side.NO)), ZERO),
    )
    if capacity <= 0:
        return None

    sets = ZERO
    hi = min(capacity, D(10000))

    def cost_at(size: Decimal) -> Decimal | None:
        q1 = executable_quote(weaker.book, Side.YES, size)
        q2 = executable_quote(stronger.book, Side.NO, size)
        if q1 is None or q2 is None or q1.filled_size < size or q2.filled_size < size:
            return None
        return q1.average_price + q2.average_price

    if (top := cost_at(hi)) is not None and top < ONE:
        sets = hi
    else:
        lo = ZERO
        for _ in range(20):
            mid = (lo + hi) / D(2)
            value = cost_at(mid)
            if value is not None and value < ONE:
                lo = mid
            else:
                hi = mid
        sets = lo.quantize(Decimal("0.01"))

    if sets <= 0:
        return None

    q1 = executable_quote(weaker.book, Side.YES, sets)
    q2 = executable_quote(stronger.book, Side.NO, sets)
    if q1 is None or q2 is None:
        return None

    gross = (q1.average_price + q2.average_price) * sets
    fee1 = taker_fee(
        weaker.market.platform, sets, q1.average_price,
        rate=weaker.market.fee_rate, fee_type=weaker.market.fee_type,
    )
    fee2 = taker_fee(
        stronger.market.platform, sets, q2.average_price,
        rate=stronger.market.fee_rate, fee_type=stronger.market.fee_type,
    )

    legs = [
        ArbLeg(
            platform=weaker.market.platform,
            platform_market_id=weaker.market.platform_market_id,
            market_id=weaker.market_id,
            title=weaker.market.title,
            side=Side.YES,
            price=q1.average_price,
            size_available=q1.filled_size,
            fee_per_contract=quantize_usd(safe_div(fee1, sets)),
        ),
        ArbLeg(
            platform=stronger.market.platform,
            platform_market_id=stronger.market.platform_market_id,
            market_id=stronger.market_id,
            title=stronger.market.title,
            side=Side.NO,
            price=q2.average_price,
            size_available=q2.filled_size,
            fee_per_contract=quantize_usd(safe_div(fee2, sets)),
        ),
    ]
    return sets, gross, fee1 + fee2, legs


def scan_exhaustive_sum(
    legs: list[ThresholdMarket], *, event_title: str, tolerance: Decimal = Decimal("0.03")
) -> ArbitrageResult | None:
    """Flag a mutually exclusive, exhaustive set whose prices do not sum to ~1.

    Reported as a mispricing only. Whether it is harvestable depends on which side is
    cheap and whether the full basket is executable, which the multi-outcome scanner
    answers directly.
    """
    prices = [(leg, leg.yes_ask) for leg in legs]
    quotable = [(leg, p) for leg, p in prices if p is not None]
    if len(quotable) < 2:
        return None

    total = sum((p for _, p in quotable), ZERO)
    deviation = total - ONE
    if abs(deviation) <= tolerance:
        return None

    direction = "exceed" if deviation > 0 else "fall short of"
    return ArbitrageResult(
        kind=ArbitrageKind.LOGICAL_CONSTRAINT,
        label=ArbitrageLabel.LOGICAL_MISPRICING,
        title=f"{event_title} - outcome probabilities sum to {total}",
        legs=[
            ArbLeg(
                platform=leg.market.platform,
                platform_market_id=leg.market.platform_market_id,
                market_id=leg.market_id,
                title=leg.market.title,
                side=Side.YES,
                price=price,
                size_available=sum((l.size for l in leg.book.asks(Side.YES)), ZERO),
            )
            for leg, price in quotable
        ],
        gross_edge_per_set=quantize_usd(abs(deviation)),
        total_cost_per_set=ZERO,
        net_profit_per_set=ZERO,
        max_executable_sets=ZERO,
        max_net_profit=ZERO,
        capital_required=ZERO,
        net_roi=ZERO,
        rule_compatibility=RuleCompatibility.EQUIVALENT,
        risk_flags=[
            f"{len(quotable)} mutually exclusive outcomes {direction} a total of $1.00 "
            f"by {abs(deviation)}",
            "reported as a mispricing only; see the multi-outcome scanner for whether "
            "a complete basket is actually executable",
        ],
        expected_resolution_time=quotable[0][0].market.expected_resolution_time,
        cost_breakdown={"sum_of_asks": str(total), "deviation": str(deviation)},
        provenance=quotable[0][0].market.provenance,
    )
