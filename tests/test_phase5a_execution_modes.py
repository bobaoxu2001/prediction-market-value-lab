"""Phase 5A: mode isolation and the production-write block.

These are the tests that carry the safety claim. Each one attempts to reach a
real-money mutation by a different route and asserts that it is refused
*locally* - before any request is constructed, let alone sent.

The suite's autouse ``_block_outbound_network`` fixture means a test that
accidentally did reach a venue would fail on the socket rather than trade. That
is a backstop, not the assertion: every test here proves the refusal happens
above the socket.
"""

from __future__ import annotations

import httpx
import pytest

from pmvl_markets.execution import (
    ExecutionMode,
    KalshiAccountClient,
    KalshiExecutionClient,
    KalshiEnvironment,
    LiveTradingNotAuthorized,
    ModeProfile,
    assert_transport_write_allowed,
)
from pmvl_markets.execution.credentials import KalshiCredentials
from pmvl_markets.execution.modes import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    WRITE_METHODS,
    base_url_for,
    environment_for,
    is_production_url,
)
from pmvl_markets.execution.transport import SignedTransport


def _creds(env: KalshiEnvironment) -> KalshiCredentials:
    return KalshiCredentials(
        key_id="test-key-id", environment=env, private_key_pem="not-a-real-key"
    )


# --------------------------------------------------------------------- mode set
def test_no_live_mode_exists() -> None:
    """The strongest guarantee: there is no value that means 'real money'."""
    names = {m.name for m in ExecutionMode}
    assert names == {"SHADOW", "DEMO", "PRODUCTION_READ_ONLY"}
    assert not any("LIVE" in n for n in names)


def test_only_demo_may_write() -> None:
    assert ExecutionMode.DEMO.may_write is True
    assert ExecutionMode.SHADOW.may_write is False
    assert ExecutionMode.PRODUCTION_READ_ONLY.may_write is False


def test_module_constant_states_the_answer() -> None:
    from pmvl_markets.execution import LIVE_AUTONOMOUS_TRADING_AUTHORIZED

    assert LIVE_AUTONOMOUS_TRADING_AUTHORIZED is False


# ------------------------------------------------------------- URL derivation
def test_mode_derives_its_own_url_so_it_cannot_be_mismatched() -> None:
    assert base_url_for(ExecutionMode.DEMO) == DEMO_BASE_URL
    assert base_url_for(ExecutionMode.SHADOW) == DEMO_BASE_URL
    assert base_url_for(ExecutionMode.PRODUCTION_READ_ONLY) == PRODUCTION_BASE_URL


def test_environment_mapping() -> None:
    assert environment_for(ExecutionMode.DEMO) is KalshiEnvironment.DEMO
    assert environment_for(ExecutionMode.SHADOW) is KalshiEnvironment.DEMO
    assert (
        environment_for(ExecutionMode.PRODUCTION_READ_ONLY)
        is KalshiEnvironment.PRODUCTION
    )


def test_demo_url_is_not_production() -> None:
    assert is_production_url(DEMO_BASE_URL) is False
    assert is_production_url(PRODUCTION_BASE_URL) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "https://evil.example.com/trade-api/v2",
        "https://demo.kalshi.co.attacker.example/trade-api/v2",
        "https://api.elections.kalshi.com/trade-api/v2",
    ],
)
def test_unknown_hosts_fail_closed_to_production(url: str) -> None:
    """Anything not provably a sandbox counts as real money."""
    assert is_production_url(url) is True


# ------------------------------------------------------- gate 6: the transport
@pytest.mark.parametrize("method", sorted(WRITE_METHODS))
def test_transport_guard_refuses_every_write_verb_to_production(method: str) -> None:
    with pytest.raises(LiveTradingNotAuthorized):
        assert_transport_write_allowed(method, PRODUCTION_BASE_URL + "/portfolio/orders")


@pytest.mark.parametrize("method", ["GET", "get"])
def test_transport_guard_allows_production_reads(method: str) -> None:
    assert_transport_write_allowed(method, PRODUCTION_BASE_URL + "/portfolio/balance")


@pytest.mark.parametrize("method", sorted(WRITE_METHODS))
def test_transport_guard_allows_demo_writes(method: str) -> None:
    assert_transport_write_allowed(method, DEMO_BASE_URL + "/portfolio/orders")


def test_transport_guard_takes_no_override_argument() -> None:
    """There must be no parameter that turns the last gate off."""
    import inspect

    params = inspect.signature(assert_transport_write_allowed).parameters
    assert list(params) == ["method", "url"]


def test_guard_error_message_does_not_leak_query_string() -> None:
    with pytest.raises(LiveTradingNotAuthorized) as exc:
        assert_transport_write_allowed(
            "POST", PRODUCTION_BASE_URL + "/portfolio/orders?token=secret123"
        )
    assert "secret123" not in str(exc.value)


# ------------------------------------------------------ gate 4: client refusal
def test_execution_client_cannot_be_constructed_in_production_read_only() -> None:
    with pytest.raises(LiveTradingNotAuthorized):
        KalshiExecutionClient(ExecutionMode.PRODUCTION_READ_ONLY)


