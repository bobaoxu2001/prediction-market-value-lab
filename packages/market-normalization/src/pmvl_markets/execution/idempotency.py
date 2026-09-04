"""Stable execution identity, so a retry cannot become a second order.

The failure this prevents
------------------------
A create-order request times out. The client does not know whether the venue
received it. If the retry carries a fresh identifier, and the first request did
in fact land, the account now holds **two** positions where the strategy intended
one. Nothing downstream can detect this as an error: both orders are individually
valid.

So ``client_order_id`` is *derived*, not generated. The same logical trade
decision produces the same identifier forever, on any machine, before and after a
process restart. Retrying is therefore safe by construction: the venue's own
idempotency on ``client_order_id`` collapses the duplicate.

Two identifiers, deliberately
-----------------------------
* ``client_order_id`` - deterministic, the *logical* order. Retry-stable.
* ``attempt_id`` - random, one per HTTP attempt, for tracing only. Never sent as
  the idempotency key.

Changing your mind is a revision, not a retry
---------------------------------------------
If the strategy genuinely wants a *different* order for the same signal - a new
price, say - it increments ``revision``, which changes the derived id. That makes
"I am replacing my order" and "I am retrying my order" different acts with
different identifiers, which is the distinction that matters.
"""

from __future__ import annotations

import hashlib
import uuid

from .models import OrderIntent

__all__ = [
    "derive_client_order_id",
    "new_attempt_id",
    "IDEMPOTENCY_VERSION",
    "CLIENT_ORDER_ID_PREFIX",
]

#: Bumping this deliberately invalidates every previously derived id. It is part
#: of the hashed material so that a change to the derivation cannot silently
#: alias old orders onto new ones.
IDEMPOTENCY_VERSION = "v1"

CLIENT_ORDER_ID_PREFIX = "pmvl"

#: Kalshi accepts a bounded client-side identifier; keep well inside any limit.
_DIGEST_CHARS = 32


def derive_client_order_id(intent: OrderIntent) -> str:
    """Deterministic identifier for one logical order.

    Every field that changes *what order this is* participates. Notably included:

    * ``revision`` - so an amended decision is a different order;
    * ``limit_price_cents`` and ``quantity`` - so a retry that quietly changed
      size or price cannot reuse the original identity and be silently collapsed
      by the venue into the old order;
    * ``mode`` - so a demo order and a shadow order can never share an id.

    Deliberately excluded: wall-clock time, attempt counters, and anything else
    that differs between a first try and its retry.
    """
    material = "\x1f".join(
        (
            IDEMPOTENCY_VERSION,
            intent.mode.value,
            intent.strategy or "unspecified",
            intent.strategy_version or "0",
            intent.decision_epoch or "",
            intent.source_signal_id or intent.intent_id,
            intent.ticker,
            intent.outcome.value,
            intent.action.value,
            str(intent.limit_price_cents),
            str(intent.quantity),
            str(intent.revision),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{CLIENT_ORDER_ID_PREFIX}-{IDEMPOTENCY_VERSION}-{digest}"


def new_attempt_id() -> str:
    """Trace identifier for a single HTTP attempt. Never an idempotency key."""
    return uuid.uuid4().hex
