"""Phase 5B: R1-R6 closure, the authorization contract, and the shadow pipeline.

The attacks in here are the ones a review would try against the *new* surface:
forge a plan, edit one after approval, present an expired one, cross an
environment, collide two different orders onto one idempotency key, clear an
UNKNOWN order without evidence.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pmvl_shared.enums import RecommendationState, Side
from pmvl_shared.timeutil import utcnow
from pmvl_markets.execution import (
    AccountBalance,
    AuthorizedExecutionPlan,
    ExecutionLedger,
    ExecutionMode,
    ExposureSnapshot,
    Fill,
    KalshiAccountClient,
    KalshiExecutionClient,
    KillSwitch,
    Order,
    OrderAction,
    OrderIntent,
    OrderStatus,
    Outcome,
    Position,
    RiskGate,
    ShadowAccountState,
    ShadowExecutionGateway,
    ShadowSignalPipeline,
    SignalExecutionAdapter,
    TimeInForce,
    derive_client_order_id,
)
from pmvl_markets.execution.authorization import (
    MAX_AUTHORIZATION_TTL,
    RISK_POLICY_VERSION,
    AuthorizationAlreadyUsed,
    AuthorizationExpired,
    AuthorizationInvalid,
    AuthorizationNotIssued,
    AuthorizationTampered,
)
from pmvl_markets.execution.credentials import (
    CredentialsUnavailable,
    KalshiCredentials,
    KalshiEnvironment,
    load_demo_credentials,
    load_production_credentials,
)
from pmvl_markets.execution.modes import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    LiveTradingNotAuthorized,
    ModeProfile,
)
from pmvl_markets.execution.reconcile import resolve_unknown_order
from pmvl_markets.execution.signals import (
    IneligibilityReason,
    IneligibleSignal,
    ResearchDecision,
)
from pmvl_markets.execution.sizing import SizingPolicy, issue_sizing
from pmvl_markets.execution.transport import SignedTransport

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

DEMO_PROFILE = ModeProfile.of(ExecutionMode.DEMO)
CREDS = KalshiCredentials(
    key_id="k", environment=KalshiEnvironment.DEMO, private_key_pem="x"
)


def _intent(**kw: object) -> OrderIntent:
    now = utcnow()
    base: dict[str, object] = {
        "intent_id": "i1",
        "ticker": "T1",
        "outcome": Outcome.YES,
        "action": OrderAction.BUY,
        "limit_price_cents": 40,
        "quantity": 2,
        "mode": ExecutionMode.DEMO,
        "expected_edge_after_fees": Decimal("0.08"),
        "signal_timestamp": now,
        "market_data_timestamp": now,
        "expires_at": now + timedelta(minutes=5),
    }
    base.update(kw)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _exposure(**kw: object) -> ExposureSnapshot:
    base: dict[str, object] = {
        "balance": AccountBalance(
            balance_usd=Decimal("500"), available_usd=Decimal("500")
        ),
        "realized_loss_today_usd": Decimal("0"),
        "spend_today_usd": Decimal("0"),
        "fetched_at": utcnow(),
    }
    base.update(kw)
    return ExposureSnapshot(**base)  # type: ignore[arg-type]


def _gate() -> RiskGate:
    return RiskGate(kill_switch=KillSwitch())


def _approved(**kw: object) -> AuthorizedExecutionPlan:
    _, plan = _gate().authorize(_intent(**kw), _exposure(), client_order_id="c1")
    assert plan is not None
    return plan


@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    return ExecutionLedger(tmp_path / "execution.db")


# ================================================================ R1: binding
def test_demo_and_production_read_disjoint_config_namespaces() -> None:
    class Cfg:
        kalshi_api_key_id = "PROD-KEY"
        kalshi_private_key_pem = "PROD-PEM"
        kalshi_private_key_path = ""
        kalshi_demo_api_key_id = ""
        kalshi_demo_private_key_pem = ""
        kalshi_demo_private_key_path = ""

    # Production configured, demo not: asking for demo must fail rather than
    # quietly fall back to the production namespace.
    assert load_production_credentials(settings=Cfg()).key_id == "PROD-KEY"
    with pytest.raises(CredentialsUnavailable):
        load_demo_credentials(settings=Cfg())


def test_mode_cannot_be_paired_with_the_other_environments_credentials() -> None:
    prod = KalshiCredentials(
        key_id="k", environment=KalshiEnvironment.PRODUCTION, private_key_pem="x"
    )
    with pytest.raises(CredentialsUnavailable):
        SignedTransport(DEMO_PROFILE, prod)


def test_loaded_credentials_carry_the_environment_of_their_namespace() -> None:
    class Cfg:
        kalshi_api_key_id = ""
        kalshi_private_key_pem = ""
        kalshi_private_key_path = ""
        kalshi_demo_api_key_id = "DEMO-KEY"
        kalshi_demo_private_key_pem = "DEMO-PEM"
        kalshi_demo_private_key_path = ""

    creds = load_demo_credentials(settings=Cfg())
    assert creds.environment is KalshiEnvironment.DEMO


# =========================================================== R2: no bypass
def test_create_order_accepts_only_an_authorized_plan() -> None:
    import inspect

    params = list(inspect.signature(KalshiExecutionClient.create_order).parameters)
    assert params[1:] == ["plan"]


def test_public_seal_constructor_no_longer_exists() -> None:
    """A public sealer is a public authorizer. It was, and it is not now."""
    assert not hasattr(AuthorizedExecutionPlan, "seal")


def test_a_self_sealed_plan_is_refused_because_no_gate_issued_it() -> None:
    """The HMAC proves 'built in this process', not 'approved'."""
    forged = AuthorizedExecutionPlan._seal(
        mode=ExecutionMode.DEMO,
        ticker="ATTACK",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        limit_price_cents=99,
        quantity=1_000_000,
        time_in_force=TimeInForce.GOOD_TILL_CANCELLED,
        client_order_id="forged",
        intent_id="none",
        risk_checked_at=utcnow(),
        authorization_expires_at=utcnow() + timedelta(minutes=1),
        nonce="not-registered",
    )
    with pytest.raises(AuthorizationNotIssued):
        forged.assert_usable(mode=ExecutionMode.DEMO)


def test_an_unsealed_plan_is_refused() -> None:
    forged = AuthorizedExecutionPlan(
        mode=ExecutionMode.DEMO,
        ticker="T1",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        limit_price_cents=40,
        quantity=2,
        time_in_force=TimeInForce.GOOD_TILL_CANCELLED,
        client_order_id="c1",
        intent_id="i1",
        risk_checked_at=utcnow(),
        authorization_expires_at=utcnow() + timedelta(minutes=5),
    )
    with pytest.raises(AuthorizationTampered):
        forged.assert_usable(mode=ExecutionMode.DEMO)


def test_blocked_intent_yields_no_plan() -> None:
    decision, plan = _gate().authorize(
        _intent(quantity=9999), _exposure(), client_order_id="c1"
    )
    assert not decision.passed
    assert plan is None


def test_evaluate_still_exists_but_cannot_authorize() -> None:
    decision = _gate().evaluate(_intent(), _exposure())
    assert decision.passed
    assert not hasattr(decision, "seal_digest")


# ============================================= AJ: immutability under attack
@pytest.mark.parametrize(
    "field,value",
    [
        ("limit_price_cents", 99),
        ("quantity", 500),
        ("ticker", "OTHER"),
        ("outcome", Outcome.NO),
        ("action", OrderAction.SELL),
        ("time_in_force", TimeInForce.FILL_OR_KILL),
        ("client_order_id", "other"),
        ("mode", ExecutionMode.SHADOW),
        ("risk_policy_version", "0.0"),
    ],
)
def test_editing_an_approved_plan_invalidates_it(field: str, value: object) -> None:
    plan = _approved()
    object.__setattr__(plan, field, value)
    with pytest.raises(AuthorizationInvalid):
        plan.assert_usable(mode=ExecutionMode.DEMO)


def test_plan_is_frozen_against_ordinary_assignment() -> None:
    import dataclasses

    plan = _approved()
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.quantity = 99  # type: ignore[misc]


def test_an_untouched_plan_verifies() -> None:
    _approved().assert_usable(mode=ExecutionMode.DEMO)


# ==================================================== AK: expiry at point of use
def test_expired_authorization_is_refused() -> None:
    _, plan = _gate().authorize(
        _intent(), _exposure(), client_order_id="c1", ttl=timedelta(seconds=-1)
    )
    assert plan is not None
    with pytest.raises(AuthorizationExpired):
        plan.assert_usable(mode=ExecutionMode.DEMO)


def test_authorization_carries_a_bounded_ttl() -> None:
    plan = _approved()
    assert plan.authorization_expires_at > plan.risk_checked_at
    assert (
        plan.authorization_expires_at - plan.risk_checked_at
    ) <= MAX_AUTHORIZATION_TTL


def test_a_caller_cannot_ask_for_an_unbounded_ttl() -> None:
    """An approval whose lifetime the caller picks is not a temporal bound."""
    _, plan = _gate().authorize(
        _intent(), _exposure(), client_order_id="c1", ttl=timedelta(days=3650)
    )
    assert plan is not None
    assert (
        plan.authorization_expires_at - plan.risk_checked_at
    ) == MAX_AUTHORIZATION_TTL


def test_an_authorization_is_single_use() -> None:
    plan = _approved()
    plan.assert_usable(mode=ExecutionMode.DEMO, consume=True)
    with pytest.raises(AuthorizationAlreadyUsed):
        plan.assert_usable(mode=ExecutionMode.DEMO, consume=True)


def test_provenance_fields_are_inside_the_seal() -> None:
    """The plan's audit trail must not be rewritable while the seal holds."""
    plan = _approved()
    before = plan.compute_seal()
    object.__setattr__(plan, "signal_timestamp", utcnow() - timedelta(days=99))
    assert plan.compute_seal() != before


