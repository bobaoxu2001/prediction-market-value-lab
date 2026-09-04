"""Phase 5A: idempotency, the execution ledger, and restart recovery."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_markets.execution import (
    ExecutionLedger,
    ExecutionMode,
    Fill,
    Order,
    OrderAction,
    OrderIntent,
    OrderStatus,
    Outcome,
    Position,
    derive_client_order_id,
    new_attempt_id,
    recover_after_restart,
)
from pmvl_markets.execution.ledger import InvalidTransition
from pmvl_markets.execution.models import is_valid_transition
from pmvl_shared.timeutil import utcnow


def _intent(**overrides: object) -> OrderIntent:
    now = utcnow()
    base: dict[str, object] = {
        "intent_id": "i-1",
        "ticker": "KXHIGHNY-26SEP04-B70",
        "outcome": Outcome.YES,
        "action": OrderAction.BUY,
        "limit_price_cents": 40,
        "quantity": 4,
        "mode": ExecutionMode.DEMO,
        "strategy": "weather-v1",
        "strategy_version": "3",
        "decision_epoch": "2026-09-04T18:00",
        "source_signal_id": "sig-42",
        "expected_edge_after_fees": Decimal("0.08"),
        "signal_timestamp": now,
        "market_data_timestamp": now,
        "expires_at": now + timedelta(minutes=5),
    }
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    return ExecutionLedger(tmp_path / "execution.db")


# ----------------------------------------------------------------- idempotency
def test_same_intent_yields_the_same_client_order_id() -> None:
    assert derive_client_order_id(_intent()) == derive_client_order_id(_intent())


def test_client_order_id_is_stable_across_processes() -> None:
    """No clock, no randomness, no process state in the derivation."""
    first = derive_client_order_id(_intent())
    import importlib

    import pmvl_markets.execution.idempotency as mod

    importlib.reload(mod)
    assert mod.derive_client_order_id(_intent()) == first


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 1),
        ("limit_price_cents", 41),
        ("quantity", 5),
        ("ticker", "KXHIGHNY-26SEP04-B72"),
        ("outcome", Outcome.NO),
        ("action", OrderAction.SELL),
        ("decision_epoch", "2026-09-04T19:00"),
        ("source_signal_id", "sig-43"),
        ("strategy_version", "4"),
    ],
)
def test_a_materially_different_order_gets_a_different_id(
    field: str, value: object
) -> None:
    assert derive_client_order_id(_intent()) != derive_client_order_id(
        _intent(**{field: value})
    )


def test_attempt_id_is_not_the_idempotency_key() -> None:
    assert new_attempt_id() != new_attempt_id()


def test_shadow_and_demo_never_share_an_identity() -> None:
    assert derive_client_order_id(
        _intent(mode=ExecutionMode.SHADOW)
    ) != derive_client_order_id(_intent(mode=ExecutionMode.DEMO))


# --------------------------------------------------------------------- ledger
def test_intent_is_persisted(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        assert ledger.record_intent(con, intent, coid) is True
        record = ledger.get(con, coid)
    assert record is not None
    assert record.ticker == intent.ticker
    assert record.status is OrderStatus.INTENDED
    assert record.requested_quantity == 4


def test_duplicate_logical_intent_is_deduplicated(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        assert ledger.record_intent(con, intent, coid) is True
        assert ledger.record_intent(con, intent, coid) is False
        rows = ledger.all_orders(con)
    assert len(rows) == 1


def test_invalid_transition_is_refused(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        with pytest.raises(InvalidTransition):
            # INTENDED cannot jump straight to FILLED.
            ledger.transition(con, coid, OrderStatus.FILLED)


def test_terminal_states_are_terminal() -> None:
    for terminal in (
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.BLOCKED,
    ):
        assert not is_valid_transition(terminal, OrderStatus.OPEN)


def test_transitions_are_audited(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        ledger.transition(con, coid, OrderStatus.OPEN, exchange_order_id="X1")
        rows = ledger.transitions_for(con, coid)
    assert [r["to_status"] for r in rows] == ["intended", "submitting", "open"]


def test_ledger_stores_no_secret_columns(ledger: ExecutionLedger) -> None:
    with ledger.connect() as con:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        columns: list[str] = []
        for table in tables:
            columns += [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    lowered = " ".join(columns).lower()
    for banned in ("private_key", "pem", "secret", "signature", "api_key", "token"):
        assert banned not in lowered


def test_execution_ledger_is_a_separate_file_from_pmvl_db(
    ledger: ExecutionLedger,
) -> None:
    with ledger.connect() as con:
        con.execute("SELECT 1")
    assert ledger.path.name == "execution.db"
    assert "pmvl.db" not in str(ledger.path)


# ----------------------------------------------------------------------- fills
def test_zero_fills_leaves_the_order_unfilled(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        record = ledger.get(con, coid)
    assert record is not None and record.filled_quantity == 0


def _fill(fill_id: str, qty: int, price: int, fee: str | None = "0.01") -> Fill:
    return Fill(
        fill_id=fill_id,
        ticker="KXHIGHNY-26SEP04-B70",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        quantity=qty,
        price_cents=price,
        exchange_order_id="X1",
        fee_usd=Decimal(fee) if fee is not None else None,
    )


def test_partial_fill_is_neither_zero_nor_full(ledger: ExecutionLedger) -> None:
    intent = _intent(quantity=10)
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.record_fill(con, _fill("f1", 3, 40), client_order_id=coid)
        record = ledger.get(con, coid)
    assert record is not None
    assert record.filled_quantity == 3
    assert record.remaining_quantity == 7


def test_multiple_fills_average_correctly(ledger: ExecutionLedger) -> None:
    intent = _intent(quantity=10)
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.record_fill(con, _fill("f1", 2, 40), client_order_id=coid)
        ledger.record_fill(con, _fill("f2", 2, 50), client_order_id=coid)
        record = ledger.get(con, coid)
    assert record is not None
    assert record.filled_quantity == 4
    assert record.average_fill_price_cents == Decimal("45")


def test_fill_ingestion_is_idempotent(ledger: ExecutionLedger) -> None:
    intent = _intent(quantity=10)
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        assert ledger.record_fill(con, _fill("f1", 3, 40), client_order_id=coid) is True
        assert ledger.record_fill(con, _fill("f1", 3, 40), client_order_id=coid) is False
        assert ledger.record_fill(con, _fill("f1", 3, 40), client_order_id=coid) is False
        record = ledger.get(con, coid)
    assert record is not None and record.filled_quantity == 3


def test_unknown_fee_keeps_total_fees_unknown(ledger: ExecutionLedger) -> None:
    """One unpriced fill must not let the total read as merely the known part."""
    intent = _intent(quantity=10)
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.record_fill(con, _fill("f1", 2, 40, fee="0.02"), client_order_id=coid)
        ledger.record_fill(con, _fill("f2", 2, 40, fee=None), client_order_id=coid)
        record = ledger.get(con, coid)
    assert record is not None and record.fees_usd is None


# ------------------------------------------------------------ restart recovery
def test_restart_reloads_state_without_resubmitting(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        ledger.transition(con, coid, OrderStatus.OPEN, exchange_order_id="X1")

    remote = Order(
        exchange_order_id="X1",
        ticker=intent.ticker,
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        status=OrderStatus.OPEN,
        limit_price_cents=40,
        original_quantity=4,
        remaining_quantity=4,
        client_order_id=coid,
    )
    report = recover_after_restart(ledger, [remote], [], [])
    assert report.is_clean

    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None and record.status is OrderStatus.OPEN


def test_restart_does_not_duplicate_an_order(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)

    remote = Order(
        exchange_order_id="X1",
        ticker=intent.ticker,
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        status=OrderStatus.OPEN,
        original_quantity=4,
        remaining_quantity=4,
        client_order_id=coid,
    )
    recover_after_restart(ledger, [remote], [], [])
    # Re-recording the same intent after recovery must be a no-op.
    with ledger.connect() as con:
        assert ledger.record_intent(con, intent, coid) is False
        assert len(ledger.all_orders(con)) == 1


def test_an_order_that_vanished_without_fills_is_not_called_filled(
    ledger: ExecutionLedger,
) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        ledger.transition(con, coid, OrderStatus.OPEN, exchange_order_id="X1")

    report = recover_after_restart(ledger, [], [], [])
    assert not report.is_clean

    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    assert record.filled_quantity == 0


def test_position_mismatch_is_flagged(ledger: ExecutionLedger) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        ledger.transition(con, coid, OrderStatus.OPEN, exchange_order_id="X1")
        ledger.record_fill(con, _fill("f1", 4, 40), client_order_id=coid)

    remote = Order(
        exchange_order_id="X1",
        ticker=intent.ticker,
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        status=OrderStatus.FILLED,
        original_quantity=4,
        filled_quantity=4,
        remaining_quantity=0,
        client_order_id=coid,
    )
    wrong_position = Position(ticker=intent.ticker, outcome=Outcome.YES, quantity=99)
    report = recover_after_restart(ledger, [remote], [], [wrong_position])
    kinds = {d.kind.value for d in report.discrepancies}
    assert "position_mismatch" in kinds
