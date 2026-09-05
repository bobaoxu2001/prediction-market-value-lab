"""Phase 5C: the Kalshi demo order lifecycle, driven through the real stack.

Only the socket is faked (see `fake_kalshi_demo`). The transport, client,
harness, risk gate, authorization contract, sizing contract, ledger and
reconciler are all production code, so what these tests exercise is the path a
real demo order would take.

What they cannot do is prove the venue behaves as modelled. That needs
credentials and a real sandbox; until then every claim here is about *our* half
of the conversation.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from pmvl_shared.timeutil import utcnow
from pmvl_markets.execution import (
    AccountBalance,
    ExecutionLedger,
    ExecutionMode,
    ExposureSnapshot,
    KalshiAccountClient,
    KalshiExecutionClient,
    KillSwitch,
    OrderAction,
    OrderIntent,
    OrderStatus,
    Outcome,
    RiskGate,
    derive_client_order_id,
)
from pmvl_markets.execution.authorization import (
    AuthorizationAlreadyUsed,
    AuthorizationExpired,
)
from pmvl_markets.execution.credentials import KalshiCredentials, KalshiEnvironment
from pmvl_markets.execution.harness import ExecutionHarness
from pmvl_markets.execution.modes import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    LiveTradingNotAuthorized,
    ModeProfile,
)
from pmvl_markets.execution.reconcile import recover_after_restart
from pmvl_markets.execution.sizing import (
    DEMO_PROBE_SIZE,
    DemoExecutionProbePolicy,
    SizingPolicy,
)
from pmvl_markets.execution.transport import SignedTransport

from tests.fake_kalshi_demo import FakeKalshiDemo

TICKER = "KXDEMO-26SEP05-T70"
DEMO_PROFILE = ModeProfile.of(ExecutionMode.DEMO)
CREDS = KalshiCredentials(
    key_id="demo-key", environment=KalshiEnvironment.DEMO, private_key_pem="x"
)


@pytest.fixture()
def venue() -> FakeKalshiDemo:
    return FakeKalshiDemo()


@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    return ExecutionLedger(tmp_path / "execution.db")


@pytest.fixture(autouse=True)
def _stub_signing():
    """Sign with a constant. Key material is never needed to test mechanics."""
    with patch(
        "pmvl_markets.execution.transport.signing_headers",
        return_value={"KALSHI-ACCESS-KEY": "demo-key"},
    ):
        yield


def _transport(venue: FakeKalshiDemo) -> SignedTransport:
    return SignedTransport(
        DEMO_PROFILE,
        CREDS,
        client=httpx.AsyncClient(
            base_url=DEMO_BASE_URL, transport=httpx.MockTransport(venue.handler)
        ),
    )


def _harness(venue: FakeKalshiDemo, ledger: ExecutionLedger, **kw) -> ExecutionHarness:
    return ExecutionHarness(
        ExecutionMode.DEMO,
        ledger=ledger,
        risk_gate=kw.pop("risk_gate", RiskGate(kill_switch=KillSwitch())),
        account_client=KalshiAccountClient(
            ExecutionMode.DEMO, transport=_transport(venue)
        ),
        execution_client=KalshiExecutionClient(
            ExecutionMode.DEMO, transport=_transport(venue)
        ),
        **kw,
    )


def _probe_intent(**kw: object) -> OrderIntent:
    """One contract, demo-probe sized, exactly as Phase 5C mandates."""
    now = utcnow()
    sizing = DemoExecutionProbePolicy().decide(
        mode=ExecutionMode.DEMO, ticker=str(kw.get("ticker", TICKER)), capacity_ceiling=50
    )
    base: dict[str, object] = {
        "intent_id": "probe-1",
        "ticker": TICKER,
        "outcome": Outcome.YES,
        "action": OrderAction.BUY,
        "limit_price_cents": 40,
        "quantity": DEMO_PROBE_SIZE,
        "mode": ExecutionMode.DEMO,
        "strategy": "demo-execution-probe",
        "strategy_version": "1",
        "decision_epoch": "probe-epoch-1",
        "source_signal_id": "EXECUTION_MECHANICS_VALIDATION",
        "expected_edge_after_fees": Decimal("0.08"),
        "signal_timestamp": now,
        "market_data_timestamp": now,
        "expires_at": now + timedelta(minutes=5),
        "capacity_ceiling": 50,
        "sizing": sizing,
    }
    base.update(kw)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _exposure() -> ExposureSnapshot:
    return ExposureSnapshot(
        balance=AccountBalance(
            balance_usd=Decimal("1000"), available_usd=Decimal("1000")
        ),
        realized_loss_today_usd=Decimal("0"),
        spend_today_usd=Decimal("0"),
        fetched_at=utcnow(),
    )


# ========================================================= §5/§6 probe policy
def test_the_probe_is_one_contract_and_says_why() -> None:
    intent = _probe_intent()
    assert intent.quantity == 1 == DEMO_PROBE_SIZE
    assert intent.sizing.policy is SizingPolicy.DEMO_EXECUTION_PROBE
    assert "EXECUTION_MECHANICS_VALIDATION" in intent.sizing.detail
    assert intent.capacity_ceiling != intent.quantity


def test_a_probe_still_passes_through_the_real_risk_gate() -> None:
    decision, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        _probe_intent(), _exposure(), client_order_id="c1"
    )
    assert decision.passed
    assert plan is not None and plan.quantity == 1


def test_a_probe_with_a_stale_signal_is_blocked() -> None:
    old = utcnow() - timedelta(days=1)
    decision, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        _probe_intent(signal_timestamp=old), _exposure(), client_order_id="c1"
    )
    assert plan is None and not decision.passed


# ============================================ §8 environment isolation, again
def test_a_demo_transport_cannot_be_built_around_a_production_client() -> None:
    with pytest.raises(LiveTradingNotAuthorized):
        SignedTransport(
            DEMO_PROFILE,
            CREDS,
            client=httpx.AsyncClient(base_url=PRODUCTION_BASE_URL),
        )


def test_every_demo_request_resolves_to_the_demo_host(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    harness = _harness(venue, ledger)
    asyncio.run(harness.submit(_probe_intent(), exposure=_exposure()))
    assert venue.requests
    assert all(p.startswith("/trade-api/v2") for _, p in venue.requests)
    # The fake only ever answers the demo client; a production-bound request
    # could not have reached it.
    assert venue.create_count == 1


# ================================================= §12/§14 create and ack
def test_a_probe_creates_exactly_one_order_and_records_venue_evidence(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _probe_intent()
    coid = derive_client_order_id(intent)
    result = asyncio.run(_harness(venue, ledger).submit(intent, exposure=_exposure()))

    assert venue.create_count == 1
    body = venue.created_bodies[0]
    assert body["client_order_id"] == coid
    assert body["ticker"] == TICKER
    assert body["count"] == 1
    assert body["side"] == "yes"
    assert body["action"] == "buy"
    assert body["yes_price"] == 40
    assert "no_price" not in body

    assert result.client_order_id == coid
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.exchange_order_id
    assert record.sizing_policy == SizingPolicy.DEMO_EXECUTION_PROBE.value


def test_a_create_response_claiming_executed_is_not_recorded_as_filled(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """Absence of fill evidence means it is not a fill, whatever the venue says."""
    intent = _probe_intent()
    coid = derive_client_order_id(intent)

    def executed(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"order": {"order_id": "x1", "status": "executed",
                                "ticker": TICKER, "side": "yes", "count": 1}},
            )
        return venue.handler(request)

    liar = FakeKalshiDemo()
    liar.handler = executed  # type: ignore[method-assign]
    asyncio.run(_harness(liar, ledger).submit(intent, exposure=_exposure()))
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.status is not OrderStatus.FILLED
    assert record.filled_quantity == 0


# ================================================== §13/§31 idempotency, live
def test_resubmitting_the_same_probe_creates_no_second_venue_order(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    harness = _harness(venue, ledger)
    asyncio.run(harness.submit(_probe_intent(), exposure=_exposure()))
    first = venue.create_count

    # Same logical decision, processed again.
    asyncio.run(_harness(venue, ledger).submit(_probe_intent(), exposure=_exposure()))

    assert len(venue.orders) == 1, "a second order rested at the venue"
    assert venue.create_count <= first + 1
    coids = {o["client_order_id"] for o in venue.orders.values()}
    assert len(coids) == 1


def test_an_authorization_cannot_be_spent_twice(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _probe_intent()
    coid = derive_client_order_id(intent)
    _, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        intent, _exposure(), client_order_id=coid
    )
    assert plan is not None
    client = KalshiExecutionClient(ExecutionMode.DEMO, transport=_transport(venue))
    asyncio.run(client.create_order(plan))
    assert venue.create_count == 1
    with pytest.raises(AuthorizationAlreadyUsed):
        asyncio.run(client.create_order(plan))
    assert venue.create_count == 1, "a replayed plan reached the venue"


def test_an_expired_authorization_never_reaches_the_venue(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _probe_intent()
    _, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        intent, _exposure(), client_order_id="c1", ttl=timedelta(seconds=-1)
    )
    assert plan is not None
    client = KalshiExecutionClient(ExecutionMode.DEMO, transport=_transport(venue))
    with pytest.raises(AuthorizationExpired):
        asyncio.run(client.create_order(plan))
    assert venue.create_count == 0


# ============================================================ §15/§16 fills
def _submit(venue: FakeKalshiDemo, ledger: ExecutionLedger) -> str:
    intent = _probe_intent()
    coid = derive_client_order_id(intent)
    asyncio.run(_harness(venue, ledger).submit(intent, exposure=_exposure()))
    return coid


def _reconcile(venue: FakeKalshiDemo, ledger: ExecutionLedger):
    account = KalshiAccountClient(ExecutionMode.DEMO, transport=_transport(venue))

    async def run():
        try:
            return (
                await account.get_orders(),
                await account.get_fills(),
                await account.get_positions(),
            )
        finally:
            await account.aclose()

    orders, fills, positions = asyncio.run(run())
    return recover_after_restart(
        ledger, orders, fills, positions, mode=ExecutionMode.DEMO
    )


def test_zero_fills_leaves_the_order_open(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    coid = _submit(venue, ledger)
    _reconcile(venue, ledger)
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None and record.filled_quantity == 0


def test_a_partial_fill_is_neither_zero_nor_complete(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _probe_intent(quantity=4, sizing=None, capacity_ceiling=None)
    coid = derive_client_order_id(intent)
    asyncio.run(_harness(venue, ledger).submit(intent, exposure=_exposure()))
    order_id = next(iter(venue.orders))
    venue.fill(order_id, 3, 40)

    _reconcile(venue, ledger)
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.filled_quantity == 3
    assert record.remaining_quantity == 1


def test_multiple_fills_accumulate_and_average(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _probe_intent(quantity=4, sizing=None, capacity_ceiling=None)
    coid = derive_client_order_id(intent)
    asyncio.run(_harness(venue, ledger).submit(intent, exposure=_exposure()))
    order_id = next(iter(venue.orders))
    venue.fill(order_id, 2, 40)
    venue.fill(order_id, 2, 50)

    _reconcile(venue, ledger)
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.filled_quantity == 4
    assert record.average_fill_price_cents == Decimal("45")


def test_fill_ingestion_is_idempotent_across_reconciles(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    coid = _submit(venue, ledger)
    order_id = next(iter(venue.orders))
    venue.fill(order_id, 1, 40)
    for _ in range(3):
        _reconcile(venue, ledger)
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None and record.filled_quantity == 1


def test_venue_fills_carry_no_invented_fee(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """Kalshi's fills endpoint returns no per-fill fee; unknown must stay unknown."""
    coid = _submit(venue, ledger)
    venue.fill(next(iter(venue.orders)), 1, 40)
    _reconcile(venue, ledger)
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.fees_usd is None