def test_plan_from_another_policy_version_is_refused() -> None:
    plan = _approved()
    object.__setattr__(plan, "risk_policy_version", "ancient")
    with pytest.raises(AuthorizationInvalid):
        plan.assert_usable(mode=ExecutionMode.DEMO)


def test_plan_presented_to_the_wrong_mode_gateway_is_refused() -> None:
    plan = _approved()
    with pytest.raises(AuthorizationInvalid):
        plan.assert_usable(mode=ExecutionMode.SHADOW)


# ============================================ R3 / AL: environment isolation
def _mock_client(base_url: str, sink: dict, **kw: object) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sink["url"] = str(request.url)
        return httpx.Response(200, json={"order": {}})

    return httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(handler), **kw
    )


def test_account_client_refuses_a_transport_from_another_environment() -> None:
    """The read path crossed environments silently before this check."""
    prod_profile = ModeProfile.of(ExecutionMode.PRODUCTION_READ_ONLY)
    prod_creds = KalshiCredentials(
        key_id="k", environment=KalshiEnvironment.PRODUCTION, private_key_pem="x"
    )
    sink: dict = {}
    prod_transport = SignedTransport(
        prod_profile, prod_creds, client=_mock_client(PRODUCTION_BASE_URL, sink)
    )
    with pytest.raises(LiveTradingNotAuthorized):
        KalshiAccountClient(ExecutionMode.DEMO, transport=prod_transport)


