"""Shared async HTTP client: retries, backoff, rate limiting, circuit breaker, cache.

Both venues rate-limit aggressively and occasionally return 5xx during settlement
windows. A scan that dies halfway leaves a partially-updated view of the book, which
is worse than no scan, so every request goes through this layer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from pmvl_shared.config import get_settings
from pmvl_shared.logging_setup import get_logger

log = get_logger(__name__)


class ProviderError(RuntimeError):
    """Non-retryable provider failure (4xx other than 429, or bad payload)."""


class RetryableProviderError(RuntimeError):
    """Transient failure: 429, 5xx, timeout, connection reset."""


class CircuitOpenError(ProviderError):
    """The breaker is open; the provider is being given time to recover."""


@dataclass
class CircuitBreaker:
    """Trips after N consecutive failures, half-opens after a cooldown.

    Without this, a venue outage turns one failed scan into thousands of timed-out
    requests, each burning the full retry budget.
    """

    threshold: int
    reset_seconds: int
    name: str = "provider"
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def check(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at >= self.reset_seconds:
            log.warning("circuit half-open for %s, allowing a probe request", self.name)
            self._opened_at = None
            self._failures = self.threshold - 1
            return
        raise CircuitOpenError(
            f"circuit open for {self.name}; retry in "
            f"{self.reset_seconds - (time.monotonic() - self._opened_at):.0f}s"
        )

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            log.error("circuit OPEN for %s after %d failures", self.name, self._failures)

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


class RateLimiter:
    """Simple async token bucket, sized per provider tier."""

    def __init__(self, rate_per_second: float, burst: int | None = None) -> None:
        self.rate = max(rate_per_second, 0.1)
        self.capacity = burst if burst is not None else max(int(self.rate * 2), 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self.rate)


class HttpClient:
    """Async JSON client with per-provider limiting, retry and a short GET cache."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str,
        rate_per_second: float = 8.0,
        headers: Mapping[str, str] | None = None,
        cache_ttl_seconds: float = 0.0,
        identify_self: bool = True,
    ) -> None:
        settings = get_settings()
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(rate_per_second)
        self._breaker = CircuitBreaker(
            threshold=settings.circuit_breaker_threshold,
            reset_seconds=settings.circuit_breaker_reset_seconds,
            name=name,
        )
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self._semaphore = asyncio.Semaphore(settings.http_max_concurrency)
        self._max_retries = settings.http_max_retries
        # `identify_self` exists for one host. NWS *requires* a self-identifying
        # User-Agent, so it is the default. ESPN's edge does the opposite: it 403s
        # every User-Agent outside a short allowlist of well-known client tokens,
        # including this project's own descriptive string.
        #
        # Passing False leaves httpx to send its own `python-httpx/<version>`,
        # which is a true statement about what is making the request - this client
        # *is* httpx. Deliberately not a browser string: claiming to be Chrome to
        # get past a filter is a different act from declining to add a custom
        # header, and only the second one is honest.
        base_headers: dict[str, str] = {"Accept": "application/json"}
        if identify_self:
            base_headers["User-Agent"] = settings.http_user_agent
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={**base_headers, **(headers or {})},
            follow_redirects=True,
        )
        #: Request counters surfaced on /system for observability.
        self.stats = {"requests": 0, "errors": 0, "retries": 0, "cache_hits": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    @property
    def circuit_open(self) -> bool:
        return self._breaker.is_open

    def _cache_key(self, path: str, params: Mapping[str, Any] | None) -> str:
        return f"{path}?{sorted((params or {}).items())}"

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        cost: float = 1.0,
        use_cache: bool = True,
        allow_404: bool = False,
    ) -> Any:
        """GET returning parsed JSON.

        ``allow_404`` returns ``None`` instead of raising - used for endpoints that
        legitimately 404 (a token with no book yet, a market with no resolution).
        """
        key = self._cache_key(path, params)
        if use_cache and self._cache_ttl > 0:
            hit = self._cache.get(key)
            if hit and (time.monotonic() - hit[0]) < self._cache_ttl:
                self.stats["cache_hits"] += 1
                return hit[1]

        self._breaker.check()
        attempt_counter = {"n": 0}

        async def _once() -> Any:
            attempt_counter["n"] += 1
            if attempt_counter["n"] > 1:
                self.stats["retries"] += 1
            await self._limiter.acquire(cost)
            async with self._semaphore:
                self.stats["requests"] += 1
                try:
                    resp = await self._client.get(path, params=dict(params or {}))
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise RetryableProviderError(f"{self.name} transport: {exc}") from exc

            if resp.status_code == 429:
                # Honour Retry-After when the venue supplies it.
                delay = _retry_after_seconds(resp)
                if delay:
                    await asyncio.sleep(min(delay, 30))
                raise RetryableProviderError(f"{self.name} rate limited (429)")
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code >= 500:
                raise RetryableProviderError(f"{self.name} {resp.status_code} on {path}")
            if resp.status_code >= 400:
                raise ProviderError(f"{self.name} {resp.status_code} on {path}: {resp.text[:200]}")
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise ProviderError(f"{self.name} returned non-JSON for {path}") from exc

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retries),
                wait=wait_exponential_jitter(initial=0.5, max=12.0),
                retry=retry_if_exception_type(RetryableProviderError),
                reraise=True,
            ):
                with attempt:
                    data = await _once()
        except (RetryError, RetryableProviderError, ProviderError) as exc:
            self.stats["errors"] += 1
            self._breaker.record_failure()
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"{self.name} exhausted retries: {exc}") from exc

        self._breaker.record_success()
        if use_cache and self._cache_ttl > 0:
            self._cache[key] = (time.monotonic(), data)
        return data


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
