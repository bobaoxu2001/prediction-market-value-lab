"""Pre-trade risk gate and kill switch. Nothing reaches a venue without passing.

Design rules
------------

**Fail closed.** Every check that cannot be evaluated blocks. Unknown current
exposure blocks; an absent market-data timestamp blocks; a missing balance
blocks. The alternative - "we could not measure it, so proceed" - is the failure
mode that turns a small bug into an unbounded position.

**Blocks are enumerated, not booleans.** A rejection carries every reason it
failed, so an operator sees the whole picture in one line instead of fixing one
limit and rediscovering the next.

**Demo is not exempt.** Every limit applies in DEMO exactly as it would in
production. Risk logic that has only ever run against real money has never been
tested; running it in the sandbox is how it earns trust before it matters.

**Defaults are deliberately tiny.** See :data:`CONSERVATIVE_LIMITS`. They are
sized so that a total failure of every other safeguard, in demo, is measured in
single-digit dollars of pretend money. Production-live limits are not derivable
from these and require explicit ratification in a later phase - notably, the
account balance is *not* treated as available risk capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .models import ExposureSnapshot, OrderIntent
from .modes import ExecutionMode

log = get_logger(__name__)

__all__ = [
    "RiskLimits",
    "RiskDecision",
    "RiskVerdict",
    "BlockReason",
    "RiskGate",
    "KillSwitch",
    "CONSERVATIVE_LIMITS",
]


class RiskVerdict(StrEnum):
    PASS = "pass"
    BLOCK = "block"


class BlockReason(StrEnum):
    KILL_SWITCH = "kill_switch"
    MODE_NOT_WRITABLE = "mode_not_writable"
    QUANTITY_OVER_CAP = "quantity_over_cap"
    NOTIONAL_OVER_CAP = "notional_over_cap"
    MARKET_EXPOSURE_OVER_CAP = "market_exposure_over_cap"
    TOTAL_EXPOSURE_OVER_CAP = "total_exposure_over_cap"
    TOO_MANY_OPEN_MARKETS = "too_many_open_markets"
    TOO_MANY_ORDERS_IN_MARKET = "too_many_orders_in_market"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    DAILY_SPEND_LIMIT = "daily_spend_limit"
    EDGE_TOO_SMALL = "edge_too_small"
    PRICE_ABOVE_MAX = "price_above_max"
    SPREAD_TOO_WIDE = "spread_too_wide"
    SIGNAL_STALE = "signal_stale"
    MARKET_DATA_STALE = "market_data_stale"
    DECISION_EXPIRED = "decision_expired"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    EXPOSURE_UNKNOWN = "exposure_unknown"
    UNRESOLVED_ORDER_STATE = "unresolved_order_state"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Hard caps. Every one is enforced on every mutating path.

    Sizes are absolute, not fractions of the balance. Expressing a cap as "2% of
    equity" makes the cap grow with the account, which is the opposite of what a
    safety limit is for while the strategy is unproven.
    """

    max_contracts_per_order: int
    max_notional_per_order_usd: Decimal
    max_exposure_per_market_usd: Decimal
    max_total_exposure_usd: Decimal
    max_open_markets: int
    max_open_orders_per_market: int
    max_daily_realized_loss_usd: Decimal
    max_daily_spend_usd: Decimal
    #: Minimum expected edge, in probability units, after fees. ``0.02`` = 2c.
    min_edge_after_fees: Decimal
    max_limit_price_cents: int
    max_spread_cents: int
    max_signal_age: timedelta
    max_market_data_age: timedelta
    #: Cash the account must retain beyond the new order's cost.
    min_balance_buffer_usd: Decimal = Decimal("0")

    def with_overrides(self, **kwargs: Any) -> "RiskLimits":
        return replace(self, **kwargs)