def test_execution_client_refuses_a_transport_from_another_environment() -> None:
    prod_profile = ModeProfile.of(ExecutionMode.PRODUCTION_READ_ONLY)
    prod_creds = KalshiCredentials(
        key_id="k", environment=KalshiEnvironment.PRODUCTION, private_key_pem="x"
    )
    sink: dict = {}
    prod_transport = SignedTransport(
        prod_profile, prod_creds, client=_mock_client(PRODUCTION_BASE_URL, sink)
    )
    with pytest.raises(LiveTradingNotAuthorized):
        KalshiExecutionClient(ExecutionMode.DEMO, transport=prod_transport)


def test_injected_production_client_still_refused() -> None:
    sink: dict = {}
    with pytest.raises(LiveTradingNotAuthorized):
        SignedTransport(
            DEMO_PROFILE, CREDS, client=_mock_client(PRODUCTION_BASE_URL, sink)
        )
    assert "url" not in sink


def test_absolute_production_url_still_refused() -> None:
    from unittest.mock import patch

    sink: dict = {}
    transport = SignedTransport(
        DEMO_PROFILE, CREDS, client=_mock_client(DEMO_BASE_URL, sink)
    )
    with patch(
        "pmvl_markets.execution.transport.signing_headers", return_value={"X": "y"}
    ):
        with pytest.raises(LiveTradingNotAuthorized):
            asyncio.run(
                transport._request(
                    "POST", PRODUCTION_BASE_URL + "/portfolio/orders", json={"a": 1}
                )
            )
    assert "url" not in sink


def test_redirect_following_client_still_refused() -> None:
    sink: dict = {}
    with pytest.raises(LiveTradingNotAuthorized):
        SignedTransport(
            DEMO_PROFILE,
            CREDS,
            client=_mock_client(DEMO_BASE_URL, sink, follow_redirects=True),
        )


