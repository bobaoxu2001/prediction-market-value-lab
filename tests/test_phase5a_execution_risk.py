"""Phase 5A: the risk gate and the kill switch.

Every limit gets a test that trips it and the conservative baseline gets a test
that passes, so a future change that accidentally disables a check fails here
rather than in an account.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_markets.execution import (
    CONSERVATIVE_LIMITS,
    AccountBalance,
    BlockReason,
    ExecutionMode,
    ExposureSnapshot,
    KillSwitch,
    Order,
    OrderAction,
    OrderIntent,
    OrderStatus,
    Outcome,
    Position,
    RiskGate,
    RiskVerdict,
)
from pmvl_shared.timeutil import utcnow


def _intent(**overrides: object) -> OrderIntent:
    now = utcnow()
    base: dict[str, object] = {
        "intent_id": "i-1",
        "ticker": "KXHIGHNY-26SEP04-B70",
        "outcome": Outcome.YES,
        "action": OrderAction.BUY,
        "limit_price_cents": 40,
        "quantity": 2,
        "mode": ExecutionMode.DEMO,
        "expected_edge_after_fees": Decimal("0.08"),
        "signal_timestamp": now,
        "market_data_timestamp": now,
        "decision_timestamp": now,
        "expires_at": now + timedelta(minutes=5),
    }
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _exposure(**overrides: object) -> ExposureSnapshot:
    base: dict[str, object] = {
        "balance": AccountBalance(
            balance_usd=Decimal("500"), available_usd=Decimal("500")
        ),
        "positions": (),
        "resting_orders": (),
        # Measured, not defaulted: these are None on a bare snapshot and the gate
        # now blocks on unmeasured cumulative state.
        "realized_loss_today_usd": Decimal("0"),
        "spend_today_usd": Decimal("0"),
        # An account snapshot with no observation time now blocks: exposure
        # measured at an unknown moment is not a measurement.
        "fetched_at": utcnow(),
    }
    base.update(overrides)
    return ExposureSnapshot(**base)  # type: ignore[arg-type]


def _gate(limits: object = None) -> RiskGate:
    """A gate with an unarmed kill switch.

    `RiskGate()` now defaults to the *configured* switch, which is armed unless a
    deployment opts in. These tests exercise the limits, so they supply a bare
    switch; the kill-switch tests below construct their own explicitly.
    """
    return RiskGate(limits, kill_switch=KillSwitch())  # type: ignore[arg-type]


def _reasons(decision: object) -> set[BlockReason]:
    return set(decision.reasons)  # type: ignore[attr-defined]


# ------------------------------------------------------------------- baseline
def test_conservative_demo_order_passes() -> None:
    decision = _gate().evaluate(_intent(), _exposure())
    assert decision.verdict is RiskVerdict.PASS
    assert decision.reasons == ()


def test_defaults_are_small() -> None:
    """A regression here means someone widened the caps without ratification."""
    assert CONSERVATIVE_LIMITS.max_contracts_per_order <= 10
    assert CONSERVATIVE_LIMITS.max_notional_per_order_usd <= Decimal("10")
    assert CONSERVATIVE_LIMITS.max_total_exposure_usd <= Decimal("100")
    assert CONSERVATIVE_LIMITS.max_open_markets <= 5


def test_balance_is_not_treated_as_risk_capital() -> None:
    """A large balance must not raise any cap."""
    rich = _exposure(
        balance=AccountBalance(
            balance_usd=Decimal("1000000"), available_usd=Decimal("1000000")
        )
    )
    decision = _gate().evaluate(_intent(quantity=50), rich)
    assert BlockReason.QUANTITY_OVER_CAP in _reasons(decision)


# --------------------------------------------------------------- per-order caps
def test_quantity_over_cap_blocks() -> None:
    decision = _gate().evaluate(_intent(quantity=99), _exposure())
    assert BlockReason.QUANTITY_OVER_CAP in _reasons(decision)


def test_notional_over_cap_blocks() -> None:
    limits = CONSERVATIVE_LIMITS.with_overrides(
        max_contracts_per_order=100, max_notional_per_order_usd=Decimal("1.00")
    )
    decision = _gate(limits).evaluate(_intent(quantity=5, limit_price_cents=90), _exposure())
    assert BlockReason.NOTIONAL_OVER_CAP in _reasons(decision)


def test_price_above_max_blocks() -> None:
    decision = _gate().evaluate(_intent(limit_price_cents=98, quantity=1), _exposure())
    assert BlockReason.PRICE_ABOVE_MAX in _reasons(decision)


def test_price_above_the_intents_own_ceiling_blocks() -> None:
    decision = _gate().evaluate(
        _intent(limit_price_cents=40, max_acceptable_price_cents=30), _exposure()
    )
    assert BlockReason.PRICE_ABOVE_MAX in _reasons(decision)


# ------------------------------------------------------------------- exposure
def test_market_exposure_over_cap_blocks() -> None:
    existing = Position(
        ticker="KXHIGHNY-26SEP04-B70",
        outcome=Outcome.YES,
        quantity=20,
        exposure_usd=Decimal("9.50"),
    )
    decision = _gate().evaluate(_intent(), _exposure(positions=(existing,)))
    assert BlockReason.MARKET_EXPOSURE_OVER_CAP in _reasons(decision)


def test_resting_orders_count_toward_market_exposure() -> None:
    """An account with no position but working bids is not flat."""
    resting = Order(
        exchange_order_id="x1",
        ticker="KXHIGHNY-26SEP04-B70",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        status=OrderStatus.OPEN,
        limit_price_cents=50,
        original_quantity=19,
        remaining_quantity=19,
    )
    decision = _gate().evaluate(_intent(), _exposure(resting_orders=(resting,)))
    assert BlockReason.MARKET_EXPOSURE_OVER_CAP in _reasons(decision)


def test_total_exposure_over_cap_blocks() -> None:
    positions = tuple(
        Position(
            ticker=f"T{i}", outcome=Outcome.YES, quantity=10, exposure_usd=Decimal("9")
        )
        for i in range(6)
    )
    limits = CONSERVATIVE_LIMITS.with_overrides(max_open_markets=99)
    decision = _gate(limits).evaluate(_intent(), _exposure(positions=positions))
    assert BlockReason.TOTAL_EXPOSURE_OVER_CAP in _reasons(decision)


def test_too_many_open_markets_blocks() -> None:
    positions = tuple(
        Position(
            ticker=f"T{i}",
            outcome=Outcome.YES,
            quantity=1,
            exposure_usd=Decimal("0.10"),
        )
        for i in range(3)
    )
    decision = _gate().evaluate(_intent(), _exposure(positions=positions))
    assert BlockReason.TOO_MANY_OPEN_MARKETS in _reasons(decision)


def test_adding_to_an_existing_market_does_not_count_as_a_new_market() -> None:
    positions = tuple(
        Position(
            ticker=f"T{i}",
            outcome=Outcome.YES,
            quantity=1,
            exposure_usd=Decimal("0.10"),
        )
        for i in range(3)
    )
    decision = _gate().evaluate(_intent(ticker="T0", quantity=1), _exposure(positions=positions))
    assert BlockReason.TOO_MANY_OPEN_MARKETS not in _reasons(decision)


def test_too_many_orders_in_one_market_blocks() -> None:
    resting = tuple(
        Order(
            exchange_order_id=f"x{i}",
            ticker="KXHIGHNY-26SEP04-B70",
            outcome=Outcome.YES,
            action=OrderAction.BUY,
            status=OrderStatus.OPEN,
            limit_price_cents=1,
            original_quantity=1,
            remaining_quantity=1,
        )
        for i in range(2)
    )
    decision = _gate().evaluate(_intent(), _exposure(resting_orders=resting))
    assert BlockReason.TOO_MANY_ORDERS_IN_MARKET in _reasons(decision)


def test_unknown_exposure_blocks() -> None:
    decision = _gate().evaluate(_intent(), None)
    assert BlockReason.EXPOSURE_UNKNOWN in _reasons(decision)


def test_missing_balance_blocks() -> None:
    decision = _gate().evaluate(_intent(), _exposure(balance=None))
    assert BlockReason.INSUFFICIENT_BALANCE in _reasons(decision)


def test_insufficient_balance_blocks() -> None:
    poor = _exposure(
        balance=AccountBalance(balance_usd=Decimal("0.10"), available_usd=Decimal("0.10"))
    )
    decision = _gate().evaluate(_intent(), poor)
    assert BlockReason.INSUFFICIENT_BALANCE in _reasons(decision)


# ----------------------------------------------------------------- daily caps
def test_daily_loss_limit_blocks() -> None:
    decision = _gate().evaluate(
        _intent(), _exposure(realized_loss_today_usd=Decimal("50"))
    )
    assert BlockReason.DAILY_LOSS_LIMIT in _reasons(decision)


def test_daily_spend_limit_blocks() -> None:
    decision = _gate().evaluate(_intent(), _exposure(spend_today_usd=Decimal("24.90")))
    assert BlockReason.DAILY_SPEND_LIMIT in _reasons(decision)


# ---------------------------------------------------------------------- edge
def test_edge_too_small_blocks() -> None:
    decision = _gate().evaluate(
        _intent(expected_edge_after_fees=Decimal("0.001")), _exposure()
    )
    assert BlockReason.EDGE_TOO_SMALL in _reasons(decision)


def test_unknown_edge_blocks() -> None:
    """Unmeasured is not the same as acceptable."""
    decision = _gate().evaluate(
        _intent(expected_edge_after_fees=None), _exposure()
    )
    assert BlockReason.EDGE_TOO_SMALL in _reasons(decision)


# ------------------------------------------------------------------ freshness
def test_stale_signal_blocks() -> None:
    old = utcnow() - timedelta(hours=2)
    decision = _gate().evaluate(_intent(signal_timestamp=old), _exposure())
    assert BlockReason.SIGNAL_STALE in _reasons(decision)


def test_missing_signal_timestamp_blocks() -> None:
    decision = _gate().evaluate(_intent(signal_timestamp=None), _exposure())
    assert BlockReason.SIGNAL_STALE in _reasons(decision)


def test_stale_market_data_blocks() -> None:
    old = utcnow() - timedelta(hours=2)
    decision = _gate().evaluate(_intent(market_data_timestamp=old), _exposure())
    assert BlockReason.MARKET_DATA_STALE in _reasons(decision)


def test_missing_market_data_timestamp_blocks() -> None:
    decision = _gate().evaluate(_intent(market_data_timestamp=None), _exposure())
    assert BlockReason.MARKET_DATA_STALE in _reasons(decision)


def test_expired_decision_blocks() -> None:
    decision = _gate().evaluate(
        _intent(expires_at=utcnow() - timedelta(seconds=1)), _exposure()
    )
    assert BlockReason.DECISION_EXPIRED in _reasons(decision)


def test_wide_spread_blocks() -> None:
    decision = _gate().evaluate(
        _intent(), _exposure(), best_bid_cents=20, best_ask_cents=60
    )
    assert BlockReason.SPREAD_TOO_WIDE in _reasons(decision)


# -------------------------------------------------------------- mode and state
def test_production_read_only_mode_blocks_at_the_risk_gate_too() -> None:
    """Gate 5, independent of the client and transport gates."""
    decision = _gate().evaluate(
        _intent(mode=ExecutionMode.PRODUCTION_READ_ONLY), _exposure()
    )
    assert BlockReason.MODE_NOT_WRITABLE in _reasons(decision)


def test_shadow_mode_is_evaluated_on_its_merits_not_auto_blocked() -> None:
    """Shadow exists to run the real risk arithmetic; a mode auto-block would
    make every shadow verdict meaningless."""
    decision = _gate().evaluate(_intent(mode=ExecutionMode.SHADOW), _exposure())
    assert decision.verdict is RiskVerdict.PASS


def test_shadow_still_blocks_on_a_real_limit() -> None:
    decision = _gate().evaluate(
        _intent(mode=ExecutionMode.SHADOW, quantity=99), _exposure()
    )
    assert BlockReason.QUANTITY_OVER_CAP in _reasons(decision)


def test_unresolved_orders_block_new_mutations() -> None:
    decision = _gate().evaluate(_intent(), _exposure(), unresolved_orders=1)
    assert BlockReason.UNRESOLVED_ORDER_STATE in _reasons(decision)


def test_all_failures_are_reported_not_just_the_first() -> None:
    decision = _gate().evaluate(
        _intent(quantity=99, limit_price_cents=99, expected_edge_after_fees=None),
        None,
    )
    assert len(decision.reasons) >= 3


# --------------------------------------------------------------- kill switch
def test_kill_switch_config_arm_blocks() -> None:
    gate = RiskGate(kill_switch=KillSwitch(config_disabled=True))
    decision = gate.evaluate(_intent(), _exposure())
    assert BlockReason.KILL_SWITCH in _reasons(decision)


def test_kill_switch_runtime_arm_blocks() -> None:
    switch = KillSwitch()
    gate = RiskGate(kill_switch=switch)
    assert gate.evaluate(_intent(), _exposure()).passed
    switch.engage("operator")
    assert BlockReason.KILL_SWITCH in _reasons(gate.evaluate(_intent(), _exposure()))


def test_kill_switch_persistent_arm_blocks(tmp_path: Path) -> None:
    flag = tmp_path / "EXECUTION_DISABLED"
    gate = RiskGate(kill_switch=KillSwitch(flag_path=flag))
    assert gate.evaluate(_intent(), _exposure()).passed
    flag.write_text("stopped")
    assert BlockReason.KILL_SWITCH in _reasons(gate.evaluate(_intent(), _exposure()))


def test_runtime_release_cannot_clear_the_config_arm() -> None:
    switch = KillSwitch(config_disabled=True)
    switch.release()
    assert switch.engaged is True


def test_runtime_release_cannot_clear_the_persistent_arm(tmp_path: Path) -> None:
    flag = tmp_path / "EXECUTION_DISABLED"
    flag.write_text("stopped")
    switch = KillSwitch(flag_path=flag)
    switch.release()
    assert switch.engaged is True


def test_kill_switch_fails_closed_when_the_flag_cannot_be_read() -> None:
    class Exploding:
        def exists(self) -> bool:
            raise OSError("permission denied")

        def __str__(self) -> str:
            return "<exploding>"

    assert KillSwitch(flag_path=Exploding()).engaged is True


def test_kill_switch_overrides_an_otherwise_valid_order() -> None:
    gate = RiskGate(kill_switch=KillSwitch(config_disabled=True))
    decision = gate.evaluate(_intent(), _exposure())
    assert decision.verdict is RiskVerdict.BLOCK
