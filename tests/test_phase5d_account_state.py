"""Phase 5D: understanding a real account, without being able to change one.

Driven through the real reader, client, transport and ledger against the fake
venue, with only the socket replaced. The real-account burn-in is blocked on
credentials; what these tests establish is the half that does not need a venue -
completeness, freshness, ownership, exposure arithmetic and the refusal to
mutate.
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
    AccountStateCompleteness,
    BlockReason,
    ExecutionLedger,
    ExecutionMode,
    ExposureSnapshot,
    KalshiAccountClient,
    KillSwitch,
    ObjectProvenance,
    OrderAction,
    OrderIntent,
    OrderStatus,
    Outcome,
    ProductionReadOnlyAccountState,
    RiskGate,
    derive_client_order_id,
)
from pmvl_markets.execution.credentials import KalshiCredentials, KalshiEnvironment
from pmvl_markets.execution.modes import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    LiveTradingNotAuthorized,
    ModeProfile,
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
    with patch(
        "pmvl_markets.execution.transport.signing_headers",
        return_value={"KALSHI-ACCESS-KEY": "k"},
    ):
        yield


def _account(venue: FakeKalshiDemo) -> KalshiAccountClient:
    return KalshiAccountClient(
        ExecutionMode.DEMO,
        transport=SignedTransport(
            DEMO_PROFILE,
            CREDS,
            client=httpx.AsyncClient(
                base_url=DEMO_BASE_URL, transport=httpx.MockTransport(venue.handler)
            ),
        ),
    )


def _reader(venue: FakeKalshiDemo, ledger: ExecutionLedger | None = None):
    return ProductionReadOnlyAccountState(
        _account(venue), ledger=ledger, mode=ExecutionMode.DEMO
    )


def _snapshot(venue: FakeKalshiDemo, ledger: ExecutionLedger | None = None):
    return asyncio.run(_reader(venue, ledger).snapshot())


def _intent(**kw: object) -> OrderIntent:
    now = utcnow()
    base: dict[str, object] = {
        "intent_id": "i1",
        "ticker": TICKER,
        "outcome": Outcome.YES,
        "action": OrderAction.BUY,
        "limit_price_cents": 40,
        "quantity": 1,
        "mode": ExecutionMode.DEMO,
        "expected_edge_after_fees": Decimal("0.08"),
        "signal_timestamp": now,
        "market_data_timestamp": now,
        "expires_at": now + timedelta(minutes=5),
    }
    base.update(kw)
    return OrderIntent(**base)  # type: ignore[arg-type]


# ============================================ the reader cannot write, at all
def test_the_reader_refuses_a_client_that_can_write(venue: FakeKalshiDemo) -> None:
    """The guarantee is that it holds nothing to write with."""

    class Writable:
        async def get_balance(self): ...
        async def get_positions(self): ...
        async def get_orders(self): ...
        async def get_fills(self): ...
        async def create_order(self, plan): ...

    with pytest.raises(ValueError):
        ProductionReadOnlyAccountState(Writable(), mode=ExecutionMode.DEMO)


def test_the_account_client_has_no_mutating_method() -> None:
    for forbidden in ("create_order", "cancel_order", "amend_order"):
        assert not hasattr(KalshiAccountClient, forbidden)


def test_production_cannot_construct_a_write_client() -> None:
    from pmvl_markets.execution import KalshiExecutionClient

    with pytest.raises(LiveTradingNotAuthorized):
        KalshiExecutionClient(ExecutionMode.PRODUCTION_READ_ONLY)


def test_reading_an_account_issues_only_get(venue: FakeKalshiDemo) -> None:
    _snapshot(venue)
    assert venue.requests
    assert {m for m, _ in venue.requests} == {"GET"}


# ====================================================== §26 partial API failure
def test_a_complete_read_is_marked_complete(venue: FakeKalshiDemo) -> None:
    snap = _snapshot(venue)
    assert snap.completeness is AccountStateCompleteness.COMPLETE
    assert snap.failures == {}
    assert snap.is_complete


@pytest.mark.parametrize(
    "endpoint", ["/portfolio/balance", "/portfolio/positions", "/portfolio/orders",
                 "/portfolio/fills"]
)
def test_one_failed_endpoint_makes_the_whole_read_incomplete(
    venue: FakeKalshiDemo, endpoint: str
) -> None:
    """Three good endpoints are not a partial account; they are the wrong one."""
    original = venue.handler

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(endpoint):
            return httpx.Response(500, json={"error": "boom"})
        return original(request)

    venue.handler = flaky  # type: ignore[method-assign]
    snap = _snapshot(venue)
    assert snap.completeness is AccountStateCompleteness.INCOMPLETE
    assert snap.failures


def test_an_incomplete_read_blocks_rather_than_looking_flat(
    venue: FakeKalshiDemo,
) -> None:
    original = venue.handler

    def no_orders(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/portfolio/orders"):
            return httpx.Response(503, json={"error": "unavailable"})
        return original(request)

    venue.handler = no_orders  # type: ignore[method-assign]
    exposure = _snapshot(venue).as_exposure()
    assert exposure.complete is False
    decision = RiskGate(kill_switch=KillSwitch()).evaluate(_intent(), exposure)
    assert BlockReason.ACCOUNT_STATE_INCOMPLETE in set(decision.reasons)


# ============================================================ §24 freshness
def _fresh_exposure(**kw: object) -> ExposureSnapshot:
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


def test_a_fresh_complete_snapshot_passes() -> None:
    decision = RiskGate(kill_switch=KillSwitch()).evaluate(_intent(), _fresh_exposure())
    assert decision.passed


def test_a_stale_snapshot_blocks() -> None:
    stale = _fresh_exposure(fetched_at=utcnow() - timedelta(minutes=10))
    decision = RiskGate(kill_switch=KillSwitch()).evaluate(_intent(), stale)
    assert BlockReason.ACCOUNT_STATE_STALE in set(decision.reasons)


def test_a_snapshot_with_no_observation_time_blocks() -> None:
    decision = RiskGate(kill_switch=KillSwitch()).evaluate(
        _intent(), _fresh_exposure(fetched_at=None)
    )
    assert BlockReason.ACCOUNT_STATE_STALE in set(decision.reasons)


def test_the_freshness_cap_is_short() -> None:
    from pmvl_markets.execution import CONSERVATIVE_LIMITS, MAX_ACCOUNT_STATE_AGE

    assert CONSERVATIVE_LIMITS.max_account_state_age <= timedelta(minutes=5)
    assert MAX_ACCOUNT_STATE_AGE <= timedelta(minutes=5)


def test_snapshot_reports_its_own_age(venue: FakeKalshiDemo) -> None:
    snap = _snapshot(venue)
    assert snap.is_fresh()
    assert snap.age().total_seconds() >= 0
    assert snap.as_dict()["fresh"] is True


# ================================== §13/§14 external vs bot-managed ownership
def test_an_order_this_system_never_placed_is_external_not_bot_managed(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.rest_order(
        {"client_order_id": "someone-elses", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 4, "yes_price": 30}
    )
    snap = _snapshot(venue, ledger)
    assert len(snap.open_orders) == 1
    order = snap.open_orders[0]
    assert snap.provenance[f"order:{order.exchange_order_id}"] == (
        ObjectProvenance.PREEXISTING_EXTERNAL.value
    )
    assert snap.bot_managed_orders() == ()
    assert len(snap.external_orders()) == 1


def test_bot_ownership_requires_a_matching_ledger_row(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        # Ownership needs evidence the order actually reached a venue. An
        # INTENDED row alone is a plan, not a placement.
        ledger.transition(con, coid, OrderStatus.SUBMITTING, submitted=True)
    venue.rest_order(
        {"client_order_id": coid, "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 1, "yes_price": 40}
    )
    snap = _snapshot(venue, ledger)
    order = snap.open_orders[0]
    assert snap.provenance[f"order:{order.exchange_order_id}"] == (
        ObjectProvenance.BOT_MANAGED.value
    )


def test_an_order_with_no_client_id_is_unknown_not_ours(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.rest_order(
        {"client_order_id": None, "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 2, "yes_price": 30}
    )
    snap = _snapshot(venue, ledger)
    order = snap.open_orders[0]
    assert snap.provenance[f"order:{order.exchange_order_id}"] == (
        ObjectProvenance.UNKNOWN.value
    )


def test_an_unreadable_ledger_yields_unknown_not_assumed_ownership(
    venue: FakeKalshiDemo,
) -> None:
    class Exploding:
        def connect(self):  # noqa: ANN201
            raise OSError("ledger unavailable")

    venue.rest_order(
        {"client_order_id": "c1", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 1, "yes_price": 40}
    )
    reader = ProductionReadOnlyAccountState(
        _account(venue), ledger=Exploding(), mode=ExecutionMode.DEMO
    )
    snap = asyncio.run(reader.snapshot())
    order = snap.open_orders[0]
    assert snap.provenance[f"order:{order.exchange_order_id}"] != (
        ObjectProvenance.BOT_MANAGED.value
    )


def test_positions_are_never_claimed_as_bot_managed(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """The venue reports no creating order on a position, so ownership is unknowable."""
    venue.positions[(TICKER, "yes")] = 3
    snap = _snapshot(venue, ledger)
    assert snap.provenance[f"position:{TICKER}:yes"] == ObjectProvenance.UNKNOWN.value


# ==================================== §13/§18 external exposure still counts
def test_a_manual_position_counts_toward_risk(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.positions[(TICKER, "yes")] = 40
    exposure = _snapshot(venue, ledger).as_exposure()
    decision = RiskGate(kill_switch=KillSwitch()).evaluate(_intent(), exposure)
    assert BlockReason.MARKET_EXPOSURE_OVER_CAP in set(decision.reasons)


def test_a_manual_resting_order_counts_toward_risk(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """An account with no position but a large working bid is not flat."""
    venue.rest_order(
        {"client_order_id": "manual", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 40, "yes_price": 90}
    )
    exposure = _snapshot(venue, ledger).as_exposure()
    decision = RiskGate(kill_switch=KillSwitch()).evaluate(_intent(), exposure)
    assert BlockReason.MARKET_EXPOSURE_OVER_CAP in set(decision.reasons)


def test_external_ownership_does_not_exempt_exposure(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """Every resting order is in the exposure view, whoever placed it."""
    venue.rest_order(
        {"client_order_id": "not-ours", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 3, "yes_price": 40}
    )
    snap = _snapshot(venue, ledger)
    assert len(snap.external_orders()) == 1
    assert len(snap.as_exposure().resting_orders) == 1


def test_yes_and_no_positions_stay_separate(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.positions[(TICKER, "yes")] = 2
    venue.positions[(TICKER, "no")] = 5
    snap = _snapshot(venue, ledger)
    by_outcome = {(p.ticker, p.outcome) for p in snap.positions}
    assert (TICKER, Outcome.YES) in by_outcome
    assert (TICKER, Outcome.NO) in by_outcome
    assert len(snap.positions) == 2


# =========================================== §16 balance is not a risk budget
def test_a_large_balance_does_not_raise_any_cap(venue: FakeKalshiDemo) -> None:
    venue.balance_cents = 500_000_00
    exposure = _snapshot(venue).as_exposure()
    decision = RiskGate(kill_switch=KillSwitch()).evaluate(
        _intent(quantity=500), exposure
    )
    assert BlockReason.QUANTITY_OVER_CAP in set(decision.reasons)


# ================================================= §7/§23 snapshot durability
def test_snapshots_are_persisted_with_their_provenance(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.positions[(TICKER, "yes")] = 2
    snap = _snapshot(venue, ledger)
    with ledger.connect() as con:
        ledger.record_account_snapshot(con, snap)
        rows = ledger.account_snapshots(con, mode=ExecutionMode.DEMO)
    assert len(rows) == 1
    assert rows[0]["completeness"] == "complete"
    assert rows[0]["positions"] == 1
    assert rows[0]["mode"] == ExecutionMode.DEMO.value


def test_incomplete_cycles_are_recorded_too(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """A burn-in that kept only its successes would overstate its reliability."""
    original = venue.handler

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/portfolio/fills"):
            return httpx.Response(500, json={"error": "boom"})
        return original(request)

    venue.handler = flaky  # type: ignore[method-assign]
    snap = _snapshot(venue, ledger)
    with ledger.connect() as con:
        ledger.record_account_snapshot(con, snap)
        rows = ledger.account_snapshots(con, mode=ExecutionMode.DEMO)
    assert rows[0]["completeness"] == "incomplete"
    assert "fills" in rows[0]["failures"]


def test_snapshot_history_is_mode_scoped(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    snap = _snapshot(venue, ledger)
    with ledger.connect() as con:
        ledger.record_account_snapshot(con, snap)
        assert len(ledger.account_snapshots(con, mode=ExecutionMode.DEMO)) == 1
        assert (
            ledger.account_snapshots(con, mode=ExecutionMode.PRODUCTION_READ_ONLY) == []
        )


def test_the_operator_summary_exposes_no_account_identifiers(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.rest_order(
        {"client_order_id": "secret-coid", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 1, "yes_price": 40}
    )
    blob = str(_snapshot(venue, ledger).as_dict()).lower()
    for banned in ("secret-coid", "demo-key", "private", "signature"):
        assert banned not in blob


# ================================================ §25 pagination completeness
def test_every_page_of_each_endpoint_is_read(venue: FakeKalshiDemo) -> None:
    calls = {"orders": 0, "positions": 0, "fills": 0}

    def paged(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for key, envelope in (
            ("orders", "orders"),
            ("positions", "market_positions"),
            ("fills", "fills"),
        ):
            if path.endswith(f"/portfolio/{key}"):
                calls[key] += 1
                if calls[key] == 1:
                    return httpx.Response(200, json={envelope: [], "cursor": "p2"})
                return httpx.Response(200, json={envelope: []})
        return httpx.Response(200, json={"balance": 1000})

    venue.handler = paged  # type: ignore[method-assign]
    snap = _snapshot(venue)
    assert snap.is_complete
    assert calls == {"orders": 2, "positions": 2, "fills": 2}


def test_an_unreadable_pagination_cursor_makes_the_read_incomplete(
    venue: FakeKalshiDemo,
) -> None:
    def endless(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/portfolio/orders"):
            return httpx.Response(200, json={"orders": [], "cursor": "forever"})
        return httpx.Response(200, json={"balance": 1000, "market_positions": [],
                                         "fills": []})

    venue.handler = endless  # type: ignore[method-assign]
    snap = _snapshot(venue)
    assert snap.completeness is AccountStateCompleteness.INCOMPLETE
    assert "orders" in snap.failures


# ======================================================== §28 restart recovery
def test_a_restart_re_reads_the_account_rather_than_trusting_the_ledger(
    venue: FakeKalshiDemo, tmp_path: Path
) -> None:
    path = tmp_path / "execution.db"
    venue.positions[(TICKER, "yes")] = 3
    first = _snapshot(venue, ExecutionLedger(path))
    with ExecutionLedger(path).connect() as con:
        ExecutionLedger(path).record_account_snapshot(con, first)

    # The account moves while we are "down".
    venue.positions[(TICKER, "yes")] = 7
    reborn = ExecutionLedger(path)
    second = _snapshot(venue, reborn)

    assert second.positions[0].quantity == 7
    assert {m for m, _ in venue.requests} == {"GET"}


def test_restart_does_not_invent_bot_ownership_for_external_state(
    venue: FakeKalshiDemo, tmp_path: Path
) -> None:
    path = tmp_path / "execution.db"
    venue.rest_order(
        {"client_order_id": "placed-by-hand", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 2, "yes_price": 30}
    )
    snap = _snapshot(venue, ExecutionLedger(path))
    order = snap.open_orders[0]
    assert snap.provenance[f"order:{order.exchange_order_id}"] == (
        ObjectProvenance.PREEXISTING_EXTERNAL.value
    )


# ========================================= §29 mode isolation, with real state
def test_a_production_read_snapshot_does_not_touch_demo_order_rows(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        before = ledger.get(con, coid)

    prod_profile = ModeProfile.of(ExecutionMode.PRODUCTION_READ_ONLY)
    prod_client = KalshiAccountClient(
        ExecutionMode.PRODUCTION_READ_ONLY,
        transport=SignedTransport(
            prod_profile,
            KalshiCredentials(
                key_id="p",
                environment=KalshiEnvironment.PRODUCTION,
                private_key_pem="x",
            ),
            client=httpx.AsyncClient(
                base_url=prod_profile.base_url,
                transport=httpx.MockTransport(venue.handler),
            ),
        ),
    )
    reader = ProductionReadOnlyAccountState(
        prod_client, ledger=ledger, mode=ExecutionMode.PRODUCTION_READ_ONLY
    )
    asyncio.run(reader.snapshot())

    with ledger.connect() as con:
        after = ledger.get(con, coid)
        assert ledger.all_orders(con, mode=ExecutionMode.PRODUCTION_READ_ONLY) == []
    assert before is not None and after is not None
    assert after.status is before.status


# =============================================== §32 kill switch interaction
def test_reads_continue_while_the_kill_switch_is_engaged(
    venue: FakeKalshiDemo,
) -> None:
    """An operator whose kill switch also blinded them would be worse off."""
    switch = KillSwitch()
    switch.engage("operator stop")
    snap = _snapshot(venue)
    assert snap.is_complete


def test_the_kill_switch_still_blocks_authorization_from_real_state(
    venue: FakeKalshiDemo,
) -> None:
    switch = KillSwitch()
    switch.engage("operator stop")
    exposure = _snapshot(venue).as_exposure()
    decision = RiskGate(kill_switch=switch).evaluate(_intent(), exposure)
    assert BlockReason.KILL_SWITCH in set(decision.reasons)


# ============================================ §37 independent risk arithmetic
def test_risk_exposure_matches_an_independent_recomputation(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    venue.positions[(TICKER, "yes")] = 3
    venue.rest_order(
        {"client_order_id": "manual", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 2, "yes_price": 25}
    )
    exposure = _snapshot(venue, ledger).as_exposure()

    # Recomputed by hand from the venue's own numbers: a resting buy risks
    # price x quantity.
    expected_resting = Decimal("0.25") * 2
    assert exposure.resting_exposure_usd(TICKER) == expected_resting
    assert len(exposure.positions) == 1
    assert exposure.positions[0].quantity == 3


# ============== findings from the Phase 5D adversarial review (all reproduced)
def _single_endpoint(venue: FakeKalshiDemo, path: str, response):
    """Serve one endpoint specially, everything else normally."""
    original = venue.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(path):
            return response() if callable(response) else response
        return original(request)

    venue.handler = handler  # type: ignore[method-assign]


@pytest.mark.parametrize(
    "label,response",
    [
        ("empty body", httpx.Response(200, content=b"")),
        ("null envelope", httpx.Response(200, json={"orders": None})),
        ("renamed key", httpx.Response(200, json={"order_list": []})),
        ("no envelope at all", httpx.Response(200, json={})),
        ("not a list", httpx.Response(200, json={"orders": {"a": 1}})),
    ],
)
def test_a_degenerate_200_is_incomplete_not_an_empty_account(
    venue: FakeKalshiDemo, label: str, response: httpx.Response
) -> None:
    """"The venue did not answer with a list" is not "the list is empty"."""
    _single_endpoint(venue, "/portfolio/orders", response)
    snap = _snapshot(venue)
    assert snap.completeness is AccountStateCompleteness.INCOMPLETE
    assert "orders" in snap.failures


def test_a_genuinely_empty_account_still_reads_complete(
    venue: FakeKalshiDemo,
) -> None:
    """The fix must not turn every empty account into an error."""
    snap = _snapshot(venue)
    assert snap.is_complete
    assert snap.open_orders == ()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 204])
def test_a_sub_400_reply_is_a_failure_not_an_empty_page(
    venue: FakeKalshiDemo, status: int
) -> None:
    """Success is 2xx. `>= 400` let an unfollowed redirect read as flat."""
    _single_endpoint(venue, "/portfolio/orders", httpx.Response(status))
    snap = _snapshot(venue)
    assert snap.completeness is AccountStateCompleteness.INCOMPLETE


def test_a_truncated_cursor_walk_is_incomplete(venue: FakeKalshiDemo) -> None:
    """A page the cursor promised, answered without the envelope, ends nothing."""
    calls = {"n": 0}

    def paged(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"orders": [], "cursor": "page-2"})
        return httpx.Response(200, json={})

    _single_endpoint(venue, "/portfolio/orders", paged)
    snap = _snapshot(venue)
    assert snap.completeness is AccountStateCompleteness.INCOMPLETE


def test_the_write_path_sees_a_pending_order(venue: FakeKalshiDemo) -> None:
    """`?status=resting` could never return an order the code calls open."""
    venue.rest_order(
        {"client_order_id": "m", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 40, "yes_price": 90}
    )
    next(iter(venue.orders.values()))["status"] = "pending"
    exposure = asyncio.run(_account(venue).get_exposure())
    assert exposure.resting_exposure_usd(TICKER) > 0
    assert exposure.complete is True


def test_a_reported_zero_exposure_does_not_beat_the_contract_count() -> None:
    """Partial information scored lower than none: 40 contracts charged $0."""
    from pmvl_markets.execution import Position

    pos = Position(
        ticker=TICKER, outcome=Outcome.YES, quantity=40, exposure_usd=Decimal("0")
    )
    exposure = ExposureSnapshot(positions=(pos,), fetched_at=utcnow())
    assert exposure.position_exposure_usd(TICKER) > 0


def test_a_stale_account_state_is_refused_at_the_point_of_use() -> None:
    """The gate checks it at issue; the write happens up to a TTL later."""
    from pmvl_markets.execution.authorization import AuthorizationExpired

    now = utcnow()
    exposure = _fresh_exposure(fetched_at=now - timedelta(seconds=119))
    _, plan = RiskGate(kill_switch=KillSwitch()).authorize(
        _intent(), exposure, client_order_id="c1"
    )
    assert plan is not None  # inside the cap at issue
    with pytest.raises(AuthorizationExpired):
        plan.assert_usable(mode=ExecutionMode.DEMO, now=now + timedelta(seconds=89))


def test_a_plan_with_no_account_state_time_is_refused_at_use() -> None:
    from pmvl_markets.execution.authorization import AuthorizationInvalid, issue_authorization
    from pmvl_markets.execution.models import TimeInForce

    plan = issue_authorization(
        mode=ExecutionMode.DEMO,
        ticker=TICKER,
        outcome=Outcome.YES,
        action=OrderAction.BUY,
        limit_price_cents=40,
        quantity=1,
        time_in_force=TimeInForce.GOOD_TILL_CANCELLED,
        client_order_id="c-none",
        intent_id="i",
        risk_checked_at=utcnow(),
        authorization_expires_at=utcnow() + timedelta(seconds=60),
        account_state_at=None,
    )
    with pytest.raises(AuthorizationInvalid):
        plan.assert_usable(mode=ExecutionMode.DEMO)


def test_a_blocked_order_is_not_evidence_that_we_placed_it(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """A BLOCKED row with no submitted_at proves the opposite of ownership."""
    intent = _intent()
    coid = derive_client_order_id(intent)
    with ledger.connect() as con:
        ledger.record_intent(con, intent, coid)
        ledger.transition(con, coid, OrderStatus.BLOCKED, note="risk gate")

    venue.rest_order(
        {"client_order_id": coid, "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 1, "yes_price": 40}
    )
    snap = _snapshot(venue, ledger)
    order = snap.open_orders[0]
    assert snap.provenance[f"order:{order.exchange_order_id}"] != (
        ObjectProvenance.BOT_MANAGED.value
    )
    assert len(snap.external_orders()) == 1


def test_a_reader_mode_must_match_its_client(venue: FakeKalshiDemo) -> None:
    """The mode scopes both the ownership query and where a snapshot is filed."""
    with pytest.raises(ValueError):
        ProductionReadOnlyAccountState(
            _account(venue), mode=ExecutionMode.PRODUCTION_READ_ONLY
        )


def test_a_venue_error_body_is_not_copied_into_the_summary_or_the_ledger(
    venue: FakeKalshiDemo, ledger: ExecutionLedger
) -> None:
    """Whatever the venue echoes would otherwise be stored forever."""
    canary = "member CANARY-ACCT-99 key ak_live_CANARY"
    _single_endpoint(
        venue, "/portfolio/orders", httpx.Response(500, json={"error": canary})
    )
    snap = _snapshot(venue, ledger)
    assert not snap.is_complete
    with ledger.connect() as con:
        ledger.record_account_snapshot(con, snap)
        rows = [dict(r) for r in ledger.account_snapshots(con, mode=ExecutionMode.DEMO)]
    assert "CANARY" not in str(snap.as_dict())
    assert "CANARY" not in str(rows)
    # The status code survives, because that is what an operator needs.
    assert "500" in str(snap.failures)


def test_a_terminal_status_with_working_quantity_is_flagged_not_dropped(
    venue: FakeKalshiDemo,
) -> None:
    """The venue contradicting itself is reconciliation, not 'nothing here'."""
    venue.rest_order(
        {"client_order_id": "m", "ticker": TICKER, "side": "yes",
         "action": "buy", "count": 40, "yes_price": 90}
    )
    order = next(iter(venue.orders.values()))
    order["status"] = "executed"
    order["remaining_count"] = 40
    order["taker_fill_count"] = 0

    snap = _snapshot(venue)
    assert len(snap.open_orders) == 1
    assert snap.open_orders[0].status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
    assert snap.as_exposure().resting_exposure_usd(TICKER) > 0
