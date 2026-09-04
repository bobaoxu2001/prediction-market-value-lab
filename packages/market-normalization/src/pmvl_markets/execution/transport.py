"""Signed HTTP transport for authenticated Kalshi calls.

Separate from :class:`pmvl_markets.providers.http.HttpClient` for two reasons
that both matter.

**That client caches GETs.** A cached balance or a cached open-order list is a
correctness bug in an execution system: risk decisions would be taken against
stale account state. This transport never caches.

**That client cannot write.** ``HttpClient`` exposes ``get_json`` and nothing
else, which is a useful property to preserve - the public data path stays
structurally incapable of mutating anything. Adding POST there would remove that.

Gate 6 lives here
-----------------
:func:`assert_transport_write_allowed` is called on **every** request, before the
URL is built and before any socket is opened. It takes only the verb and the
target, so no amount of misuse in the layers above can talk it out of refusing a
production mutation. There is no parameter that disables it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import get_logger

from ..providers.http import CircuitBreaker, ProviderError, RateLimiter
from .credentials import KalshiCredentials, redact_headers, signing_headers
from .modes import (
    LiveTradingNotAuthorized,
    ModeProfile,
    _safe_target,
    assert_transport_write_allowed,
    is_production_url,
)

log = get_logger(__name__)

__all__ = ["SignedTransport", "ExecutionApiError"]


class ExecutionApiError(ProviderError):
    """An authenticated Kalshi call failed.

    ``status_code`` is preserved because the caller's reaction depends on it: a
    409 on create-order is a duplicate ``client_order_id`` (benign, reconcile),
    while a 401 is an auth failure that must trip the circuit breaker.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SignedTransport:
    """Minimal authenticated JSON transport. No cache, no implicit retries on writes."""

    def __init__(
        self,
        profile: ModeProfile,
        credentials: KalshiCredentials,
        *,
        rate_per_second: float = 4.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Gate 3: the credentials must belong to this mode's environment.
        credentials.assert_environment(profile.environment)

        settings = get_settings()
        self._profile = profile
        self._credentials = credentials
        self._limiter = RateLimiter(rate_per_second)
        self._breaker = CircuitBreaker(
            threshold=settings.circuit_breaker_threshold,
            reset_seconds=settings.circuit_breaker_reset_seconds,
            name=f"kalshi-{profile.environment.value}",
        )
        # A caller-supplied client decides the real destination, so it has to
        # agree with the profile before it is accepted. Without this check the
        # transport validates one URL and sends another: an adversarial review
        # delivered a DEMO-labelled order POST to the production host with every
        # other gate reporting success.
        if client is not None:
            supplied = str(client.base_url or "").rstrip("/")
            expected = profile.base_url.rstrip("/")
            if supplied != expected:
                raise LiveTradingNotAuthorized(
                    "refusing a caller-supplied HTTP client whose base URL "
                    f"({_safe_target(supplied)}) is not this mode's "
                    f"({_safe_target(expected)})"
                )
            if getattr(client, "follow_redirects", False):
                raise LiveTradingNotAuthorized(
                    "refusing a caller-supplied HTTP client with redirects "
                    "enabled: a redirect can cross environments"
                )

        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=profile.base_url,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": settings.http_user_agent,
            },
            follow_redirects=False,  # a redirect could cross environments
        )
        self.stats: dict[str, Any] = {"requests": 0, "errors": 0, "writes": 0}

    @property
    def profile(self) -> ModeProfile:
        return self._profile

    @property
    def circuit_open(self) -> bool:
        return self._breaker.is_open

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "SignedTransport":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ verbs
    async def get(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, *, json: Mapping[str, Any]) -> Any:
        return await self._request("POST", path, json=json)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ---------------------------------------------------------------- internals
    def _full_path(self, path: str) -> str:
        """Absolute request path, which is also what gets signed.

        Kalshi signs the whole path including the ``/trade-api/v2`` prefix, so it
        is taken from the base URL rather than assumed.
        """
        base_path = httpx.URL(self._profile.base_url).path.rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{base_path}{suffix}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        full_path = self._full_path(path)

        # Ask httpx where this request is *actually* going rather than rebuilding
        # the URL by hand. A hand-built string describes the request we intended;
        # `build_request` resolves the one that will be sent - which is the only
        # thing worth guarding. It also closes the absolute-`path` escape, since
        # an absolute URL overrides base_url here exactly as it would on the wire.
        request = self._client.build_request(
            method,
            path,
            params=dict(params or {}) or None,
            json=dict(json) if json is not None else None,
        )
        target = str(request.url)

        # ---- Gate 6. Before the socket, before anything. --------------------
        assert_transport_write_allowed(method, target)

        # ---- Gate 4b: a writable mode must never be pointed at production. ---
        if method.upper() != "GET" and is_production_url(target):
            raise LiveTradingNotAuthorized(
                f"refusing {method} against production base URL in mode "
                f"{self._profile.mode.value}"
            )

        self._breaker.check()
        await self._limiter.acquire()

        headers = dict(
            signing_headers(self._credentials, method, full_path)
        )

        self.stats["requests"] += 1
        if method.upper() != "GET":
            self.stats["writes"] += 1

        try:
            request.headers.update(headers)
            response = await self._client.send(request)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self.stats["errors"] += 1
            self._breaker.record_failure()
            raise ExecutionApiError(
                f"{method} {path} transport failure: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            self.stats["errors"] += 1
            # 4xx other than 429 are our fault and must not trip the breaker on
            # their own; 5xx and 429 are the venue's and must.
            if response.status_code >= 500 or response.status_code == 429:
                self._breaker.record_failure()
            elif response.status_code in (401, 403):
                # Auth failure is a hard stop: every subsequent call would fail
                # the same way, and hammering it looks like an attack.
                self._breaker.record_failure()
            log.warning(
                "kalshi %s %s -> %s (env=%s headers=%s)",
                method,
                _safe_path(path),
                response.status_code,
                self._profile.environment.value,
                redact_headers(headers),
            )
            raise ExecutionApiError(
                f"{method} {_safe_path(path)} failed with "
                f"{response.status_code}: {_safe_body(response)}",
                status_code=response.status_code,
            )

        self._breaker.record_success()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ExecutionApiError(
                f"{method} {_safe_path(path)} returned non-JSON body"
            ) from exc


def _safe_path(path: str) -> str:
    return path.split("?", 1)[0]


def _safe_body(response: httpx.Response) -> str:
    """A bounded slice of an error body, for diagnosis without dumping payloads."""
    try:
        return response.text[:200]
    except Exception:  # pragma: no cover - defensive
        return "<unreadable>"
