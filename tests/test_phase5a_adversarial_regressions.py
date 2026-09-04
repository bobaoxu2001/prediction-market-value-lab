"""Regressions for defects an adversarial review actually reproduced.

Each test here corresponds to a bypass that WORKED against the first cut of the
execution harness. They are kept separate from the feature tests because their
value is historical: they encode attacks that succeeded once, and a refactor that
reopens any of them is not a style regression.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from pmvl_markets.execution import (
    AccountBalance,
    BlockReason,
    DiscrepancyKind,
    ExecutionLedger,
    ExecutionMode,
    ExposureSnapshot,
    KillSwitch,
    Order,
    OrderAction,
    OrderStatus,
    Outcome,
    Position,
    RiskGate,
    reconcile_positions,
)
from pmvl_markets.execution.credentials import KalshiCredentials, KalshiEnvironment
from pmvl_markets.execution.modes import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    LiveTradingNotAuthorized,
    ModeProfile,
    assert_transport_write_allowed,
)
from pmvl_markets.execution.transport import SignedTransport

DEMO_PROFILE = ModeProfile.of(ExecutionMode.DEMO)
CREDS = KalshiCredentials(
    key_id="k", environment=KalshiEnvironment.DEMO, private_key_pem="x"
)


def _mock_client(base_url: str, sink: dict, **kw) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sink["url"] = str(request.url)
        return httpx.Response(200, json={"order": {}})

    return httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(handler), **kw
    )


# ------------------------------------------------- the transport bypass (CRIT)
def test_injected_production_client_is_refused_at_construction() -> None:
    """A demo transport handed a production-pointed client used to send there."""
    sink: dict = {}
    with pytest.raises(LiveTradingNotAuthorized):
        SignedTransport(
            DEMO_PROFILE, CREDS, client=_mock_client(PRODUCTION_BASE_URL, sink)
        )
    assert "url" not in sink


def test_client_with_redirects_enabled_is_refused() -> None:
    sink: dict = {}
    with pytest.raises(LiveTradingNotAuthorized):
        SignedTransport(
            DEMO_PROFILE,
            CREDS,
            client=_mock_client(DEMO_BASE_URL, sink, follow_redirects=True),
        )


def test_absolute_url_as_path_cannot_escape_the_demo_host() -> None:
    """`path` is caller-controlled; an absolute URL overrides base_url in httpx."""
    sink: dict = {}
    transport = SignedTransport(DEMO_PROFILE, CREDS, client=_mock_client(DEMO_BASE_URL, sink))
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


def test_a_legitimate_demo_write_still_reaches_the_demo_host() -> None:
    """The guard must refuse the attack without breaking the supported path."""
    sink: dict = {}
    transport = SignedTransport(DEMO_PROFILE, CREDS, client=_mock_client(DEMO_BASE_URL, sink))
    with patch(
        "pmvl_markets.execution.transport.signing_headers", return_value={"X": "y"}
    ):
        asyncio.run(transport._request("POST", "/portfolio/orders", json={"a": 1}))
    assert sink["url"].startswith(DEMO_BASE_URL)


def test_guard_refuses_every_non_get_verb_not_only_the_named_four() -> None:
    for verb in ("OPTIONS", "HEAD", "TRACE", "PROPFIND", "post"):
        with pytest.raises(LiveTradingNotAuthorized):
            assert_transport_write_allowed(verb, PRODUCTION_BASE_URL + "/x")
    assert_transport_write_allowed("GET", PRODUCTION_BASE_URL + "/x")


# ------------------------------------------------ kill switch failing OPEN
def test_kill_switch_path_is_absolute_so_a_cd_cannot_disarm_it() -> None:
    from pmvl_shared.config import get_settings

    settings = get_settings()
    assert Path(settings.execution_kill_switch_path).is_absolute()
    assert Path(settings.execution_db_path).is_absolute()


def test_default_risk_gate_consults_the_configured_kill_switch() -> None:
    """`RiskGate()` used to build a switch that checked nothing at all."""
    gate = RiskGate()
    assert gate.kill_switch.engaged is True  # execution_enabled defaults to False


def test_bare_kill_switch_is_still_available_for_tests() -> None:
    assert KillSwitch().engaged is False


# --------------------------------------------- position orientation collapse
def test_the_exact_inverse_position_does_not_reconcile_clean() -> None:
    """Long 5 YES vs long 5 NO is a total inversion, not a match."""
    local = {("T1", "yes"): 5}
    remote = [Position(ticker="T1", outcome=Outcome.NO, quantity=5)]
    out = reconcile_positions(local, remote)
    assert [d.kind for d in out] == [
        DiscrepancyKind.POSITION_MISMATCH,
        DiscrepancyKind.POSITION_MISMATCH,
    ]


def test_matching_orientation_still_reconciles() -> None:
    local = {("T1", "yes"): 5}
    remote = [Position(ticker="T1", outcome=Outcome.YES, quantity=5)]
    assert reconcile_positions(local, remote) == []


# ------------------------------------- orphan discrepancies were never stored
def test_a_blocking_discrepancy_with_no_local_row_is_persisted(
    tmp_path: Path,
) -> None:
    ledger = ExecutionLedger(tmp_path / "execution.db")
    with ledger.connect() as con:
        assert ledger.open_blocking_discrepancies(con) == []
        ledger.record_blocking_discrepancy(
            con, kind="remote_only", ticker="T1", detail="ghost order at venue"
        )
        assert len(ledger.open_blocking_discrepancies(con)) == 1
        assert ledger.clear_blocking_discrepancies(con) == 1
        assert ledger.open_blocking_discrepancies(con) == []


# ------------------------------------------- daily caps were structurally dead
def _exposure(**kw: object) -> ExposureSnapshot:
    base: dict[str, object] = {
        "balance": AccountBalance(
            balance_usd=Decimal("500"), available_usd=Decimal("500")
        ),
        "realized_loss_today_usd": Decimal("0"),
        "spend_today_usd": Decimal("0"),
    }
    base.update(kw)
    return ExposureSnapshot(**base)  # type: ignore[arg-type]


def test_unmeasured_daily_loss_blocks_rather_than_reading_as_zero() -> None:
    from tests.test_phase5a_execution_risk import _intent  # reuse the builder

    decision = RiskGate(kill_switch=KillSwitch()).evaluate(
        _intent(), _exposure(realized_loss_today_usd=None)
    )
    assert BlockReason.EXPOSURE_UNKNOWN in set(decision.reasons)


def test_unmeasured_daily_spend_blocks_rather_than_reading_as_zero() -> None:
    from tests.test_phase5a_execution_risk import _intent

    decision = RiskGate(kill_switch=KillSwitch()).evaluate(
        _intent(), _exposure(spend_today_usd=None)
    )
    assert BlockReason.EXPOSURE_UNKNOWN in set(decision.reasons)


def test_ledger_measures_todays_spend(tmp_path: Path) -> None:
    from pmvl_markets.execution import derive_client_order_id
    from pmvl_shared.timeutil import utcnow

    from tests.test_phase5a_execution_ledger import _intent as _ledger_intent

    ledger = ExecutionLedger(tmp_path / "execution.db")
    intent = _ledger_intent(limit_price_cents=40, quantity=4)
    coid = derive_client_order_id(intent)
    today = utcnow().strftime("%Y-%m-%d")
    with ledger.connect() as con:
        assert ledger.spend_today_usd(con, today=today) == Decimal("0")
        ledger.record_intent(con, intent, coid)
        # Not submitted yet - an intent is not a spend.
        assert ledger.spend_today_usd(con, today=today) == Decimal("0")
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        assert ledger.spend_today_usd(con, today=today) == Decimal("1.60")


# --------------------------------------- FILLED without evidence / bad cancel
def test_create_response_claiming_filled_does_not_write_filled(
    tmp_path: Path,
) -> None:
    """The venue saying "filled" is a claim to verify, not a fill to record."""
    from pmvl_markets.execution import derive_client_order_id
    from pmvl_markets.execution.harness import ExecutionHarness

    from tests.test_phase5a_execution_ledger import _intent as _ledger_intent

    ledger = ExecutionLedger(tmp_path / "execution.db")
    intent = _ledger_intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)

    harness = ExecutionHarness(
        mode=ExecutionMode.DEMO, ledger=ledger, risk_gate=RiskGate(kill_switch=KillSwitch())
    )
    harness._apply_submit_response(  # type: ignore[attr-defined]
        intent, coid, {"order": {"order_id": "X1", "status": "executed"}}
    )
    with ledger.connect() as con:
        record = ledger.get(con, coid)
    assert record is not None
    assert record.status is not OrderStatus.FILLED
    assert record.filled_quantity == 0


def test_nothing_to_cancel_is_not_recorded_as_cancelled(tmp_path: Path) -> None:
    """A 404 on cancel is indistinguishable from "it already filled"."""
    from pmvl_markets.execution import derive_client_order_id

    from tests.test_phase5a_execution_ledger import _intent as _ledger_intent

    ledger = ExecutionLedger(tmp_path / "execution.db")
    intent = _ledger_intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
        ledger.transition(con, coid, OrderStatus.OPEN, exchange_order_id="X1")
        record = ledger.get(con, coid)
    assert record is not None and record.status is OrderStatus.OPEN


# --------------------------------------------------- operator-CLI duplicates
def test_repeating_an_operator_command_does_not_mint_a_new_identity() -> None:
    from pmvl_worker.execution_cli import _build_intent
    from pmvl_markets.execution import derive_client_order_id

    a = _build_intent(ExecutionMode.SHADOW, "T1", "yes", "buy", 40, 2, "0.05")
    b = _build_intent(ExecutionMode.SHADOW, "T1", "yes", "buy", 40, 2, "0.05")
    assert derive_client_order_id(a) == derive_client_order_id(b)


def test_a_different_operator_order_still_gets_a_different_identity() -> None:
    from pmvl_worker.execution_cli import _build_intent
    from pmvl_markets.execution import derive_client_order_id

    a = _build_intent(ExecutionMode.SHADOW, "T1", "yes", "buy", 40, 2, "0.05")
    b = _build_intent(ExecutionMode.SHADOW, "T1", "yes", "buy", 41, 2, "0.05")
    c = _build_intent(
        ExecutionMode.SHADOW, "T1", "yes", "buy", 40, 2, "0.05", decision_epoch="t2"
    )
    ids = {derive_client_order_id(x) for x in (a, b, c)}
    assert len(ids) == 3
