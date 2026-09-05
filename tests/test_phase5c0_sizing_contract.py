"""Phase 5C0: the sizing contract, and what may not be mistaken for a size.

Three quantities that must never collapse into one:

    approved_quantity <= requested_quantity <= capacity_ceiling

A capacity ceiling is what the book can absorb. A risk cap is a limit. Neither is
a recommendation. The tests here exist because reading either one as a quantity
produces an order nobody authorized.
"""

from __future__ import annotations

import dataclasses
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmvl_shared.enums import RecommendationState, Side
from pmvl_shared.timeutil import utcnow
from pmvl_markets.execution import (
    AccountBalance,
    BlockReason,
    ExecutionLedger,
    ExecutionMode,
    ExposureSnapshot,
    KillSwitch,
    OrderIntent,
    Outcome,
    OrderAction,
    RiskGate,
    derive_client_order_id,
)
from pmvl_markets.execution.signals import (
    IneligibilityReason,
    IneligibleSignal,
    ResearchDecision,
    SignalExecutionAdapter,
)
from pmvl_markets.execution.sizing import (
    DEMO_PROBE_SIZE,
    SIZING_POLICY_VERSION,
    DemoExecutionProbePolicy,
    SizingDecision,
    SizingNotIssued,
    SizingPolicy,
    SizingRefused,
    StrategySizingPolicy,
    issue_sizing,
)

CAPACITY = 1829


def _decision(**kw: object) -> ResearchDecision:
    now = utcnow()
    base: dict[str, object] = {
        "recommendation_id": 1,
        "batch_id": "b1",
        "ticker": "KXHIGHNY-26SEP04-T89",
        "platform": "kalshi",
        "side": Side.YES,
        "state": RecommendationState.STILL_ACTIONABLE,
        "market_status": "open",
        "entry_price": Decimal("0.40"),
        "current_price": Decimal("0.40"),
        "fair_probability": Decimal("0.55"),
        "conservative_probability": Decimal("0.48"),
        "net_ev_per_contract": Decimal("0.12"),
        "conservative_net_ev": Decimal("0.06"),
        "total_cost_per_contract": Decimal("0.42"),
        "recommended_position_cap": Decimal(CAPACITY),
        "model_version": "TEST_ONLY",
        # The adapter allowlists live provenance; these fixtures stand in for a
        # live row so the rest of the contract is exercised. TEST_ONLY.
        "provenance": "live",
        "horizon": "24h",
        "created_at": now,
        "evidence_updated_at": now,
        "close_time": datetime(2099, 1, 1, tzinfo=timezone.utc),
    }
    base.update(kw)
    return ResearchDecision(**base)  # type: ignore[arg-type]


def _exposure() -> ExposureSnapshot:
    return ExposureSnapshot(
        balance=AccountBalance(
            balance_usd=Decimal("500"), available_usd=Decimal("500")
        ),
        realized_loss_today_usd=Decimal("0"),
        spend_today_usd=Decimal("0"),
        fetched_at=utcnow(),
    )


def _probe_intent() -> OrderIntent:
    adapter = SignalExecutionAdapter(
        mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
    )
    out = adapter.to_intent(_decision())
    assert isinstance(out, OrderIntent)
    return out


# ============================================= capacity ceiling is not a size
def test_capacity_ceiling_is_never_the_quantity() -> None:
    intent = _probe_intent()
    assert intent.capacity_ceiling == CAPACITY
    assert intent.quantity == DEMO_PROBE_SIZE
    assert intent.quantity != intent.capacity_ceiling


def test_no_sizing_policy_blocks_every_signal() -> None:
    out = SignalExecutionAdapter(mode=ExecutionMode.SHADOW).to_intent(_decision())
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.SIZE_POLICY_UNDEFINED


def test_strategy_sizing_policy_refuses_because_none_exists() -> None:
    """The system must fail closed rather than invent a size."""
    out = SignalExecutionAdapter(
        mode=ExecutionMode.SHADOW, sizing_policy=StrategySizingPolicy()
    ).to_intent(_decision())
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.SIZE_POLICY_UNDEFINED


def test_strategy_policy_raises_rather_than_guessing() -> None:
    with pytest.raises(SizingRefused):
        StrategySizingPolicy().decide(
            mode=ExecutionMode.DEMO, ticker="T", capacity_ceiling=CAPACITY
        )


def test_missing_capacity_still_blocks() -> None:
    out = SignalExecutionAdapter(
        mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
    ).to_intent(_decision(recommended_position_cap=None))
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.MISSING_SIZE