# =========================================================== R4: idempotency v2
def test_time_in_force_changes_the_identity() -> None:
    a = derive_client_order_id(_intent())
    b = derive_client_order_id(_intent(time_in_force=TimeInForce.FILL_OR_KILL))
    assert a != b


def test_identity_is_versioned_v2() -> None:
    assert derive_client_order_id(_intent()).startswith("pmvl-v2-")


@pytest.mark.parametrize(
    "field,value",
    [
        ("limit_price_cents", 41),
        ("quantity", 3),
        ("outcome", Outcome.NO),
        ("action", OrderAction.SELL),
        ("ticker", "T2"),
        ("revision", 1),
        ("time_in_force", TimeInForce.FILL_OR_KILL),
    ],
)
def test_every_execution_semantic_field_changes_the_identity(
    field: str, value: object
) -> None:
    assert derive_client_order_id(_intent()) != derive_client_order_id(
        _intent(**{field: value})
    )


def test_identical_terms_reuse_the_identity() -> None:
    assert derive_client_order_id(_intent()) == derive_client_order_id(_intent())


# ================================================ R5: mode-scoped ledger state
def test_discrepancies_are_scoped_by_mode(ledger: ExecutionLedger) -> None:
    with ledger.connect() as con:
        ledger.record_blocking_discrepancy(
            con, mode=ExecutionMode.DEMO, kind="remote_only", ticker="T1"
        )
        assert len(ledger.open_blocking_discrepancies(con, mode=ExecutionMode.DEMO)) == 1
        assert (
            ledger.open_blocking_discrepancies(
                con, mode=ExecutionMode.PRODUCTION_READ_ONLY
            )
            == []
        )


def test_clearing_one_mode_does_not_clear_another(ledger: ExecutionLedger) -> None:
    with ledger.connect() as con:
        for mode in (ExecutionMode.DEMO, ExecutionMode.PRODUCTION_READ_ONLY):
            ledger.record_blocking_discrepancy(con, mode=mode, kind="remote_only")
        ledger.clear_blocking_discrepancies(con, mode=ExecutionMode.DEMO)
        assert ledger.open_blocking_discrepancies(con, mode=ExecutionMode.DEMO) == []
        assert (
            len(
                ledger.open_blocking_discrepancies(
                    con, mode=ExecutionMode.PRODUCTION_READ_ONLY
                )
            )
            == 1
        )


def test_pre_migration_rows_block_every_mode(tmp_path: Path) -> None:
    """An unattributable discrepancy must fail closed, not vanish."""
    import sqlite3

    path = tmp_path / "execution.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE blocking_discrepancies (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " raised_at TEXT NOT NULL, kind TEXT NOT NULL, ticker TEXT NOT NULL"
        " DEFAULT '', detail TEXT NOT NULL DEFAULT '', cleared_at TEXT)"
    )
    con.execute(
        "INSERT INTO blocking_discrepancies (raised_at, kind) VALUES ('x','remote_only')"
    )
    con.commit()
    con.close()

    led = ExecutionLedger(path)
    with led.connect() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(blocking_discrepancies)")}
        assert "mode" in cols
        for mode in (ExecutionMode.DEMO, ExecutionMode.PRODUCTION_READ_ONLY):
            assert len(led.open_blocking_discrepancies(c, mode=mode)) == 1


def test_orders_are_scoped_by_mode(ledger: ExecutionLedger) -> None:
    with ledger.connect() as con:
        for mode in (ExecutionMode.DEMO, ExecutionMode.SHADOW):
            intent = _intent(mode=mode)
            ledger.record_intent(con, intent, derive_client_order_id(intent))
        assert len(ledger.all_orders(con, mode=ExecutionMode.DEMO)) == 1
        assert len(ledger.all_orders(con, mode=ExecutionMode.SHADOW)) == 1
        assert len(ledger.all_orders(con)) == 2


# ================================================= R6 / AO: UNKNOWN recovery
def _wedged(ledger: ExecutionLedger, mode: ExecutionMode = ExecutionMode.DEMO) -> str:
    intent = _intent(mode=mode)
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        ledger.transition(
            con, coid, OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED, note="timeout"
        )
    return coid


def test_no_unsafe_escape_hatch_is_exported() -> None:
    import pmvl_markets.execution.reconcile as mod

    banned = ("force_clear", "mark_filled", "mark_cancelled", "ignore_unknown")
    assert [n for n in dir(mod) if n in banned] == []