#: Phase 5A defaults. Small on purpose - see the module docstring.
CONSERVATIVE_LIMITS = RiskLimits(
    max_contracts_per_order=5,
    max_notional_per_order_usd=Decimal("5.00"),
    max_exposure_per_market_usd=Decimal("10.00"),
    max_total_exposure_usd=Decimal("50.00"),
    max_open_markets=3,
    max_open_orders_per_market=2,
    max_daily_realized_loss_usd=Decimal("10.00"),
    max_daily_spend_usd=Decimal("25.00"),
    min_edge_after_fees=Decimal("0.02"),
    max_limit_price_cents=95,
    max_spread_cents=5,
    max_signal_age=timedelta(seconds=300),
    max_market_data_age=timedelta(seconds=300),
    min_balance_buffer_usd=Decimal("0"),
)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    verdict: RiskVerdict
    reasons: tuple[BlockReason, ...] = ()
    detail: tuple[str, ...] = ()
    evaluated_at: datetime | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is RiskVerdict.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reasons": [r.value for r in self.reasons],
            "detail": list(self.detail),
        }


class KillSwitch:
    """Three independent ways to stop new execution, checked as a disjunction.

    * **configuration** - a settings flag, for a deployment that should never
      trade;
    * **runtime** - an in-process flag an operator or circuit breaker can trip
      without a restart;
    * **persistent file** - a path on disk whose mere existence disables
      execution, so that a crash-restart loop cannot clear the stop and so that
      an operator can disable a running system without touching its config.

    Reads and reconciliation deliberately remain available while killed: an
    operator whose kill switch also blinded them would be worse off.
    """

    def __init__(
        self,
        *,
        config_disabled: bool = False,
        flag_path: Any = None,
    ) -> None:
        self._config_disabled = bool(config_disabled)
        self._runtime_engaged = False
        self._runtime_reason = ""
        self._flag_path = flag_path

    @classmethod
    def from_settings(cls) -> "KillSwitch":
        """The switch as the deployment configured it - all three arms live.

        This is what any caller should get by default. ``KillSwitch()`` with no
        arguments is a switch that consults nothing, which is useful in tests and
        dangerous everywhere else.
        """
        from pathlib import Path

        from pmvl_shared.config import get_settings

        settings = get_settings()
        return cls(
            config_disabled=not settings.execution_enabled,
            flag_path=Path(settings.execution_kill_switch_path),
        )

    def engage(self, reason: str = "") -> None:
        self._runtime_engaged = True
        self._runtime_reason = reason or "engaged by operator"
        log.error("execution kill switch ENGAGED: %s", self._runtime_reason)

    def release(self) -> None:
        """Clear only the in-process flag.

        Cannot clear the config flag or delete the persistent file - releasing
        those is an explicit operator act outside this process.
        """
        self._runtime_engaged = False
        self._runtime_reason = ""
        log.warning("execution kill switch released (runtime flag only)")

    @property
    def persistent_engaged(self) -> bool:
        path = self._flag_path
        if path is None:
            return False
        try:
            return path.exists()
        except OSError:
            # Cannot tell. A kill switch that fails open is not a kill switch.
            return True

    @property
    def engaged(self) -> bool:
        return (
            self._config_disabled or self._runtime_engaged or self.persistent_engaged
        )

    @property
    def reason(self) -> str:
        if self._config_disabled:
            return "execution disabled by configuration"
        if self._runtime_engaged:
            return self._runtime_reason
        if self.persistent_engaged:
            return f"persistent kill flag present at {self._flag_path}"
        return ""