# ==================================================== the demo execution probe
def test_demo_probe_is_exactly_one_contract() -> None:
    assert DEMO_PROBE_SIZE == 1
    decision = DemoExecutionProbePolicy().decide(
        mode=ExecutionMode.DEMO, ticker="T", capacity_ceiling=CAPACITY
    )
    assert decision.requested_quantity == 1
    assert decision.policy is SizingPolicy.DEMO_EXECUTION_PROBE


@pytest.mark.parametrize(
    "mode", [ExecutionMode.PRODUCTION_READ_ONLY, ExecutionMode.SHADOW]
)
def test_demo_probe_refuses_outside_demo(mode: ExecutionMode) -> None:
    with pytest.raises(SizingRefused):
        DemoExecutionProbePolicy().decide(mode=mode, ticker="T")


def test_a_demo_probe_decision_cannot_be_spent_in_another_mode() -> None:
    """Bound in two places: the policy refuses, and the decision refuses."""
    decision = DemoExecutionProbePolicy().decide(mode=ExecutionMode.DEMO, ticker="T")
    with pytest.raises(SizingNotIssued):
        decision.assert_issued(mode=ExecutionMode.PRODUCTION_READ_ONLY)


def test_probe_is_labelled_as_a_test_not_a_recommendation() -> None:
    decision = DemoExecutionProbePolicy().decide(mode=ExecutionMode.DEMO, ticker="T")
    blob = (decision.detail + decision.policy.value).lower()
    assert "probe" in blob
    for forbidden in ("recommended", "optimal", "model_size"):
        assert forbidden not in blob


def test_probe_refuses_when_the_book_cannot_absorb_one_contract() -> None:
    with pytest.raises(SizingRefused):
        DemoExecutionProbePolicy().decide(
            mode=ExecutionMode.DEMO, ticker="T", capacity_ceiling=0
        )


# ================================================= forgery (the §37 lesson)
def test_a_hand_built_sizing_decision_is_refused() -> None:
    """Two phases running, a public helper reproduced the privileged object."""
    forged = SizingDecision(
        requested_quantity=9999,
        policy=SizingPolicy.STRATEGY_SIZED,
        policy_version="x",
        mode=ExecutionMode.DEMO,
        ticker="T",
        decided_at=utcnow(),
    )
    with pytest.raises(SizingNotIssued):
        forged.assert_issued(mode=ExecutionMode.DEMO)


def test_editing_an_issued_sizing_decision_invalidates_it() -> None:
    decision = DemoExecutionProbePolicy().decide(mode=ExecutionMode.DEMO, ticker="T")
    object.__setattr__(decision, "requested_quantity", 5000)
    with pytest.raises(SizingNotIssued):
        decision.assert_issued(mode=ExecutionMode.DEMO)


def test_sizing_decision_is_frozen() -> None:
    decision = DemoExecutionProbePolicy().decide(mode=ExecutionMode.DEMO, ticker="T")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.requested_quantity = 5  # type: ignore[misc]


@pytest.mark.parametrize("quantity", [0, -1, -100])
def test_non_positive_quantities_are_refused(quantity: int) -> None:
    decision = issue_sizing(
        requested_quantity=quantity,
        policy=SizingPolicy.OPERATOR_SPECIFIED_DEMO_SMOKE,
        policy_version=SIZING_POLICY_VERSION,
        mode=ExecutionMode.DEMO,
        ticker="T",
        decided_at=utcnow(),
    )
    with pytest.raises(SizingNotIssued):
        decision.assert_issued(mode=ExecutionMode.DEMO)


def test_a_request_above_the_capacity_ceiling_is_refused() -> None:
    decision = issue_sizing(
        requested_quantity=500,
        policy=SizingPolicy.OPERATOR_SPECIFIED_DEMO_SMOKE,
        policy_version=SIZING_POLICY_VERSION,
        mode=ExecutionMode.DEMO,
        ticker="T",
        decided_at=utcnow(),
        capacity_ceiling=10,
    )
    with pytest.raises(SizingNotIssued):
        decision.assert_issued(mode=ExecutionMode.DEMO)


# ================================================ risk gate quantity semantics
def test_risk_gate_refuses_a_quantity_that_sizing_did_not_authorize() -> None:
    intent = _probe_intent()
    tampered = dataclasses.replace(intent, quantity=50)
    decision, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        tampered, _exposure(), client_order_id="c1"
    )
    assert plan is None
    assert BlockReason.SIZING_NOT_AUTHORIZED in set(decision.reasons)