# ======================================================== §17/§18 cancellation
def test_a_confirmed_cancel_is_recorded_as_cancelled(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    coid = _submit(venue, ledger)
    result = asyncio.run(_harness(venue, ledger).cancel(coid))
    assert result.status is OrderStatus.CANCELLED
    assert venue.orders[next(iter(venue.orders))]["status"] == "canceled"


def test_a_cancel_that_races_a_fill_is_not_recorded_as_cancelled(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """The venue answers 'nothing to cancel'. That is not evidence of a cancel."""
    coid = _submit(venue, ledger)
    order_id = next(iter(venue.orders))
    venue.fill(order_id, 1, 40)  # fills between the last poll and the cancel

    result = asyncio.run(_harness(venue, ledger).cancel(coid))
    assert result.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.status is not OrderStatus.CANCELLED


def test_cancelling_a_missing_order_is_ambiguous_not_cancelled(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    coid = _submit(venue, ledger)
    venue.vanish(next(iter(venue.orders)))
    result = asyncio.run(_harness(venue, ledger).cancel(coid))
    assert result.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED


# ================================================ §19 position reconciliation
def test_local_position_matches_the_venue_after_a_fill(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    _submit(venue, ledger)
    venue.fill(next(iter(venue.orders)), 1, 40)
    report = _reconcile(venue, ledger)
    kinds = {d.kind.value for d in report.discrepancies}
    assert "position_mismatch" not in kinds


def test_an_inverted_venue_position_is_caught(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """Long 1 YES locally against long 1 NO at the venue is not a match."""
    _submit(venue, ledger)
    venue.fill(next(iter(venue.orders)), 1, 40)
    venue.positions.clear()
    venue.positions[(TICKER, "no")] = 1

    report = _reconcile(venue, ledger)
    kinds = {d.kind.value for d in report.discrepancies}
    assert "position_mismatch" in kinds


# ==================================================== §20 restart recovery
def test_restart_reconciles_without_duplicating_an_order(
    venue: FakeKalshiDemo, tmp_path: Path
) -> None:
    path = tmp_path / "execution.db"
    intent = _probe_intent()
    coid = derive_client_order_id(intent)

    asyncio.run(
        _harness(venue, ExecutionLedger(path)).submit(intent, exposure=_exposure())
    )
    venue.fill(next(iter(venue.orders)), 1, 40)
    orders_before = venue.create_count

    # A fresh ledger object stands in for a new process against the same file.
    reborn = ExecutionLedger(path)
    _reconcile(venue, reborn)
    asyncio.run(_harness(venue, reborn).submit(_probe_intent(), exposure=_exposure()))

    assert venue.create_count == orders_before, "restart re-sent the order"
    with reborn.connect() as con:
        record = reborn.get(con, coid)
        assert len(reborn.all_orders(con, mode=ExecutionMode.DEMO)) == 1
    assert record is not None
    assert record.filled_quantity == 1


def test_an_order_that_vanished_without_fills_blocks_rather_than_resolving(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    coid = _submit(venue, ledger)
    venue.vanish(next(iter(venue.orders)))
    report = _reconcile(venue, ledger)
    assert not report.is_clean
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    assert record.filled_quantity == 0


# ============================================ §21 ambiguous write recovery
def test_a_timeout_after_post_does_not_mint_a_new_identity(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """The order may have rested. A second POST would be a second order."""
    intent = _probe_intent()
    coid = derive_client_order_id(intent)
    venue.next_create_raises = httpx.ReadTimeout("timed out")

    asyncio.run(_harness(venue, ledger).submit(intent, exposure=_exposure()))
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    # The identity is unchanged, so a reconcile can find the order remotely.
    assert derive_client_order_id(_probe_intent()) == coid


def test_unresolved_state_blocks_the_next_probe(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.next_create_raises = httpx.ReadTimeout("timed out")
    asyncio.run(_harness(venue, ledger).submit(_probe_intent(), exposure=_exposure()))
    before = venue.create_count

    other = _probe_intent(intent_id="probe-2", decision_epoch="probe-epoch-2")
    result = asyncio.run(_harness(venue, ledger).submit(other, exposure=_exposure()))
    assert result.status is OrderStatus.BLOCKED
    assert venue.create_count == before, "traded while account state was unknown"


def test_a_duplicate_client_order_id_at_the_venue_is_not_retried(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _probe_intent()
    coid = derive_client_order_id(intent)
    venue.rest_order({"client_order_id": coid, "ticker": TICKER, "side": "yes",
                      "action": "buy", "count": 1, "yes_price": 40})
    asyncio.run(_harness(venue, ledger).submit(intent, exposure=_exposure()))
    assert len(venue.orders) == 1


# ============================================ §33/§34 kill switch and UNKNOWN
def test_the_kill_switch_stops_a_probe_before_the_socket(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    switch = KillSwitch()
    switch.engage("operator stop")
    harness = _harness(venue, ledger, risk_gate=RiskGate(kill_switch=switch))
    result = asyncio.run(harness.submit(_probe_intent(), exposure=_exposure()))
    assert result.status is OrderStatus.BLOCKED
    assert venue.create_count == 0


def test_reads_still_work_while_killed(venue: FakeKalshiDemo) -> None:
    """A kill switch that also blinded the operator would be worse than useless."""
    account = KalshiAccountClient(ExecutionMode.DEMO, transport=_transport(venue))
    balance = asyncio.run(account.get_balance())
    asyncio.run(account.aclose())
    assert balance is not None


# ================================================ §38/§39 auth and HTTP faults
def test_an_auth_failure_does_not_retry_the_mutation(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.fail_auth = True
    asyncio.run(_harness(venue, ledger).submit(_probe_intent(), exposure=_exposure()))
    assert venue.create_count == 1, "retried a write after an auth failure"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_venue_error_never_produces_a_second_order(
    venue: FakeKalshiDemo, ledger: ExecutionLedger, status: int
) -> None:
    venue.next_create_status = status
    asyncio.run(_harness(venue, ledger).submit(_probe_intent(), exposure=_exposure()))
    assert venue.create_count == 1
    assert len(venue.orders) == 0


def test_a_local_rejection_is_distinguishable_from_a_venue_rejection(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    switch = KillSwitch()
    switch.engage("stop")
    local = asyncio.run(
        _harness(venue, ledger, risk_gate=RiskGate(kill_switch=switch)).submit(
            _probe_intent(), exposure=_exposure()
        )
    )
    assert local.status is OrderStatus.BLOCKED
    assert venue.create_count == 0  # never left the process

    venue.next_create_status = 400
    remote = asyncio.run(
        _harness(venue, ExecutionLedger(ledger.path)).submit(
            _probe_intent(intent_id="p3", decision_epoch="e3"), exposure=_exposure()
        )
    )
    assert remote.status is not OrderStatus.BLOCKED
    assert venue.create_count == 1  # reached the venue and was refused there


# ================================================== §35 cross-mode isolation
def test_demo_activity_does_not_touch_another_modes_ledger_state(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    _submit(venue, ledger)
    venue.fill(next(iter(venue.orders)), 1, 40)
    _reconcile(venue, ledger)
    with ledger.connect() as con:
        assert len(ledger.all_orders(con, mode=ExecutionMode.DEMO)) == 1
        assert ledger.all_orders(con, mode=ExecutionMode.PRODUCTION_READ_ONLY) == []
        assert ledger.all_orders(con, mode=ExecutionMode.SHADOW) == []
        assert (
            ledger.open_blocking_discrepancies(
                con, mode=ExecutionMode.PRODUCTION_READ_ONLY
            )
            == []
        )


# ============================================================ §40 durability
def test_the_ledger_holds_the_whole_lifecycle(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    coid = _submit(venue, ledger)
    venue.fill(next(iter(venue.orders)), 1, 40)
    _reconcile(venue, ledger)

    with ledger.connect() as con:
        record = ledger.get(con, coid)
        transitions = ledger.transitions_for(con, coid)
        fills = ledger.fills_for(con, coid)
    assert record is not None
    assert record.mode == ExecutionMode.DEMO.value
    assert record.sizing_policy == SizingPolicy.DEMO_EXECUTION_PROBE.value
    assert record.client_order_id == coid
    assert len(transitions) >= 3
    assert len(fills) == 1


def test_no_secret_material_reaches_the_execution_database(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    _submit(venue, ledger)
    with ledger.connect() as con:
        tables = [
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        blob = ""
        for table in tables:
            for row in con.execute(f"SELECT * FROM {table}"):
                blob += " ".join(str(v) for v in row)
    lowered = blob.lower()
    for banned in ("private", "begin rsa", "-----begin", "signature", "demo-key"):
        assert banned not in lowered


# ============================================== §10 time-bounded demo arming
@pytest.fixture()
def arm_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from pmvl_shared.config import reset_settings_cache

    monkeypatch.setenv("EXECUTION_DEMO_ARM_PATH", str(tmp_path / "DEMO_ARMED"))
    monkeypatch.setenv("EXECUTION_KILL_SWITCH_PATH", str(tmp_path / "DISABLED"))
    reset_settings_cache()
    yield tmp_path / "DEMO_ARMED", tmp_path / "DISABLED"
    reset_settings_cache()


def _arm(path: Path, minutes: int) -> None:
    path.write_text((utcnow() + timedelta(minutes=minutes)).isoformat() + " test\n")


def test_without_an_arm_every_mode_is_blocked(arm_paths) -> None:
    for mode in ExecutionMode:
        assert KillSwitch.from_settings(mode=mode).engaged is True


def test_an_arm_unblocks_demo_and_only_demo(arm_paths) -> None:
    arm, _ = arm_paths
    _arm(arm, 30)
    assert KillSwitch.from_settings(mode=ExecutionMode.DEMO).engaged is False
    assert KillSwitch.from_settings(mode=ExecutionMode.SHADOW).engaged is True
    assert (
        KillSwitch.from_settings(mode=ExecutionMode.PRODUCTION_READ_ONLY).engaged
        is True
    )
    assert KillSwitch.from_settings().engaged is True


def test_an_expired_arm_blocks_again(arm_paths) -> None:
    arm, _ = arm_paths
    _arm(arm, -1)
    assert KillSwitch.from_settings(mode=ExecutionMode.DEMO).engaged is True


def test_the_kill_file_overrides_a_valid_arm(arm_paths) -> None:
    """`pmvl exec kill` has to remain absolute."""
    arm, kill = arm_paths
    _arm(arm, 30)
    kill.write_text("stop\n")
    assert KillSwitch.from_settings(mode=ExecutionMode.DEMO).engaged is True


def test_an_unreadable_arm_fails_closed(arm_paths) -> None:
    arm, _ = arm_paths
    arm.write_text("not-a-timestamp\n")
    assert KillSwitch.from_settings(mode=ExecutionMode.DEMO).engaged is True


def test_an_armed_demo_still_cannot_reach_production() -> None:
    """Arming is about the kill switch, never about the environment gate."""
    from pmvl_markets.execution.modes import assert_transport_write_allowed

    with pytest.raises(LiveTradingNotAuthorized):
        assert_transport_write_allowed("POST", PRODUCTION_BASE_URL + "/portfolio/orders")
    assert not any("LIVE" in m.name for m in ExecutionMode)


# ============================================================ §25 order classes
def test_a_probe_is_never_labelled_as_a_research_order() -> None:
    intent = _probe_intent()
    assert intent.sizing.policy is SizingPolicy.DEMO_EXECUTION_PROBE
    assert intent.sizing.policy is not SizingPolicy.STRATEGY_SIZED
    blob = (intent.reason + intent.strategy + intent.sizing.detail).lower()
    for forbidden in ("recommended", "optimal", "edge opportunity", "profit"):
        assert forbidden not in blob


# ============ findings from the Phase 5C adversarial review (all reproduced)
def test_a_cancel_racing_a_partial_fill_is_not_recorded_as_cancelled(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """The guard must read the venue's answer, not our stale local row.

    All three attackers found this independently: the old guard consulted
    `record.filled_quantity`, which reconciliation had not yet populated - so it
    was 0 in exactly the case the race creates, and a partially filled order was
    written terminal-CANCELLED with the fill discarded.
    """
    intent = _probe_intent(quantity=3, sizing=None, capacity_ceiling=None)
    coid = derive_client_order_id(intent)
    asyncio.run(_harness(venue, ledger).submit(intent, exposure=_exposure()))
    order_id = next(iter(venue.orders))
    venue.fill(order_id, 1, 40)  # 1 of 3, between the last poll and the cancel

    result = asyncio.run(_harness(venue, ledger).cancel(coid))
    assert result.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.status is not OrderStatus.CANCELLED


def test_an_unfilled_cancel_is_still_terminal(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """The fix must not turn every honest cancel into an ambiguity."""
    coid = _submit(venue, ledger)
    result = asyncio.run(_harness(venue, ledger).cancel(coid))
    assert result.status is OrderStatus.CANCELLED


def test_cancelling_an_unknown_order_explains_instead_of_crashing(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    coid = _submit(venue, ledger)
    venue.vanish(next(iter(venue.orders)))
    asyncio.run(_harness(venue, ledger).cancel(coid))  # -> UNKNOWN
    result = asyncio.run(_harness(venue, ledger).cancel(coid))  # must not raise
    assert result.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    assert "reconcile-order" in (result.error or "")


def test_redirects_are_pinned_per_send_not_only_at_construction(
    venue: FakeKalshiDemo,
) -> None:
    """httpx reads follow_redirects at send time; a mutable attribute defeated it."""
    client = httpx.AsyncClient(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(venue.handler)
    )
    transport = SignedTransport(DEMO_PROFILE, CREDS, client=client)
    client.follow_redirects = True  # flipped after the constructor check

    import inspect

    source = inspect.getsource(type(transport)._request)
    assert "follow_redirects=False" in source


def test_a_venue_status_alone_cannot_write_filled(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """FILLED needs fill rows. A status string is a claim about an outcome."""
    from pmvl_markets.execution.ledger import InvalidTransition

    coid = _submit(venue, ledger)
    with ledger.connect() as con:
        ledger.transition(
            con, coid, OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED, note="ambiguous"
        )
        with pytest.raises(InvalidTransition):
            ledger.force_status_from_evidence(
                con,
                client_order_id=coid,
                status=OrderStatus.FILLED,
                note="venue says filled",
            )


def test_resolution_stays_unknown_when_the_venue_reports_a_fill_it_cannot_show(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    from pmvl_markets.execution.models import Order as RemoteOrder
    from pmvl_markets.execution.reconcile import resolve_unknown_order

    coid = _submit(venue, ledger)
    with ledger.connect() as con:
        ledger.transition(
            con, coid, OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED, note="ambiguous"
        )
    remote = RemoteOrder(
        exchange_order_id="demo-order-1",
        ticker=TICKER,
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        status=OrderStatus.FILLED,
        original_quantity=1,
        filled_quantity=1,
        remaining_quantity=0,
        client_order_id=coid,
    )
    result = resolve_unknown_order(
        ledger, coid, mode=ExecutionMode.DEMO, remote_order=remote, remote_fills=[]
    )
    assert result.resolved is False
    assert result.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED


def test_a_probe_label_cannot_be_minted_for_more_than_one_contract() -> None:
    from pmvl_markets.execution.sizing import (
        SIZING_POLICY_VERSION,
        SizingRefused,
        issue_sizing,
    )

    with pytest.raises(SizingRefused):
        issue_sizing(
            requested_quantity=5,
            policy=SizingPolicy.DEMO_EXECUTION_PROBE,
            policy_version=SIZING_POLICY_VERSION,
            mode=ExecutionMode.DEMO,
            ticker=TICKER,
            decided_at=utcnow(),
            capacity_ceiling=50,
        )


def test_account_reads_follow_the_cursor(venue: FakeKalshiDemo) -> None:
    """A ghost order past the first page must not reconcile clean."""
    pages = {"n": 0}

    def paged(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/portfolio/orders"):
            pages["n"] += 1
            if pages["n"] == 1:
                return httpx.Response(
                    200, json={"orders": [], "cursor": "page-2"}
                )
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {"order_id": "ghost", "ticker": TICKER, "side": "yes",
                         "action": "buy", "status": "resting", "initial_count": 5,
                         "remaining_count": 5, "taker_fill_count": 0,
                         "yes_price": 40}
                    ]
                },
            )
        return venue.handler(request)

    v = FakeKalshiDemo()
    v.handler = paged  # type: ignore[method-assign]
    account = KalshiAccountClient(ExecutionMode.DEMO, transport=_transport(v))
    orders = asyncio.run(account.get_orders())
    asyncio.run(account.aclose())
    assert [o.exchange_order_id for o in orders] == ["ghost"]
    assert pages["n"] == 2


def test_an_endless_cursor_raises_rather_than_returning_a_partial_read(
    venue: FakeKalshiDemo,
) -> None:
    from pmvl_markets.execution.transport import ExecutionApiError

    def endless(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/portfolio/orders"):
            return httpx.Response(200, json={"orders": [], "cursor": "always-more"})
        return venue.handler(request)

    v = FakeKalshiDemo()
    v.handler = endless  # type: ignore[method-assign]
    account = KalshiAccountClient(ExecutionMode.DEMO, transport=_transport(v))
    with pytest.raises(ExecutionApiError):
        asyncio.run(account.get_orders())
    asyncio.run(account.aclose())


def test_the_demo_arm_reaches_the_paths_an_order_actually_takes(arm_paths) -> None:
    """The arm was inert: no shipped caller passed `mode=` to from_settings."""
    from pmvl_markets.execution.gateway import ShadowExecutionGateway

    arm, _ = arm_paths
    _arm(arm, 30)
    assert RiskGate(mode=ExecutionMode.DEMO).kill_switch.engaged is False
    assert RiskGate(mode=ExecutionMode.SHADOW).kill_switch.engaged is True
    assert RiskGate().kill_switch.engaged is True


def test_an_over_long_arm_is_refused_by_the_reader(arm_paths) -> None:
    """The 240-minute bound lived only in the CLI; a copied file ignored it."""
    arm, _ = arm_paths
    arm.write_text((utcnow() + timedelta(days=365)).isoformat() + " forever\n")
    assert KillSwitch.demo_arm_active()[0] is False
    assert KillSwitch.from_settings(mode=ExecutionMode.DEMO).engaged is True
