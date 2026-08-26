"""Position sizing, and the ability to say no.

Not Kelly. Kelly is optimal against a probability you *know*; against one you
estimated it is a leverage multiplier applied to model error, and the estimates
here have never been shown to beat the market price. The existing value pipeline
already scales Kelly down by a configured fraction for that reason, and this
layer starts more conservative still: a flat notional bounded by liquidity,
capacity and a stack of exposure caps.

The most important return value is :data:`NO_TRADE`. A sizing function that
always produces a number cannot express "the uncertainty here is too high", so
every reason to decline would have to live somewhere upstream and would be lost
by the time anyone read the size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from pmvl_shared.money import ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.timeutil import ensure_utc


class DeclineReason(str, Enum):
    """Why no position was taken. A closed vocabulary, so refusals can be counted."""

    NO_EDGE = "no_edge"
    EDGE_BELOW_FLOOR = "edge_below_floor"
    UNCERTAINTY_TOO_HIGH = "uncertainty_too_high"
    FILL_PROBABILITY_TOO_LOW = "fill_probability_too_low"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    MARKET_EXPOSURE_CAP = "market_exposure_cap"
    CATEGORY_EXPOSURE_CAP = "category_exposure_cap"
    STRATEGY_EXPOSURE_CAP = "strategy_exposure_cap"
    DAILY_DEPLOYMENT_CAP = "daily_deployment_cap"
    CORRELATION_CAP = "correlation_cap"
    BELOW_MIN_ORDER_SIZE = "below_min_order_size"


@dataclass(frozen=True)
class SizeDecision:
    """How much to deploy, or why nothing.

    ``binding_constraint`` names which cap actually decided the number, not just
    that some cap applied. Over a research run that field answers "is this
    strategy small because the edge is small or because one exposure limit is
    doing all the work", and those have very different fixes.
    """

    contracts: Decimal
    notional_usd: Decimal
    declined: DeclineReason | None = None
    binding_constraint: str = ""
    detail: str = ""
    considered: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        return self.contracts > ZERO and self.declined is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "contracts": str(self.contracts),
            "notional_usd": str(self.notional_usd),
            "declined": self.declined.value if self.declined else None,
            "binding_constraint": self.binding_constraint,
            "detail": self.detail,
            "considered": {k: str(v) for k, v in self.considered.items()},
        }


def NO_TRADE(reason: DeclineReason, detail: str = "") -> SizeDecision:  # noqa: N802
    """The explicit refusal. Named in capitals because it is a decision.

    Spelled as a constructor rather than a singleton so a refusal can carry the
    reason that produced it - a bare sentinel would collapse eleven different
    findings into one.
    """
    return SizeDecision(
        contracts=ZERO, notional_usd=ZERO, declined=reason, detail=detail
    )


@dataclass(frozen=True)
class ExposureLimits:
    """Every cap, in one reviewable object.

    Defaults are small on purpose. This is a research system with no
    demonstrated edge; the correct default position size is one whose loss
    teaches something and does not matter.
    """

    #: Notional per position, before any cap applies.
    base_notional_usd: Decimal = Decimal("25")
    #: Ceiling on any single market.
    max_per_market_usd: Decimal = Decimal("50")
    #: Ceiling on all open positions in one category.
    max_per_category_usd: Decimal = Decimal("200")
    #: Ceiling on all open positions for one strategy.
    max_per_strategy_usd: Decimal = Decimal("500")
    #: Ceiling on capital deployed in one calendar day.
    max_daily_deployment_usd: Decimal = Decimal("250")
    #: Ceiling on a correlated cluster - siblings of one event, say.
    max_per_correlation_group_usd: Decimal = Decimal("75")
    #: Share of observed profitable depth this may consume.
    max_share_of_capacity: Decimal = Decimal("0.10")
    #: Minimum per-contract edge worth acting on at all.
    min_edge_per_contract: Decimal = Decimal("0.02")
    #: Widest outcome interval that still permits a position.
    max_interval_width: Decimal = Decimal("0.35")
    #: Lowest fill probability worth resting an order for.
    min_fill_probability: Decimal = Decimal("0.10")


@dataclass
class ExposureLedger:
    """Running exposure, so the caps mean something across a session.

    Mutable by design and updated only by :meth:`commit`. A sizing function that
    read a ledger without anybody writing to it would apply every cap to the
    first position and none to the rest.
    """

    per_market: dict[str, Decimal] = field(default_factory=dict)
    per_category: dict[str, Decimal] = field(default_factory=dict)
    per_strategy: dict[str, Decimal] = field(default_factory=dict)
    per_day: dict[str, Decimal] = field(default_factory=dict)
    per_correlation_group: dict[str, Decimal] = field(default_factory=dict)

    def commit(
        self,
        *,
        notional: Decimal,
        market_key: str,
        category: str,
        strategy: str,
        day: str,
        correlation_group: str,
    ) -> None:
        self.per_market[market_key] = self.per_market.get(market_key, ZERO) + notional
        self.per_category[category] = self.per_category.get(category, ZERO) + notional
        self.per_strategy[strategy] = self.per_strategy.get(strategy, ZERO) + notional
        self.per_day[day] = self.per_day.get(day, ZERO) + notional
        self.per_correlation_group[correlation_group] = (
            self.per_correlation_group.get(correlation_group, ZERO) + notional
        )

    def release(
        self,
        *,
        notional: Decimal,
        market_key: str,
        category: str,
        strategy: str,
        correlation_group: str,
    ) -> None:
        """Free exposure when a position closes.

        The daily deployment total is deliberately **not** released. It measures
        capital committed that day, and a strategy that opened and closed twenty
        positions before lunch has deployed twenty positions' worth of capital
        regardless of how much is open now.
        """
        for bucket, key in (
            (self.per_market, market_key),
            (self.per_category, category),
            (self.per_strategy, strategy),
            (self.per_correlation_group, correlation_group),
        ):
            bucket[key] = max(ZERO, bucket.get(key, ZERO) - notional)


def size_position(
    *,
    edge_per_contract: Decimal,
    entry_price: Decimal,
    interval_width: Decimal,
    capacity_usd: Decimal,
    min_order_size: Decimal,
    market_key: str,
    category: str,
    strategy: str,
    day: str,
    correlation_group: str,
    limits: ExposureLimits = ExposureLimits(),
    ledger: ExposureLedger | None = None,
    fill_probability: Decimal | None = None,
) -> SizeDecision:
    """Decide a position size, or decline with a reason.

    Order of checks matters and is chosen so the *most informative* refusal wins.
    "No edge" is a finding about the signal; "category cap" is a finding about
    the portfolio. Testing the signal first means a research run's refusal
    histogram describes the strategy rather than the order in which markets
    happened to be visited.
    """
    ledger = ledger if ledger is not None else ExposureLedger()
    considered: dict[str, Decimal] = {}

    if edge_per_contract <= ZERO:
        return NO_TRADE(DeclineReason.NO_EDGE, "the conservative edge is not positive")
    if edge_per_contract < limits.min_edge_per_contract:
        return NO_TRADE(
            DeclineReason.EDGE_BELOW_FLOOR,
            f"edge {edge_per_contract} below the {limits.min_edge_per_contract} floor",
        )
    if interval_width > limits.max_interval_width:
        return NO_TRADE(
            DeclineReason.UNCERTAINTY_TOO_HIGH,
            f"outcome interval is {interval_width} wide, above "
            f"{limits.max_interval_width}; the estimate is not precise enough to size",
        )
    if fill_probability is not None and fill_probability < limits.min_fill_probability:
        return NO_TRADE(
            DeclineReason.FILL_PROBABILITY_TOO_LOW,
            f"estimated fill probability {fill_probability} below "
            f"{limits.min_fill_probability}",
        )
    if entry_price <= ZERO or entry_price >= ONE:
        return NO_TRADE(
            DeclineReason.NO_EDGE, f"entry price {entry_price} is outside (0, 1)"
        )

    # Every cap, expressed as a notional headroom. The smallest one wins, and
    # which one it was is reported.
    headrooms: list[tuple[str, Decimal, DeclineReason]] = [
        ("base", limits.base_notional_usd, DeclineReason.NO_EDGE),
        (
            "capacity",
            quantize_usd(capacity_usd * limits.max_share_of_capacity),
            DeclineReason.INSUFFICIENT_CAPACITY,
        ),
        (
            "per_market",
            limits.max_per_market_usd - ledger.per_market.get(market_key, ZERO),
            DeclineReason.MARKET_EXPOSURE_CAP,
        ),
        (
            "per_category",
            limits.max_per_category_usd - ledger.per_category.get(category, ZERO),
            DeclineReason.CATEGORY_EXPOSURE_CAP,
        ),
        (
            "per_strategy",
            limits.max_per_strategy_usd - ledger.per_strategy.get(strategy, ZERO),
            DeclineReason.STRATEGY_EXPOSURE_CAP,
        ),
        (
            "per_day",
            limits.max_daily_deployment_usd - ledger.per_day.get(day, ZERO),
            DeclineReason.DAILY_DEPLOYMENT_CAP,
        ),
        (
            "per_correlation_group",
            limits.max_per_correlation_group_usd
            - ledger.per_correlation_group.get(correlation_group, ZERO),
            DeclineReason.CORRELATION_CAP,
        ),
    ]
    for name, value, _ in headrooms:
        considered[name] = quantize_usd(value)

    binding_name, binding_value, binding_reason = min(
        headrooms, key=lambda item: item[1]
    )
    if binding_value <= ZERO:
        return SizeDecision(
            contracts=ZERO,
            notional_usd=ZERO,
            declined=binding_reason,
            binding_constraint=binding_name,
            detail=f"{binding_name} headroom is {quantize_usd(binding_value)}",
            considered=considered,
        )

    contracts = (
        safe_div(binding_value, entry_price).to_integral_value(rounding="ROUND_FLOOR")
    )
    if contracts < max(min_order_size, ONE):
        return SizeDecision(
            contracts=ZERO,
            notional_usd=ZERO,
            declined=DeclineReason.BELOW_MIN_ORDER_SIZE,
            binding_constraint=binding_name,
            detail=(
                f"{binding_name} headroom of {quantize_usd(binding_value)} buys "
                f"{contracts} contracts at {entry_price}, below the venue minimum "
                f"of {min_order_size}"
            ),
            considered=considered,
        )

    return SizeDecision(
        contracts=contracts,
        notional_usd=quantize_usd(contracts * entry_price),
        binding_constraint=binding_name,
        detail=f"bounded by {binding_name}",
        considered=considered,
    )


def scale_for_uncertainty(
    notional: Decimal, interval_width: Decimal, *, floor: Decimal = Decimal("0.25")
) -> Decimal:
    """Shrink a notional as the outcome interval widens.

    Offered as a separate, opt-in step rather than folded into
    :func:`size_position`, because it is a *preference* about risk appetite and
    the caps above are *limits*. Mixing the two would make a limit look
    negotiable.
    """
    if interval_width <= ZERO:
        return notional
    factor = max(floor, ONE - interval_width)
    return quantize_usd(notional * factor)


def kelly_fraction_reference(
    probability: Decimal, cost: Decimal, *, fraction: Decimal = Decimal("0.05")
) -> Decimal:
    """Fractional Kelly, provided for **comparison only**.

    Deliberately not wired into :func:`size_position`. It is here so a research
    report can show what Kelly would have said next to what the bounded sizer
    actually did - which is the honest way to argue about whether the caps are
    too tight, rather than switching to Kelly and finding out.

    The default fraction is 5%, an order of magnitude below full Kelly, and even
    that is optimistic against probabilities this system has not shown to beat
    the market price.
    """
    if cost <= ZERO or cost >= ONE:
        return ZERO
    edge = probability - cost
    if edge <= ZERO:
        return ZERO
    return quantize_usd(safe_div(edge, ONE - cost) * fraction)


def day_key(timestamp: datetime) -> str:
    """UTC date string used to bucket daily deployment.

    UTC, not local. A cap that resets at the operator's midnight resets at a
    different instant depending on where the process runs, and two runs of the
    same research would then disagree about how much was deployed on which day.
    """
    return ensure_utc(timestamp).strftime("%Y-%m-%d")