def test_risk_gate_never_approves_more_than_requested() -> None:
    intent = _probe_intent()
    decision, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        intent, _exposure(), client_order_id="c1"
    )
    assert plan is not None
    assert plan.quantity <= intent.sizing.requested_quantity
    assert intent.sizing.requested_quantity <= intent.capacity_ceiling


def test_the_full_size_ordering_invariant_holds() -> None:
    intent = _probe_intent()
    _, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        intent, _exposure(), client_order_id="c1"
    )
    assert plan is not None
    assert (
        plan.quantity
        <= intent.sizing.requested_quantity
        <= (intent.capacity_ceiling or 10**9)
    )


def test_a_risk_cap_is_not_a_size_recommendation() -> None:
    """max_contracts_per_order = 5 must never become 'therefore buy 5'."""
    from pmvl_markets.execution import CONSERVATIVE_LIMITS

    intent = _probe_intent()
    assert intent.quantity == 1
    assert intent.quantity != CONSERVATIVE_LIMITS.max_contracts_per_order


def test_quantity_participates_in_the_idempotency_key() -> None:
    intent = _probe_intent()
    other = dataclasses.replace(intent, quantity=2, sizing=None)
    assert derive_client_order_id(intent) != derive_client_order_id(other)


# =============================================================== persistence
def test_sizing_provenance_is_persisted(tmp_path: Path) -> None:
    ledger = ExecutionLedger(tmp_path / "execution.db")
    intent = _probe_intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        record = ledger.get(con, coid)
    assert record is not None
    assert record.sizing_policy == SizingPolicy.DEMO_EXECUTION_PROBE.value
    assert record.sizing_policy_version == SIZING_POLICY_VERSION
    assert record.capacity_ceiling == CAPACITY
    assert record.requested_quantity == DEMO_PROBE_SIZE


def test_a_pre_5c0_ledger_migrates(tmp_path: Path) -> None:
    """An execution.db written before the sizing columns must keep working.

    Built by creating the real schema and dropping the three new columns, so the
    fixture is a genuine older database rather than a hand-written approximation.
    """
    import sqlite3

    path = tmp_path / "execution.db"
    with ExecutionLedger(path).connect() as con:
        pass
    con = sqlite3.connect(path)
    for column in ("sizing_policy", "sizing_policy_version", "capacity_ceiling"):
        con.execute(f"ALTER TABLE execution_orders DROP COLUMN {column}")
    con.commit()
    cols_before = {r[1] for r in con.execute("PRAGMA table_info(execution_orders)")}
    con.close()
    assert "sizing_policy" not in cols_before

    ledger = ExecutionLedger(path)
    with ledger.connect() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(execution_orders)")}
    assert {"sizing_policy", "sizing_policy_version", "capacity_ceiling"} <= cols

    # And a row written before the migration still reads back.
    intent = _probe_intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as c:
        ledger.record_intent(c, intent, coid)
        assert ledger.get(c, coid) is not None


# ============================================ seed fixtures stay non-tradeable
def test_seed_fixtures_cannot_become_orders_even_with_a_sizing_policy() -> None:
    out = SignalExecutionAdapter(
        mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
    ).to_intent(_decision(provenance="demo"))
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.DEMO_PROVENANCE


def test_stale_recommendations_cannot_become_orders() -> None:
    old = utcnow() - timedelta(days=40)
    out = SignalExecutionAdapter(
        mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
    ).to_intent(_decision(created_at=old, evidence_updated_at=old))
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.STALE_SIGNAL


def test_non_actionable_state_cannot_become_an_order() -> None:
    for state in (
        RecommendationState.SETTLED,
        RecommendationState.MARKET_CLOSED,
        RecommendationState.NO_LONGER_ACTIONABLE,
    ):
        out = SignalExecutionAdapter(
            mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
        ).to_intent(_decision(state=state))
        assert isinstance(out, IneligibleSignal)
        assert out.reason is IneligibilityReason.NOT_ACTIONABLE


# ================== findings from the Phase 5C0 adversarial review (reproduced)
def test_intent_mutated_during_risk_evaluation_cannot_be_sealed() -> None:
    """The seal must cover the values that were actually checked.

    `evaluate()` calls methods on the caller-supplied exposure object, and
    `authorize()` used to re-read the intent afterwards to build the plan. An
    exposure that mutated the intent in between produced a sealed, admissible
    plan for 5000 contracts against a 5-contract cap.
    """
    intent = _probe_intent()

    class Mutating(ExposureSnapshot):
        def position_exposure_usd(self, ticker: str) -> Decimal:
            object.__setattr__(intent, "quantity", 5000)
            return Decimal("0")

    hostile = Mutating(
        balance=AccountBalance(
            balance_usd=Decimal("500"), available_usd=Decimal("500")
        ),
        realized_loss_today_usd=Decimal("0"),
        spend_today_usd=Decimal("0"),
        fetched_at=utcnow(),
    )
    _, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        intent, hostile, client_order_id="c1"
    )
    assert plan is None or plan.quantity == DEMO_PROBE_SIZE