def test_unknown_without_venue_evidence_stays_unknown(ledger: ExecutionLedger) -> None:
    coid = _wedged(ledger)
    result = resolve_unknown_order(
        ledger, coid, mode=ExecutionMode.DEMO, remote_order=None,
        operator_note="operator insists it was cancelled",
    )
    assert result.resolved is False
    assert result.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    with ledger.connect() as con:
        assert ledger.get(con, coid).status is (  # type: ignore[union-attr]
            OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
        )


def test_an_operator_note_alone_authorizes_nothing(ledger: ExecutionLedger) -> None:
    coid = _wedged(ledger)
    resolve_unknown_order(
        ledger, coid, mode=ExecutionMode.DEMO, remote_order=None, operator_note="fine"
    )
    with ledger.connect() as con:
        attempts = ledger.resolution_attempts(con, coid)
    assert len(attempts) == 1
    assert attempts[0]["resolved"] == 0


def test_venue_evidence_resolves_the_order(ledger: ExecutionLedger) -> None:
    coid = _wedged(ledger)
    remote = Order(
        exchange_order_id="X1",
        ticker="T1",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        status=OrderStatus.CANCELLED,
        original_quantity=2,
        remaining_quantity=0,
        client_order_id=coid,
    )
    result = resolve_unknown_order(
        ledger, coid, mode=ExecutionMode.DEMO, remote_order=remote
    )
    assert result.resolved is True
    assert result.status is OrderStatus.CANCELLED


def test_wrong_mode_evidence_cannot_resolve(ledger: ExecutionLedger) -> None:
    coid = _wedged(ledger)
    remote = Order(
        exchange_order_id="X1",
        ticker="T1",
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        status=OrderStatus.CANCELLED,
        original_quantity=2,
        remaining_quantity=0,
        client_order_id=coid,
    )
    result = resolve_unknown_order(
        ledger, coid, mode=ExecutionMode.PRODUCTION_READ_ONLY, remote_order=remote
    )
    assert result.resolved is False


def test_unknown_exposure_blocks_new_submissions(ledger: ExecutionLedger) -> None:
    """An unresolved order in the SAME mode makes that mode untradeable."""
    _wedged(ledger, ExecutionMode.SHADOW)
    gateway = ShadowExecutionGateway(ledger=ledger, kill_switch=KillSwitch())
    _, plan = _gate().authorize(
        _intent(mode=ExecutionMode.SHADOW, ticker="T9"),
        _exposure(),
        client_order_id="c-other",
    )
    assert plan is not None
    with pytest.raises(AuthorizationInvalid):
        asyncio.run(gateway.submit(plan))


def test_an_unresolved_order_in_another_mode_does_not_block_shadow(
    ledger: ExecutionLedger,
) -> None:
    """R5: a wedged DEMO order must not make SHADOW untradeable, or vice versa."""
    _wedged(ledger, ExecutionMode.DEMO)
    gateway = ShadowExecutionGateway(ledger=ledger, kill_switch=KillSwitch())
    intent = _intent(mode=ExecutionMode.SHADOW, ticker="T9")
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
    _, plan = _gate().authorize(intent, _exposure(), client_order_id=coid)
    assert plan is not None
    asyncio.run(gateway.submit(plan))  # must not raise


# ====================================================== signal adapter (Stage 2)
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
        "recommended_position_cap": Decimal("2"),
        "model_version": "TEST_ONLY",
        # Allowlisted live provenance; the row itself is TEST_ONLY.
        "provenance": "live",
        "horizon": "24h",
        "created_at": now,
        "evidence_updated_at": now,
        "close_time": datetime(2099, 1, 1, tzinfo=timezone.utc),
    }
    base.update(kw)
    return ResearchDecision(**base)  # type: ignore[arg-type]


class _FixedSizePolicy:
    """TEST_ONLY sizing: a declared constant, so these tests exercise the path
    without depending on a strategy policy that does not exist."""

    policy = SizingPolicy.OPERATOR_SPECIFIED_DEMO_SMOKE

    def __init__(self, quantity: int = 2) -> None:
        self.quantity = quantity

    def decide(self, *, mode, ticker, capacity_ceiling=None):  # noqa: ANN001
        return issue_sizing(
            requested_quantity=min(self.quantity, capacity_ceiling or self.quantity),
            policy=self.policy,
            policy_version="TEST_ONLY",
            mode=mode,
            ticker=ticker,
            decided_at=utcnow(),
            capacity_ceiling=capacity_ceiling,
            detail="TEST_ONLY fixed size",
        )