class RiskGate:
    """The only path from an :class:`OrderIntent` to a venue mutation."""

    def __init__(
        self,
        limits: RiskLimits | None = None,
        *,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.limits = limits or CONSERVATIVE_LIMITS
        # Default to the *configured* switch, not a blank one.
        #
        # `KillSwitch()` consults nothing: no config arm, no flag file. Making it
        # the default meant the most obvious line of code - `RiskGate()` - built a
        # gate with all three arms disabled, silently. Every other guard in this
        # subsystem is structural; this one was a convention, and conventions are
        # what the next caller forgets.
        self.kill_switch = kill_switch or KillSwitch.from_settings()

    def evaluate(
        self,
        intent: OrderIntent,
        exposure: ExposureSnapshot | None,
        *,
        now: datetime | None = None,
        best_bid_cents: int | None = None,
        best_ask_cents: int | None = None,
        unresolved_orders: int = 0,
    ) -> RiskDecision:
        """Evaluate every limit and return all failures, not just the first."""
        now = now or utcnow()
        reasons: list[BlockReason] = []
        detail: list[str] = []

        def block(reason: BlockReason, message: str) -> None:
            reasons.append(reason)
            detail.append(message)

        # ---- gates that do not depend on measurable state -------------------
        if self.kill_switch.engaged:
            block(BlockReason.KILL_SWITCH, self.kill_switch.reason)

        # Gate 5 fires for modes that touch a *real* account. SHADOW is exempt on
        # purpose: its whole value is running the genuine risk arithmetic on a
        # genuine intent, and a mode-based auto-block would make every shadow
        # evaluation return BLOCK for a reason that has nothing to do with the
        # order. Shadow cannot write regardless - the harness stops before the
        # submit, the execution client refuses to construct, and the transport
        # guard refuses the verb.
        if intent.mode.touches_real_account:
            block(
                BlockReason.MODE_NOT_WRITABLE,
                f"mode {intent.mode.value} may not mutate an account",
            )

        if unresolved_orders > 0:
            # An order whose remote state is unknown means our exposure estimate
            # is unreliable. Trading on an unreliable estimate is exactly what
            # reconciliation exists to prevent.
            block(
                BlockReason.UNRESOLVED_ORDER_STATE,
                f"{unresolved_orders} order(s) awaiting reconciliation",
            )

        # ---- per-order caps -------------------------------------------------
        if intent.quantity > self.limits.max_contracts_per_order:
            block(
                BlockReason.QUANTITY_OVER_CAP,
                f"quantity {intent.quantity} > cap "
                f"{self.limits.max_contracts_per_order}",
            )

        notional = intent.notional_usd
        if notional > self.limits.max_notional_per_order_usd:
            block(
                BlockReason.NOTIONAL_OVER_CAP,
                f"notional ${notional} > cap "
                f"${self.limits.max_notional_per_order_usd}",
            )

        if intent.limit_price_cents > self.limits.max_limit_price_cents:
            block(
                BlockReason.PRICE_ABOVE_MAX,
                f"limit {intent.limit_price_cents}c > cap "
                f"{self.limits.max_limit_price_cents}c",
            )

        if (
            intent.max_acceptable_price_cents is not None
            and intent.limit_price_cents > intent.max_acceptable_price_cents
        ):
            block(
                BlockReason.PRICE_ABOVE_MAX,
                f"limit {intent.limit_price_cents}c exceeds the intent's own "
                f"max {intent.max_acceptable_price_cents}c",
            )

        # ---- edge ------------------------------------------------------------
        edge = intent.expected_edge_after_fees
        if edge is None:
            block(
                BlockReason.EDGE_TOO_SMALL,
                "expected edge after fees is unknown",
            )
        elif edge < self.limits.min_edge_after_fees:
            block(
                BlockReason.EDGE_TOO_SMALL,
                f"edge {edge} < minimum {self.limits.min_edge_after_fees}",
            )

        # ---- freshness -------------------------------------------------------
        self._check_freshness(intent, now, block)

        # ---- spread ----------------------------------------------------------
        if best_bid_cents is not None and best_ask_cents is not None:
            spread = best_ask_cents - best_bid_cents
            if spread > self.limits.max_spread_cents:
                block(
                    BlockReason.SPREAD_TOO_WIDE,
                    f"spread {spread}c > cap {self.limits.max_spread_cents}c",
                )

        # ---- exposure --------------------------------------------------------
        if exposure is None:
            block(
                BlockReason.EXPOSURE_UNKNOWN,
                "current account exposure was not retrieved",
            )
        else:
            self._check_exposure(intent, exposure, notional, block)

        if reasons:
            decision = RiskDecision(
                verdict=RiskVerdict.BLOCK,
                reasons=tuple(reasons),
                detail=tuple(detail),
                evaluated_at=now,
            )
            log.warning(
                "risk BLOCK intent=%s ticker=%s reasons=%s",
                intent.intent_id,
                intent.ticker,
                ",".join(r.value for r in reasons),
            )
            return decision

        return RiskDecision(verdict=RiskVerdict.PASS, evaluated_at=now)

    # ------------------------------------------------------------------ helpers
    def _check_freshness(self, intent: OrderIntent, now: datetime, block: Any) -> None:
        if intent.signal_timestamp is None:
            block(BlockReason.SIGNAL_STALE, "signal timestamp missing")
        else:
            age = now - intent.signal_timestamp
            if age > self.limits.max_signal_age:
                block(
                    BlockReason.SIGNAL_STALE,
                    f"signal age {age.total_seconds():.0f}s > "
                    f"{self.limits.max_signal_age.total_seconds():.0f}s",
                )

        if intent.market_data_timestamp is None:
            block(BlockReason.MARKET_DATA_STALE, "market data timestamp missing")
        else:
            age = now - intent.market_data_timestamp
            if age > self.limits.max_market_data_age:
                block(
                    BlockReason.MARKET_DATA_STALE,
                    f"market data age {age.total_seconds():.0f}s > "
                    f"{self.limits.max_market_data_age.total_seconds():.0f}s",
                )

        if intent.expires_at is not None and now > intent.expires_at:
            block(
                BlockReason.DECISION_EXPIRED,
                f"intent expired at {intent.expires_at.isoformat()}",
            )

    def _check_exposure(
        self,
        intent: OrderIntent,
        exposure: ExposureSnapshot,
        notional: Decimal,
        block: Any,
    ) -> None:
        limits = self.limits

        # Existing position + resting orders + this order. Never assume flat.
        market_existing = exposure.position_exposure_usd(
            intent.ticker
        ) + exposure.resting_exposure_usd(intent.ticker)
        market_after = market_existing + notional
        if market_after > limits.max_exposure_per_market_usd:
            block(
                BlockReason.MARKET_EXPOSURE_OVER_CAP,
                f"{intent.ticker} exposure ${market_existing} + ${notional} "
                f"= ${market_after} > cap ${limits.max_exposure_per_market_usd}",
            )

        total_after = exposure.total_exposure_usd() + notional
        if total_after > limits.max_total_exposure_usd:
            block(
                BlockReason.TOTAL_EXPOSURE_OVER_CAP,
                f"total exposure ${total_after} > cap "
                f"${limits.max_total_exposure_usd}",
            )

        # A new market only counts against the cap when it is genuinely new.
        open_markets = exposure.open_market_count()
        is_new_market = market_existing == 0
        projected_markets = open_markets + (1 if is_new_market else 0)
        if projected_markets > limits.max_open_markets:
            block(
                BlockReason.TOO_MANY_OPEN_MARKETS,
                f"{projected_markets} open markets > cap {limits.max_open_markets}",
            )

        orders_here = exposure.open_order_count(intent.ticker)
        if orders_here + 1 > limits.max_open_orders_per_market:
            block(
                BlockReason.TOO_MANY_ORDERS_IN_MARKET,
                f"{orders_here + 1} orders in {intent.ticker} > cap "
                f"{limits.max_open_orders_per_market}",
            )

        if exposure.realized_loss_today_usd is None:
            block(
                BlockReason.EXPOSURE_UNKNOWN,
                "realized loss today was not measured",
            )
        elif exposure.realized_loss_today_usd > limits.max_daily_realized_loss_usd:
            block(
                BlockReason.DAILY_LOSS_LIMIT,
                f"realized loss today ${exposure.realized_loss_today_usd} > cap "
                f"${limits.max_daily_realized_loss_usd}",
            )

        if exposure.spend_today_usd is None:
            block(BlockReason.EXPOSURE_UNKNOWN, "spend today was not measured")
        elif exposure.spend_today_usd + notional > limits.max_daily_spend_usd:
            block(
                BlockReason.DAILY_SPEND_LIMIT,
                f"daily spend ${exposure.spend_today_usd} + ${notional} > cap "
                f"${limits.max_daily_spend_usd}",
            )

        balance = exposure.balance
        if balance is None:
            block(BlockReason.INSUFFICIENT_BALANCE, "balance was not retrieved")
        else:
            available = (
                balance.available_usd
                if balance.available_usd is not None
                else balance.balance_usd
            )
            if available - notional < limits.min_balance_buffer_usd:
                block(
                    BlockReason.INSUFFICIENT_BALANCE,
                    f"available ${available} - ${notional} would fall below "
                    f"buffer ${limits.min_balance_buffer_usd}",
                )
