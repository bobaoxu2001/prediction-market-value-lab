"""A stateful fake of the Kalshi demo venue, for driving the real execution stack.

Everything above the socket is production code: `SignedTransport`,
`KalshiAccountClient`, `KalshiExecutionClient`, `ExecutionHarness`, the risk gate,
the authorization contract, the ledger and the reconciler. Only the network is
replaced.

That matters because the alternative — mocking the client — would test the mock.
Here a create really goes through gate 6, really gets signed headers stubbed at
the last moment, really lands in the ledger, and the order that comes back out of
`get_orders` is the one this venue actually holds.

It is a fake, not a simulator. It does not model queue position, latency or
matching; it models the *state machine* the reconciler has to survive: an order
that rests, fills in parts, fills between a poll and a cancel, disappears, or
answers with a 429.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

__all__ = ["FakeKalshiDemo"]


class FakeKalshiDemo:
    """Minimal Kalshi portfolio API with controllable behaviour."""

    def __init__(self, *, balance_cents: int = 100_000) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.fills: list[dict[str, Any]] = []
        self.positions: dict[tuple[str, str], int] = {}
        self.balance_cents = balance_cents

        #: Requests actually issued, so a test can assert "no second POST".
        self.requests: list[tuple[str, str]] = []
        #: Bodies of every create, to prove what reached the wire.
        self.created_bodies: list[dict[str, Any]] = []

        #: Behaviour switches.
        self.next_create_status: int | None = None
        self.next_create_raises: Exception | None = None
        self.fail_auth = False
        self.rate_limit_next = 0
        self._seq = 0

    # ------------------------------------------------------------------ venue
    def _order_id(self) -> str:
        self._seq += 1
        return f"demo-order-{self._seq}"

    def rest_order(self, body: dict[str, Any]) -> dict[str, Any]:
        order_id = self._order_id()
        order = {
            "order_id": order_id,
            "client_order_id": body.get("client_order_id"),
            "ticker": body.get("ticker"),
            "side": body.get("side"),
            "action": body.get("action"),
            "status": "resting",
            "initial_count": body.get("count"),
            "remaining_count": body.get("count"),
            "taker_fill_count": 0,
            "yes_price": body.get("yes_price"),
            "no_price": body.get("no_price"),
        }
        self.orders[order_id] = order
        return order

    def fill(self, order_id: str, quantity: int, price_cents: int) -> dict[str, Any]:
        """Fill part or all of a resting order, exactly as the venue would."""
        order = self.orders[order_id]
        remaining = int(order["remaining_count"])
        quantity = min(quantity, remaining)
        order["remaining_count"] = remaining - quantity
        order["taker_fill_count"] = int(order["taker_fill_count"]) + quantity
        order["status"] = "executed" if order["remaining_count"] == 0 else "resting"

        self._seq += 1
        record = {
            "trade_id": f"demo-fill-{self._seq}",
            "order_id": order_id,
            "ticker": order["ticker"],
            "side": order["side"],
            "action": order["action"],
            "count": quantity,
            "yes_price": price_cents if order["side"] == "yes" else None,
            "no_price": price_cents if order["side"] == "no" else None,
        }
        self.fills.append(record)
        key = (str(order["ticker"]), str(order["side"]))
        self.positions[key] = self.positions.get(key, 0) + quantity
        return record

    def vanish(self, order_id: str) -> None:
        """The venue stops listing the order, with no fill and no cancel record."""
        self.orders.pop(order_id, None)

    # --------------------------------------------------------------- handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.requests.append((method, path))

        if self.fail_auth:
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return httpx.Response(429, json={"error": "rate limited"})

        if method == "GET" and path.endswith("/portfolio/balance"):
            return httpx.Response(200, json={"balance": self.balance_cents})

        if method == "GET" and path.endswith("/portfolio/orders"):
            return httpx.Response(200, json={"orders": list(self.orders.values())})

        if method == "GET" and path.endswith("/portfolio/fills"):
            return httpx.Response(200, json={"fills": list(self.fills)})

        if method == "GET" and path.endswith("/portfolio/positions"):
            return httpx.Response(
                200,
                json={
                    "market_positions": [
                        {
                            "ticker": ticker,
                            # Kalshi signs this field: positive is long YES,
                            # negative is long NO. There is no `side` here, and
                            # inventing one would test a convention the real
                            # venue does not use.
                            "position": quantity if side == "yes" else -quantity,
                            "realized_pnl": 0,
                        }
                        for (ticker, side), quantity in self.positions.items()
                        if quantity
                    ]
                },
            )

        if method == "POST" and path.endswith("/portfolio/orders"):
            body = json.loads(request.content or b"{}")
            self.created_bodies.append(body)
            if self.next_create_raises is not None:
                exc = self.next_create_raises
                self.next_create_raises = None
                raise exc
            if self.next_create_status is not None:
                status = self.next_create_status
                self.next_create_status = None
                return httpx.Response(status, json={"error": "refused"})
            # The venue's own idempotency: a repeated client_order_id is a
            # conflict, not a second order.
            coid = body.get("client_order_id")
            for existing in self.orders.values():
                if existing.get("client_order_id") == coid:
                    return httpx.Response(409, json={"error": "duplicate"})
            return httpx.Response(201, json={"order": self.rest_order(body)})

        if method == "DELETE" and "/portfolio/orders/" in path:
            order_id = path.rsplit("/", 1)[-1]
            order = self.orders.get(order_id)
            if order is None:
                return httpx.Response(404, json={"error": "not found"})
            if int(order["remaining_count"]) == 0:
                # Already fully filled: there is nothing to cancel, and the venue
                # says so. This is the cancel/fill race.
                return httpx.Response(404, json={"error": "already resolved"})
            order["status"] = "canceled"
            order["remaining_count"] = 0
            return httpx.Response(200, json={"order": order})

        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    @property
    def create_count(self) -> int:
        return sum(1 for m, p in self.requests if m == "POST" and p.endswith("/orders"))