def _adapter(**kw: object) -> SignalExecutionAdapter:
    kw.setdefault("sizing_policy", _FixedSizePolicy())
    return SignalExecutionAdapter(mode=ExecutionMode.SHADOW, **kw)  # type: ignore[arg-type]


def test_buy_yes_is_preserved() -> None:
    out = _adapter().to_intent(_decision(side=Side.YES))
    assert isinstance(out, OrderIntent)
    assert out.outcome is Outcome.YES
    assert out.action is OrderAction.BUY


def test_buy_no_is_preserved_not_inverted() -> None:
    out = _adapter().to_intent(_decision(side=Side.NO))
    assert isinstance(out, OrderIntent)
    assert out.outcome is Outcome.NO


def test_adapter_does_not_alter_model_probabilities() -> None:
    d = _decision()
    out = _adapter().to_intent(d)
    assert isinstance(out, OrderIntent)
    assert out.model_probability == d.fair_probability
    assert out.conservative_probability == d.conservative_probability
    assert out.expected_edge_after_fees == d.conservative_net_ev


def test_adapter_uses_the_decisions_own_price_as_the_ceiling() -> None:
    out = _adapter().to_intent(_decision(entry_price=Decimal("0.40")))
    assert isinstance(out, OrderIntent)
    assert out.limit_price_cents == 40
    assert out.max_acceptable_price_cents == 40


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"state": RecommendationState.SETTLED}, IneligibilityReason.NOT_ACTIONABLE),
        (
            {"state": RecommendationState.NO_LONGER_ACTIONABLE},
            IneligibilityReason.NOT_ACTIONABLE,
        ),
        (
            {"state": RecommendationState.MARKET_CLOSED},
            IneligibilityReason.NOT_ACTIONABLE,
        ),
        ({"ticker": ""}, IneligibilityReason.MISSING_TICKER),
        ({"platform": "polymarket"}, IneligibilityReason.NOT_KALSHI),
        ({"market_status": "closed"}, IneligibilityReason.MARKET_NOT_OPEN),
        (
            {"close_time": datetime(2020, 1, 1, tzinfo=timezone.utc)},
            IneligibilityReason.MARKET_CLOSED,
        ),
        ({"entry_price": None}, IneligibilityReason.MISSING_EXECUTABLE_PRICE),
        ({"entry_price": Decimal("1.5")}, IneligibilityReason.PRICE_OUT_OF_RANGE),
        ({"conservative_net_ev": None}, IneligibilityReason.MISSING_EDGE),
        ({"recommended_position_cap": None}, IneligibilityReason.MISSING_SIZE),
        ({"recommended_position_cap": Decimal("0")}, IneligibilityReason.MISSING_SIZE),
    ],
)
def test_ineligible_decisions_never_become_orders(
    kw: dict, reason: IneligibilityReason
) -> None:
    out = _adapter().to_intent(_decision(**kw))
    assert isinstance(out, IneligibleSignal)
    assert out.reason is reason


def test_stale_decisions_are_refused() -> None:
    old = utcnow() - timedelta(days=3)
    out = _adapter().to_intent(_decision(created_at=old, evidence_updated_at=old))
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.STALE_SIGNAL


def test_demo_provenance_is_refused_by_default() -> None:
    out = _adapter().to_intent(_decision(provenance="demo"))
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.DEMO_PROVENANCE


# ================================================================ shadow path
def test_shadow_pipeline_authorizes_and_records(ledger: ExecutionLedger) -> None:
    pipe = ShadowSignalPipeline(
        ledger=ledger, adapter=_adapter(), risk_gate=_gate()
    )
    report = asyncio.run(pipe.run([_decision()]))
    assert report.signals_seen == 1
    assert report.authorized == 1
    with ledger.connect() as con:
        rows = ledger.all_orders(con, mode=ExecutionMode.SHADOW)
    assert [r.status for r in rows] == [OrderStatus.SHADOW_AUTHORIZED]


def test_shadow_never_claims_a_venue_outcome() -> None:
    from pmvl_markets.execution.models import is_valid_transition

    for venue_only in (
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.OPEN,
    ):
        assert not is_valid_transition(OrderStatus.SHADOW_AUTHORIZED, venue_only)


