"""Phase 5A: reconciliation classification, orientation safety, and the harness.

The recurring assertion: an ambiguity is *flagged*, never traded away, and never
resolved in the optimistic direction.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pmvl_markets.execution.authorization import issue_authorization
from pmvl_markets.execution import (
    AccountBalance,
    DiscrepancyKind,
    ExecutionLedger,
    ExecutionMode,
    ExposureSnapshot,
    Fill,
    KalshiExecutionClient,
    KillSwitch,
    LiveTradingNotAuthorized,
    ModeProfile,
    Order,
    OrderAction,
    OrderIntent,
    OrderStatus,
    Outcome,
    Position,
    ResolutionStatus,
    RiskGate,
    derive_client_order_id,
    reconcile_orders,
    reconcile_positions,
    resolve_market,
)
from pmvl_markets.execution.client import (
    KALSHI_STATUS_MAP,
    normalize_fill,
    normalize_order,
    normalize_position,
)
from pmvl_markets.execution.credentials import KalshiCredentials
from pmvl_markets.execution.harness import ExecutionHarness
from pmvl_markets.execution.ledger import LedgerRecord
from pmvl_markets.execution.modes import KalshiEnvironment
from pmvl_markets.execution.resolve import orientation_for
from pmvl_markets.execution.transport import SignedTransport
from pmvl_shared.enums import Category, MarketStatus, Platform, Side
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow


def _record(**overrides: object) -> LedgerRecord:
    base: dict[str, object] = {
        "client_order_id": "pmvl-v1-abc",
        "intent_id": "i-1",
        "mode": "demo",
        "ticker": "T1",
        "outcome": "yes",
        "action": "buy",
        "requested_price_cents": 40,
        "requested_quantity": 10,
        "status": OrderStatus.OPEN,
        "exchange_order_id": "X1",
        "filled_quantity": 0,
        "average_fill_price_cents": None,
        "fees_usd": None,
        "created_at": "2026-09-04T00:00:00+00:00",
        "submitted_at": None,
        "last_reconciled_at": None,
        "error": "",
    }
    base.update(overrides)
    return LedgerRecord(**base)  # type: ignore[arg-type]


def _order(**overrides: object) -> Order:
    base: dict[str, object] = {
        "exchange_order_id": "X1",
        "ticker": "T1",
        "outcome": Outcome.YES,
        "action": OrderAction.BUY,
        "status": OrderStatus.OPEN,
        "limit_price_cents": 40,
        "original_quantity": 10,
        "filled_quantity": 0,
        "remaining_quantity": 10,
        "client_order_id": "pmvl-v1-abc",
    }
    base.update(overrides)
    return Order(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------- classification
def test_matching_state_is_reconciled() -> None:
    report = reconcile_orders([_record()], [_order()], [])
    assert {d.kind for d in report.discrepancies} == {DiscrepancyKind.RECONCILED}
    assert report.is_clean


def test_local_only_open_order_is_blocking() -> None:
    report = reconcile_orders([_record()], [], [])
    assert report.discrepancies[0].kind is DiscrepancyKind.LOCAL_ONLY
    assert report.discrepancies[0].blocking is True


def test_remote_only_open_order_is_blocking() -> None:
    stranger = _order(exchange_order_id="X9", client_order_id=None)
    report = reconcile_orders([], [stranger], [])
    assert report.discrepancies[0].kind is DiscrepancyKind.REMOTE_ONLY
    assert report.discrepancies[0].blocking is True


def test_fill_mismatch_is_blocking() -> None:
    report = reconcile_orders([_record()], [_order(filled_quantity=4)], [])
    assert report.discrepancies[0].kind is DiscrepancyKind.FILL_MISMATCH
    assert report.discrepancies[0].blocking is True


def test_status_mismatch_with_matching_fills_is_non_blocking() -> None:
    report = reconcile_orders(
        [_record()], [_order(status=OrderStatus.CANCEL_PENDING)], []
    )
    assert report.discrepancies[0].kind is DiscrepancyKind.STATUS_MISMATCH
    assert report.discrepancies[0].blocking is False


def test_unrecognised_remote_status_is_blocking() -> None:
    weird = _order(status=OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED, raw_status="wat")
    report = reconcile_orders([_record()], [weird], [])
    assert report.discrepancies[0].blocking is True


def test_terminal_local_order_absent_remotely_is_consistent() -> None:
    report = reconcile_orders([_record(status=OrderStatus.CANCELLED)], [], [])
    assert report.discrepancies[0].kind is DiscrepancyKind.RECONCILED


def test_absent_remotely_with_full_fill_evidence_reconciles() -> None:
    fills = [
        Fill(
            fill_id="f1",
            ticker="T1",
            outcome=Outcome.YES,
            action=OrderAction.BUY,
            quantity=10,
            price_cents=40,
            exchange_order_id="X1",
        )
    ]
    report = reconcile_orders([_record()], [], fills)
    assert report.discrepancies[0].kind is DiscrepancyKind.RECONCILED


def test_absent_remotely_with_partial_fill_is_flagged() -> None:
    """Cancelled after a partial fill is not the same as filled."""
    fills = [
        Fill(
            fill_id="f1",
            ticker="T1",
            outcome=Outcome.YES,
            action=OrderAction.BUY,
            quantity=3,
            price_cents=40,
            exchange_order_id="X1",
        )
    ]
    report = reconcile_orders([_record()], [], fills)
    assert report.discrepancies[0].kind is DiscrepancyKind.FILL_MISMATCH
    assert report.discrepancies[0].blocking is True


def test_position_mismatch_detected() -> None:
    out = reconcile_positions(
        {("T1", "yes"): 5}, [Position(ticker="T1", outcome=Outcome.YES, quantity=3)]
    )
    assert out and out[0].kind is DiscrepancyKind.POSITION_MISMATCH


def test_position_match_is_silent() -> None:
    assert (
        reconcile_positions(
            {("T1", "yes"): 3},
            [Position(ticker="T1", outcome=Outcome.YES, quantity=3)],
        )
        == []
    )


def test_reconciliation_never_places_a_trade() -> None:
    """Structural: the module must not import or call any write path."""
    import pmvl_markets.execution.reconcile as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("create_order", "cancel_order", "KalshiExecutionClient"):
        assert forbidden not in source


# ------------------------------------------------------------ normalization
def test_unknown_venue_status_becomes_unknown_not_a_guess() -> None:
    order = normalize_order({"order_id": "X", "status": "something_new"})
    assert order.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED


def test_known_statuses_map_without_inventing_terminal_states() -> None:
    assert KALSHI_STATUS_MAP["resting"] is OrderStatus.OPEN
    assert KALSHI_STATUS_MAP["executed"] is OrderStatus.FILLED
    assert KALSHI_STATUS_MAP["canceled"] is OrderStatus.CANCELLED


def test_missing_fee_is_none_not_zero() -> None:
    fill = normalize_fill(
        {"trade_id": "t", "ticker": "T", "side": "yes", "count": 1, "yes_price": 40}
    )
    assert fill.fee_usd is None


def test_negative_kalshi_position_becomes_a_no_position() -> None:
    pos = normalize_position({"ticker": "T", "position": -7})
    assert pos.outcome is Outcome.NO
    assert pos.quantity == 7


def test_no_order_reads_the_no_price_field() -> None:
    order = normalize_order(
        {"order_id": "X", "status": "resting", "side": "no", "no_price": 33,
         "yes_price": 67, "count": 2}
    )
    assert order.outcome is Outcome.NO
    assert order.limit_price_cents == 33


# ------------------------------------------------------- YES/NO orientation
def test_outcome_and_action_are_independent_enums() -> None:
    """A transposed pair must be a type error, not a silent inversion."""
    with pytest.raises(ValueError):
        Outcome("buy")
    with pytest.raises(ValueError):
        OrderAction("yes")


def test_outcome_opposite() -> None:
    assert Outcome.YES.opposite is Outcome.NO
    assert Outcome.NO.opposite is Outcome.YES


def test_side_bridge_does_not_invert() -> None:
    assert orientation_for(Side.YES) is Outcome.YES
    assert orientation_for(Side.NO) is Outcome.NO
    assert Outcome.YES.to_side() is Side.YES
    assert Outcome.NO.to_side() is Side.NO


@pytest.mark.parametrize(
    "outcome,action,price_key",
    [
        (Outcome.YES, OrderAction.BUY, "yes_price"),
        (Outcome.YES, OrderAction.SELL, "yes_price"),
        (Outcome.NO, OrderAction.BUY, "no_price"),
        (Outcome.NO, OrderAction.SELL, "no_price"),
    ],
)
async def test_all_four_orientations_build_the_right_body(
    outcome: Outcome, action: OrderAction, price_key: str
) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.append(_json.loads(request.content))
        return httpx.Response(200, json={"order": {"order_id": "X", "status": "resting"}})

    profile = ModeProfile.of(ExecutionMode.DEMO)
    transport = SignedTransport(
        profile,
        KalshiCredentials(
            key_id="k", environment=KalshiEnvironment.DEMO, private_key_pem="x"
        ),
        client=httpx.AsyncClient(
            base_url=profile.base_url, transport=httpx.MockTransport(handler)
        ),
    )
    import pmvl_markets.execution.credentials as creds_mod

    original = creds_mod.sign_payload
    creds_mod.sign_payload = lambda c, p: "sig"  # type: ignore[assignment]
    try:
        client = KalshiExecutionClient(ExecutionMode.DEMO, transport=transport)
        intent = OrderIntent(
            intent_id="i",
            ticker="T1",
            outcome=outcome,
            action=action,
            limit_price_cents=42,
            quantity=1,
            mode=ExecutionMode.DEMO,
        )
        # Sealed directly: this test is about how the client renders an
        # approved order onto the wire, not about whether risk approves it.
        plan = issue_authorization(
            mode=ExecutionMode.DEMO,
            ticker=intent.ticker,
            outcome=intent.outcome,
            action=intent.action,
            limit_price_cents=intent.limit_price_cents,
            quantity=intent.quantity,
            time_in_force=intent.time_in_force,
            client_order_id="pmvl-v2-x",
            intent_id=intent.intent_id,
            risk_checked_at=utcnow(),
            authorization_expires_at=utcnow() + timedelta(minutes=5),
        )
        await client.create_order(plan)
    finally:
        creds_mod.sign_payload = original  # type: ignore[assignment]
        await transport.aclose()

    body = captured[0]
    assert body["side"] == outcome.value
    assert body["action"] == action.value
    assert body[price_key] == 42
    other = "no_price" if price_key == "yes_price" else "yes_price"
    assert other not in body


# ------------------------------------------------------------ market identity
def _market(**overrides: object) -> NormalizedMarket:
    base: dict[str, object] = {
        "platform": Platform.KALSHI,
        "platform_market_id": "KXHIGHNY-26SEP04-B70",
        "platform_event_id": "E",
        "series_ticker": "KXHIGHNY",
        "title": "NYC high temperature",
        "normalized_title": "nyc high",
        "category": Category.WEATHER,
        "status": MarketStatus.OPEN,
        "accepting_orders": True,
        "close_time": utcnow() + timedelta(hours=6),
    }
    base.update(overrides)
    return NormalizedMarket(**base)  # type: ignore[arg-type]


def test_resolver_finds_the_exact_ticker() -> None:
    result = resolve_market([_market()], ticker="KXHIGHNY-26SEP04-B70")
    assert result.ok


def test_resolver_refuses_a_missing_ticker() -> None:
    result = resolve_market([_market()], ticker="NOPE")
    assert result.status is ResolutionStatus.NOT_FOUND


def test_resolver_refuses_ambiguity_rather_than_choosing() -> None:
    result = resolve_market(
        [_market(), _market()], ticker="KXHIGHNY-26SEP04-B70"
    )
    assert result.status is ResolutionStatus.AMBIGUOUS


def test_resolver_refuses_a_closed_market() -> None:
    result = resolve_market(
        [_market(status=MarketStatus.CLOSED)], ticker="KXHIGHNY-26SEP04-B70"
    )
    assert result.status is ResolutionStatus.CLOSED


def test_resolver_refuses_a_market_not_accepting_orders() -> None:
    result = resolve_market(
        [_market(accepting_orders=False)], ticker="KXHIGHNY-26SEP04-B70"
    )
    assert result.status is ResolutionStatus.NOT_ACCEPTING_ORDERS


def test_resolver_refuses_an_expiring_market() -> None:
    result = resolve_market(
        [_market(close_time=utcnow() + timedelta(seconds=5))],
        ticker="KXHIGHNY-26SEP04-B70",
    )
    assert result.status is ResolutionStatus.EXPIRED


def test_resolver_refuses_a_wrong_station() -> None:
    result = resolve_market(
        [_market()], ticker="KXHIGHNY-26SEP04-B70", expected_station="KMIA"
    )
    assert result.status is ResolutionStatus.WRONG_STATION


def test_resolver_never_substitutes_a_neighbour() -> None:
    near = _market(platform_market_id="KXHIGHNY-26SEP04-B72")
    result = resolve_market([near], ticker="KXHIGHNY-26SEP04-B70")
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.market is None


# ------------------------------------------------------------------- harness
@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    return ExecutionLedger(tmp_path / "execution.db")


def _flat_exposure() -> ExposureSnapshot:
    """A known-empty account. Not the same as unknown - see the test below."""
    return ExposureSnapshot(
        realized_loss_today_usd=Decimal("0"),
        spend_today_usd=Decimal("0"),
        balance=AccountBalance(
            balance_usd=Decimal("100"), available_usd=Decimal("100")
        )
    )


def _shadow_intent() -> OrderIntent:
    now = utcnow()
    return OrderIntent(
        intent_id="i-shadow",
        ticker="T1",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        limit_price_cents=40,
        quantity=2,
        mode=ExecutionMode.SHADOW,
        expected_edge_after_fees=Decimal("0.10"),
        signal_timestamp=now,
        market_data_timestamp=now,
        expires_at=now + timedelta(minutes=5),
    )


async def test_shadow_mode_persists_an_intent_and_sends_nothing(
    ledger: ExecutionLedger,
) -> None:
    harness = ExecutionHarness(
        ExecutionMode.SHADOW, ledger=ledger, risk_gate=RiskGate(kill_switch=KillSwitch())
    )
    intent = _shadow_intent()
    result = await harness.submit(intent, exposure=_flat_exposure())

    assert result.simulated is True
    assert result.status is OrderStatus.SHADOW_AUTHORIZED
    assert result.exchange_order_id is None

    with ledger.connect() as con:
        record = ledger.get(con, result.client_order_id)
    assert record is not None
    # `submitted_at` marks that the plan was acted on - it is what makes the
    # daily-spend cap measurable in shadow. The no-venue-contact invariant is
    # carried by exchange_order_id, which stays None.
    assert record.exchange_order_id is None


async def test_shadow_harness_rejects_an_execution_client(
    ledger: ExecutionLedger,
) -> None:
    with pytest.raises(LiveTradingNotAuthorized):
        ExecutionHarness(
            ExecutionMode.SHADOW,
            ledger=ledger,
            risk_gate=RiskGate(kill_switch=KillSwitch()),
            execution_client=object(),  # type: ignore[arg-type]
        )


async def test_harness_refuses_a_mode_mismatched_intent(
    ledger: ExecutionLedger,
) -> None:
    harness = ExecutionHarness(
        ExecutionMode.SHADOW, ledger=ledger, risk_gate=RiskGate(kill_switch=KillSwitch())
    )
    demo_intent = OrderIntent(
        intent_id="i",
        ticker="T1",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        limit_price_cents=40,
        quantity=1,
        mode=ExecutionMode.DEMO,
    )
    with pytest.raises(LiveTradingNotAuthorized):
        await harness.plan(demo_intent)


async def test_a_blocked_intent_is_recorded_as_blocked(
    ledger: ExecutionLedger,
) -> None:
    harness = ExecutionHarness(
        ExecutionMode.SHADOW, ledger=ledger, risk_gate=RiskGate(kill_switch=KillSwitch())
    )
    intent = OrderIntent(
        intent_id="i-big",
        ticker="T1",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        limit_price_cents=40,
        quantity=99,
        mode=ExecutionMode.SHADOW,
        expected_edge_after_fees=Decimal("0.10"),
        signal_timestamp=utcnow(),
        market_data_timestamp=utcnow(),
    )
    result = await harness.submit(intent, exposure=_flat_exposure())
    assert result.status is OrderStatus.BLOCKED

    with ledger.connect() as con:
        record = ledger.get(con, result.client_order_id)
    assert record is not None and record.status is OrderStatus.BLOCKED


async def test_shadow_with_unknown_exposure_blocks_rather_than_assuming_flat(
    ledger: ExecutionLedger,
) -> None:
    """Fail closed: unmeasured exposure is not zero exposure."""
    harness = ExecutionHarness(
        ExecutionMode.SHADOW, ledger=ledger, risk_gate=RiskGate(kill_switch=KillSwitch())
    )
    result = await harness.submit(_shadow_intent())
    assert result.status is OrderStatus.BLOCKED
    assert "exposure" in result.error
