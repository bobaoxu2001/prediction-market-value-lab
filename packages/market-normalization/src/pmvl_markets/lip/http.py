"""One HTTP client per recorder lane.

``HttpClient.last_request_window`` is a single attribute per client. Two lanes sharing
a client would stamp each other's socket windows onto their books - a skew bound
describing a different request - so every lane gets its own client, its own rate
limiter with no burst, and no cache. Book lanes make one attempt: a book that arrives
after its second has passed is not the book of that second, and a retry would
disguise a slow read as a fresh one.

Read-only by construction: this module adds no method beyond ``get_json``.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from pmvl_shared.config import get_settings

from ..providers.http import HttpClient, ProviderError, RateLimiter
from ..providers.kalshi import KalshiProvider

__all__ = ["LaneClient", "lane_provider"]


class LaneClient(HttpClient):
    def __init__(self, base_url: str, *, name: str, rate: float, attempts: int = 1) -> None:
        super().__init__(base_url, name=f"lip-{name}", rate_per_second=rate, cache_ttl_seconds=0.0)
        self._limiter = RateLimiter(rate, burst=1)
        self._max_retries = max(1, attempts)
        self.rate = rate
        #: requests started per UTC minute, for the lane-rate audit
        self.per_minute: Counter[int] = Counter()
        #: epoch seconds of every 429 this lane received - counted where the response
        #: arrives, so a 429 that a retry then got past is still counted
        self.rate_limited: list[float] = []
        self.failures: int = 0
        self._client.event_hooks.setdefault("response", []).append(self._on_response)

    async def _on_response(self, response: Any) -> None:
        if getattr(response, "status_code", None) == 429:
            self.rate_limited.append(time.time())

    def set_rate(self, rate: float) -> None:
        """The section 8 backoff: halve a lane's rate after a 429, restore it later."""
        self.rate = rate
        self._limiter = RateLimiter(rate, burst=1)

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        self.per_minute[int(time.time() // 60)] += 1
        try:
            return await super().get_json(path, **kwargs)
        except ProviderError:
            self.failures += 1
            raise


def lane_provider(name: str, rate: float, *, attempts: int = 1) -> KalshiProvider:
    base = get_settings().kalshi_api_base
    return KalshiProvider(client=LaneClient(base, name=name, rate=rate, attempts=attempts))
