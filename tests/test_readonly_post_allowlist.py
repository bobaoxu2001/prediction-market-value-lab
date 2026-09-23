"""The market-data client may POST to exactly one kind of place, and no other.

"This client cannot POST" used to be a structural guarantee that it could not place
an order. Polymarket's batch orderbook read needs a POST - the token ids go in the
body - so the guarantee had to change shape rather than disappear: it may POST to an
allowlist, which is checkable, and adding to that list is a visible edit rather than
a call site nobody reviews.
"""

from __future__ import annotations

import pytest

from pmvl_markets.providers.http import (
    READ_ONLY_POST_PATHS,
    HttpClient,
    NotAReadOnlyEndpoint,
)


@pytest.fixture
def client():
    return HttpClient("https://clob.polymarket.com", name="test")


@pytest.mark.parametrize(
    "path",
    [
        "/portfolio/orders",
        "/portfolio/orders/batched",
        "/order",
        "/orders",
        "/books/../portfolio/orders",
        "/BOOKS",
        "books",
        "",
        "/",
    ],
)
@pytest.mark.asyncio
async def test_every_path_that_is_not_the_allowlisted_one_is_refused(client, path):
    """Including the ones that only look like it. Matching is exact, not fuzzy."""
    try:
        with pytest.raises(NotAReadOnlyEndpoint):
            await client.post_json_readonly(path, [{"token_id": "x"}])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_the_refusal_happens_before_anything_is_sent(client, monkeypatch):
    """A guard that fires after the request has left is not a guard."""
    sent: list[str] = []

    async def _boom(*args, **kwargs):
        sent.append("post")
        raise AssertionError("a request was sent for a non-allowlisted path")

    monkeypatch.setattr(client._client, "post", _boom)
    try:
        with pytest.raises(NotAReadOnlyEndpoint):
            await client.post_json_readonly("/portfolio/orders", [])
        assert sent == []
    finally:
        await client.aclose()


def test_the_allowlist_holds_only_reads():
    """A one-line audit. If this list ever grows an order path, the test says so."""
    assert READ_ONLY_POST_PATHS == frozenset({"/books"})
    forbidden = ("order", "position", "portfolio", "cancel", "transfer", "withdraw")
    for path in READ_ONLY_POST_PATHS:
        assert not any(word in path.lower() for word in forbidden), path


@pytest.mark.asyncio
async def test_the_client_still_has_no_general_post(client):
    """The only POST door is the allowlisted one; there is no unguarded sibling."""
    try:
        assert not hasattr(client, "post_json")
        assert not hasattr(client, "post")
        assert hasattr(client, "post_json_readonly")
    finally:
        await client.aclose()