def test_shadow_gateway_holds_no_transport(ledger: ExecutionLedger) -> None:
    gateway = ShadowExecutionGateway(ledger=ledger, kill_switch=KillSwitch())
    assert not hasattr(gateway, "_client")
    assert not hasattr(gateway, "_transport")


def test_shadow_deduplicates_the_same_decision(ledger: ExecutionLedger) -> None:
    pipe = ShadowSignalPipeline(ledger=ledger, adapter=_adapter(), risk_gate=_gate())
    asyncio.run(pipe.run([_decision()]))
    again = asyncio.run(pipe.run([_decision()]))
    assert again.authorized == 0
    assert again.deduplicated == 1


def test_shadow_survives_a_restart_without_duplicating(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution.db"
    first = ShadowSignalPipeline(
        ledger=ExecutionLedger(path), adapter=_adapter(), risk_gate=_gate()
    )
    asyncio.run(first.run([_decision()]))
    # A fresh ledger object stands in for a new process against the same file.
    second = ShadowSignalPipeline(
        ledger=ExecutionLedger(path), adapter=_adapter(), risk_gate=_gate()
    )
    report = asyncio.run(second.run([_decision()]))
    assert report.authorized == 0
    assert report.deduplicated == 1


@pytest.mark.parametrize("arm", ["config", "runtime", "persistent"])
def test_kill_switch_blocks_a_real_signal(arm: str, tmp_path: Path) -> None:
    if arm == "config":
        switch = KillSwitch(config_disabled=True)
    elif arm == "runtime":
        switch = KillSwitch()
        switch.engage("operator stop")
    else:
        flag = tmp_path / "EXECUTION_DISABLED"
        flag.write_text("stop")
        switch = KillSwitch(flag_path=flag)

    ledger = ExecutionLedger(tmp_path / "execution.db")
    pipe = ShadowSignalPipeline(
        ledger=ledger,
        adapter=_adapter(),
        risk_gate=RiskGate(kill_switch=switch),
    )
    report = asyncio.run(pipe.run([_decision()]))
    assert report.authorized == 0
    assert "kill_switch" in report.risk_blocked


def test_shadow_account_state_counts_authorized_plans_as_exposure(
    ledger: ExecutionLedger,
) -> None:
    pipe = ShadowSignalPipeline(ledger=ledger, adapter=_adapter(), risk_gate=_gate())
    asyncio.run(pipe.run([_decision()]))
    snapshot = ShadowAccountState(ledger=ledger).snapshot()
    assert len(snapshot.resting_orders) == 1
    assert snapshot.balance is not None


def test_shadow_balance_is_virtual_not_the_real_account(
    ledger: ExecutionLedger,
) -> None:
    from pmvl_markets.execution.shadow import SHADOW_VIRTUAL_BALANCE_USD

    snapshot = ShadowAccountState(ledger=ledger).snapshot()
    assert snapshot.balance is not None
    assert snapshot.balance.balance_usd == SHADOW_VIRTUAL_BALANCE_USD
    assert SHADOW_VIRTUAL_BALANCE_USD <= Decimal("1000")


def test_every_signal_outcome_is_counted(ledger: ExecutionLedger) -> None:
    """The denominator must survive: nothing is silently dropped."""
    pipe = ShadowSignalPipeline(ledger=ledger, adapter=_adapter(), risk_gate=_gate())
    decisions = [
        _decision(recommendation_id=1),
        _decision(recommendation_id=2, state=RecommendationState.SETTLED),
        _decision(recommendation_id=3, platform="polymarket"),
        _decision(recommendation_id=4, entry_price=Decimal("0.99")),
    ]
    report = asyncio.run(pipe.run(decisions))
    accounted = (
        sum(report.ineligible.values())
        + sum(report.risk_blocked.values())
        + report.authorized
        + report.deduplicated
    )
    assert report.signals_seen == 4
    assert accounted >= 4


def test_shadow_reports_no_pnl_fields() -> None:
    from pmvl_markets.execution.shadow import ShadowRunReport

    keys = set(ShadowRunReport().as_dict())
    for banned in ("pnl", "profit", "win_rate", "expectancy", "realized"):
        assert not any(banned in k for k in keys)


# ============================== findings from the Phase 5B adversarial review
def test_open_orders_is_mode_scoped(ledger: ExecutionLedger) -> None:
    """A production reconcile must not read demo rows."""
    import inspect

    assert "mode" in inspect.signature(ExecutionLedger.open_orders).parameters
    with ledger.connect() as con:
        for mode in (ExecutionMode.DEMO, ExecutionMode.SHADOW):
            intent = _intent(mode=mode)
            coid = derive_client_order_id(intent)
            ledger.record_intent(con, intent, coid)
            ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        assert len(ledger.open_orders(con, mode=ExecutionMode.DEMO)) == 1
        assert len(ledger.open_orders(con, mode=ExecutionMode.SHADOW)) == 1
        assert len(ledger.open_orders(con)) == 2


def test_production_reconcile_does_not_touch_demo_rows(ledger: ExecutionLedger) -> None:
    from pmvl_markets.execution.reconcile import recover_after_restart

    demo_intent = _intent(mode=ExecutionMode.DEMO)
    coid = derive_client_order_id(demo_intent)
    with ledger.connect() as con:
        ledger.record_intent(con, demo_intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        ledger.transition(con, coid, OrderStatus.OPEN, exchange_order_id="demo-1")

    production_position = Position(
        ticker="REAL-TICKER", outcome=Outcome.NO, quantity=250
    )
    recover_after_restart(
        ledger, [], [], [production_position], mode=ExecutionMode.PRODUCTION_READ_ONLY
    )

    with ledger.connect() as con:
        record = ledger.get(con, coid)
        demo_blocking = ledger.open_blocking_discrepancies(
            con, mode=ExecutionMode.DEMO
        )
        prod_blocking = ledger.open_blocking_discrepancies(
            con, mode=ExecutionMode.PRODUCTION_READ_ONLY
        )
    assert record is not None
    assert record.status is OrderStatus.OPEN, "demo order was wedged by a prod reconcile"
    assert demo_blocking == []
    assert len(prod_blocking) == 1


def test_discrepancies_can_be_cleared_by_an_operator(ledger: ExecutionLedger) -> None:
    """Write-only discrepancies wedge a mode permanently with no remedy."""
    with ledger.connect() as con:
        ledger.record_blocking_discrepancy(
            con, mode=ExecutionMode.DEMO, kind="position_mismatch", ticker="T1"
        )
        assert ledger.clear_blocking_discrepancies(con, mode=ExecutionMode.DEMO) == 1
        assert ledger.open_blocking_discrepancies(con, mode=ExecutionMode.DEMO) == []


def test_shadow_records_submitted_at_so_daily_spend_is_measurable(
    ledger: ExecutionLedger,
) -> None:
    """A hard zero reads as 'measured', so the daily-spend cap must see real spend."""
    from pmvl_shared.timeutil import utcnow as _now

    pipe = ShadowSignalPipeline(ledger=ledger, adapter=_adapter(), risk_gate=_gate())
    asyncio.run(pipe.run([_decision()]))
    with ledger.connect() as con:
        spend = ledger.spend_today_usd(
            con, today=_now().strftime("%Y-%m-%d"), mode=ExecutionMode.SHADOW
        )
    assert spend > Decimal("0")


def test_cancel_is_refused_while_the_kill_switch_is_engaged(
    ledger: ExecutionLedger,
) -> None:
    """A cancel is a venue mutation; the stop control has to reach it."""
    from pmvl_markets.execution.harness import ExecutionHarness

    switch = KillSwitch()
    switch.engage("operator stop")
    harness = ExecutionHarness(
        ExecutionMode.SHADOW, ledger=ledger, risk_gate=RiskGate(kill_switch=switch)
    )
    with pytest.raises(LiveTradingNotAuthorized):
        asyncio.run(harness.cancel("anything"))


def test_adapter_refuses_to_invent_a_position_size() -> None:
    """`recommended_position_cap` is a capacity ceiling, not a size."""
    bare = SignalExecutionAdapter(mode=ExecutionMode.SHADOW)
    out = bare.to_intent(_decision(recommended_position_cap=Decimal("5000")))
    assert isinstance(out, IneligibleSignal)
    assert out.reason is IneligibilityReason.SIZE_POLICY_UNDEFINED


def test_declared_size_is_capped_by_research_capacity() -> None:
    sized = SignalExecutionAdapter(
        mode=ExecutionMode.SHADOW, sizing_policy=_FixedSizePolicy(10)
    )
    out = sized.to_intent(_decision(recommended_position_cap=Decimal("3")))
    assert isinstance(out, OrderIntent)
    assert out.quantity == 3


def test_matches_intent_is_gone_rather_than_dead() -> None:
    """A documented safeguard with no call sites reads as protection that isn't."""
    assert not hasattr(AuthorizedExecutionPlan, "matches_intent")