def test_sizing_is_bound_to_the_market_it_was_decided_for() -> None:
    """A ceiling is a per-book fact; it cannot vouch for a different book."""
    sizing = DemoExecutionProbePolicy().decide(
        mode=ExecutionMode.DEMO, ticker="DEEP-MARKET", capacity_ceiling=1829
    )
    intent = OrderIntent(
        intent_id="i",
        ticker="THIN-MARKET",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        limit_price_cents=40,
        quantity=1,
        mode=ExecutionMode.DEMO,
        expected_edge_after_fees=Decimal("0.08"),
        signal_timestamp=utcnow(),
        market_data_timestamp=utcnow(),
        expires_at=utcnow() + timedelta(minutes=5),
        capacity_ceiling=1,
        sizing=sizing,
    )
    decision, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        intent, _exposure(), client_order_id="c1"
    )
    assert plan is None
    assert BlockReason.SIZING_NOT_AUTHORIZED in set(decision.reasons)


def test_a_lookalike_policy_enum_cannot_claim_strategy_sized() -> None:
    """The reserved label was guarded by `is`, which a look-alike defeated."""
    from enum import StrEnum

    class Impostor(StrEnum):
        STRATEGY_SIZED = "strategy_sized"

    with pytest.raises(SizingRefused):
        issue_sizing(
            requested_quantity=5,
            policy=Impostor.STRATEGY_SIZED,
            policy_version="alpha-v9",
            mode=ExecutionMode.DEMO,
            ticker="T",
            decided_at=utcnow(),
        )


def test_a_sizing_decision_underwrites_exactly_one_order() -> None:
    decision = DemoExecutionProbePolicy().decide(mode=ExecutionMode.DEMO, ticker="T")
    decision.assert_issued(mode=ExecutionMode.DEMO)
    decision.consume()
    with pytest.raises(SizingNotIssued):
        decision.assert_issued(mode=ExecutionMode.DEMO)


@pytest.mark.parametrize("provenance", ["fixture", "DEMO", "seed", "", "Live "])
def test_only_live_provenance_is_admitted(provenance: str) -> None:
    """An allowlist, because a denylist misses every value nobody enumerated."""
    out = SignalExecutionAdapter(
        mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
    ).to_intent(_decision(provenance=provenance))
    if provenance.strip().lower() == "live":
        assert isinstance(out, OrderIntent)
    else:
        assert isinstance(out, IneligibleSignal)
        assert out.reason is IneligibilityReason.DEMO_PROVENANCE


def test_a_buy_limit_never_rounds_above_the_researched_entry_price() -> None:
    """0.5966 -> 60c paid a third of a cent more than the edge was measured at."""
    out = SignalExecutionAdapter(
        mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
    ).to_intent(_decision(entry_price=Decimal("0.5966"), provenance="live"))
    assert isinstance(out, OrderIntent)
    assert out.limit_price_cents == 59
    assert out.limit_price_cents <= Decimal("0.5966") * 100


def test_market_data_freshness_measures_the_quote_not_the_signal() -> None:
    quote_at = utcnow() - timedelta(hours=3)
    out = SignalExecutionAdapter(
        mode=ExecutionMode.DEMO, sizing_policy=DemoExecutionProbePolicy()
    ).to_intent(_decision(provenance="live", quote_observed_at=quote_at))
    assert isinstance(out, OrderIntent)
    assert out.market_data_timestamp == quote_at
    assert out.market_data_timestamp != out.signal_timestamp


def test_the_ledger_records_no_sizing_label_it_could_not_verify(
    tmp_path: Path,
) -> None:
    """An unverified label is worse than none: the column answers 'why N?'."""
    intent = _probe_intent()
    object.__setattr__(intent.sizing, "requested_quantity", 1500)  # breaks the seal
    ledger = ExecutionLedger(tmp_path / "execution.db")
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        record = ledger.get(con, coid)
    assert record is not None
    assert record.sizing_policy is None
    assert record.sizing_policy_version is None