def test_execution_client_cannot_be_constructed_in_shadow() -> None:
    with pytest.raises(LiveTradingNotAuthorized):
        KalshiExecutionClient(ExecutionMode.SHADOW)


def test_account_client_has_no_mutating_method() -> None:
    """PRODUCTION_READ_ONLY is a type-level property, not a runtime flag."""
    forbidden = {"create_order", "cancel_order", "amend_order", "batch_create_orders"}
    assert forbidden.isdisjoint(dir(KalshiAccountClient))


def test_account_client_refuses_shadow_mode() -> None:
    with pytest.raises(LiveTradingNotAuthorized):
        KalshiAccountClient(ExecutionMode.SHADOW)


# ------------------------------------------- adversarial: bypass the layers
async def test_direct_transport_post_in_production_mode_is_refused() -> None:
    """Skip the client entirely and drive the transport. Still refused."""
    profile = ModeProfile.of(ExecutionMode.PRODUCTION_READ_ONLY)
    transport = SignedTransport(
        profile,
        _creds(KalshiEnvironment.PRODUCTION),
        client=httpx.AsyncClient(
            base_url=profile.base_url,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        ),
    )
    with pytest.raises(LiveTradingNotAuthorized):
        await transport.post("/portfolio/orders", json={"ticker": "X"})
    await transport.aclose()


async def test_direct_transport_delete_in_production_mode_is_refused() -> None:
    profile = ModeProfile.of(ExecutionMode.PRODUCTION_READ_ONLY)
    transport = SignedTransport(
        profile,
        _creds(KalshiEnvironment.PRODUCTION),
        client=httpx.AsyncClient(
            base_url=profile.base_url,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        ),
    )
    with pytest.raises(LiveTradingNotAuthorized):
        await transport.delete("/portfolio/orders/abc123")
    await transport.aclose()


async def test_no_http_request_is_made_when_a_production_write_is_refused() -> None:
    """The refusal happens above the socket, not at it."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    profile = ModeProfile.of(ExecutionMode.PRODUCTION_READ_ONLY)
    transport = SignedTransport(
        profile,
        _creds(KalshiEnvironment.PRODUCTION),
        client=httpx.AsyncClient(
            base_url=profile.base_url, transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(LiveTradingNotAuthorized):
        await transport.post("/portfolio/orders", json={})
    assert seen == []
    await transport.aclose()


def test_credentials_cannot_cross_environments() -> None:
    """Demo credentials must not authenticate a production transport."""
    profile = ModeProfile.of(ExecutionMode.PRODUCTION_READ_ONLY)
    from pmvl_markets.execution.credentials import CredentialsUnavailable

    with pytest.raises(CredentialsUnavailable):
        SignedTransport(profile, _creds(KalshiEnvironment.DEMO))


def test_production_credentials_cannot_be_used_against_demo() -> None:
    profile = ModeProfile.of(ExecutionMode.DEMO)
    from pmvl_markets.execution.credentials import CredentialsUnavailable

    with pytest.raises(CredentialsUnavailable):
        SignedTransport(profile, _creds(KalshiEnvironment.PRODUCTION))


def test_mode_profile_is_frozen_so_it_cannot_be_retargeted() -> None:
    import dataclasses

    profile = ModeProfile.of(ExecutionMode.DEMO)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.base_url = PRODUCTION_BASE_URL  # type: ignore[misc]


def test_profile_assert_may_write_refuses_read_only_modes() -> None:
    for mode in (ExecutionMode.SHADOW, ExecutionMode.PRODUCTION_READ_ONLY):
        with pytest.raises(LiveTradingNotAuthorized):
            ModeProfile.of(mode).assert_may_write("create_order")


async def test_demo_transport_targets_only_the_demo_host() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"order": {}})

    profile = ModeProfile.of(ExecutionMode.DEMO)
    transport = SignedTransport(
        profile,
        _creds(KalshiEnvironment.DEMO),
        client=httpx.AsyncClient(
            base_url=profile.base_url, transport=httpx.MockTransport(handler)
        ),
    )
    import pmvl_markets.execution.credentials as creds_mod

    original = creds_mod.sign_payload
    creds_mod.sign_payload = lambda c, p: "sig"  # type: ignore[assignment]
    try:
        await transport.post("/portfolio/orders", json={"ticker": "T"})
    finally:
        creds_mod.sign_payload = original  # type: ignore[assignment]
    await transport.aclose()

    assert len(seen) == 1
    assert "demo.kalshi.co" in seen[0]
    assert "elections.kalshi.com" not in seen[0]


def test_transport_does_not_follow_redirects() -> None:
    """A redirect could carry a demo write onto a production host."""
    profile = ModeProfile.of(ExecutionMode.DEMO)
    transport = SignedTransport(profile, _creds(KalshiEnvironment.DEMO))
    assert transport._client.follow_redirects is False


def test_public_http_client_still_cannot_write() -> None:
    """The research data path must stay structurally incapable of mutation."""
    from pmvl_markets.providers.http import HttpClient

    assert not {"post", "put", "patch", "delete"} & set(dir(HttpClient))
