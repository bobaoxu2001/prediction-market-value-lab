"""The only object a mutation-facing gateway will accept.

Phase 5A shipped a risk gate that returned a *verdict*, and a write client that
took an ``OrderIntent``. Those two facts did not touch: an adversarial review
pointed out that ``KalshiExecutionClient.create_order`` was public, took raw
caller values, and consulted nothing. The risk gate was documented as "the only
path from an OrderIntent to a venue mutation" and was in fact a convention.

This module makes it a type.

    OrderIntent -> RiskGate.authorize(...) -> AuthorizedExecutionPlan
                                           -> ExecutionGateway.submit(plan)

The gateway accepts *only* a plan. There is no overload taking a ticker, a
price, or an intent, so "call the client directly" is not a shortcut a future
strategy module can take by accident - the code does not compile into a
meaningful call.

Three properties make the plan worth trusting.

**It binds semantics, not permission.** The plan carries the exact order that was
approved - ticker, outcome, action, price, quantity, time-in-force, mode,
idempotency key. Submitting is not "you may trade", it is "you may place *this*".

**It is sealed.** ``frozen=True`` stops honest mutation; it does not stop
``object.__setattr__``. Every plan therefore carries a digest over its own bound
fields, keyed by a secret generated per process, and the gateway recomputes it
before acting. Editing a field after approval invalidates the seal, so the
tamper is detected rather than trusted-because-frozen.

**It expires.** Risk approval is a statement about a moment: an exposure
snapshot, a quote, a signal age, a kill-switch position. Approval at 10:00 says
nothing about 11:00, so a plan carries ``authorization_expires_at`` and the
gateway refuses a stale one rather than re-using it.

The seal secret lives only in memory and is regenerated on import, which means a
plan cannot survive a process restart. That is deliberate: an authorization that
outlives the account state it was computed against is exactly the thing this
module exists to prevent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pmvl_shared.timeutil import utcnow

from .models import ExecutionMode, OrderAction, Outcome, TimeInForce
from .modes import ExecutionModeError

__all__ = [
    "AuthorizedExecutionPlan",
    "AuthorizationInvalid",
    "AuthorizationExpired",
    "AuthorizationTampered",
    "AuthorizationNotIssued",
    "AuthorizationAlreadyUsed",
    "RISK_POLICY_VERSION",
    "DEFAULT_AUTHORIZATION_TTL",
    "MAX_AUTHORIZATION_TTL",
    "issue_authorization",
]

#: Bumped whenever the risk policy changes in a way that should invalidate
#: approvals computed under the old one. Part of the sealed material.
RISK_POLICY_VERSION = "5b.1"

#: Short by design. An approval is a claim about current account state and a
#: current quote; neither stays true for long.
DEFAULT_AUTHORIZATION_TTL = timedelta(seconds=90)

#: Hard ceiling. A caller may ask for less, never more.
MAX_AUTHORIZATION_TTL = timedelta(minutes=5)

#: Per-process, never persisted, never logged. A plan from another process (or
#: from before a restart) cannot verify, which is the intended behaviour.
_SEAL_KEY = secrets.token_bytes(32)

#: Nonces this process's risk gate has actually issued, mapped to whether they
#: have been spent.
#:
#: The seal alone was not enough. An HMAC keyed by a module global proves only
#: "built in this process" - and since ``seal()`` was public, any caller could
#: build one. That is the same defect as a public ``create_order``, moved up a
#: layer: a forged plan for a million contracts at 99c verified perfectly.
#:
#: Membership here is what distinguishes "the risk gate approved this" from
#: "somebody constructed this", because only :func:`issue_authorization` - which
#: the risk gate calls after a passing evaluation - ever adds to it. It also
#: makes an authorization single-use: replaying a spent plan is refused rather
#: than relying on the venue's own dedupe to mask it.
_ISSUED: dict[str, bool] = {}

#: Bound so a long-running process cannot accumulate authorizations forever.
_MAX_TRACKED = 4096


class AuthorizationInvalid(ExecutionModeError):
    """A plan was rejected at the point of use."""


class AuthorizationExpired(AuthorizationInvalid):
    """The approval was real but is no longer current."""


class AuthorizationTampered(AuthorizationInvalid):
    """The plan's bound semantics do not match its seal."""


class AuthorizationNotIssued(AuthorizationInvalid):
    """The plan verifies but no risk gate in this process ever issued it."""


class AuthorizationAlreadyUsed(AuthorizationInvalid):
    """The plan was already spent. Authorizations are single-use."""


@dataclass(frozen=True, slots=True)
class AuthorizedExecutionPlan:
    """One approved order. Immutable, sealed, and short-lived.

    Construct via :meth:`seal`, never by hand - a directly constructed instance
    carries no valid seal and every gateway will refuse it.
    """

    mode: ExecutionMode
    ticker: str
    outcome: Outcome
    action: OrderAction
    limit_price_cents: int
    quantity: int
    time_in_force: TimeInForce
    client_order_id: str

    intent_id: str
    risk_checked_at: datetime
    authorization_expires_at: datetime
    risk_policy_version: str = RISK_POLICY_VERSION

    #: Provenance of the state the approval was computed against. Carried so the
    #: point-of-use check can re-assert freshness rather than trust the caller.
    signal_timestamp: datetime | None = None
    market_data_timestamp: datetime | None = None
    account_state_at: datetime | None = None

    #: Random per-issuance. Not secret - its value is that only the risk gate
    #: can put it in the issued registry.
    nonce: str = ""
    seal_digest: str = ""

    # ------------------------------------------------------------------ sealing
    def _material(self) -> str:
        """Exactly the fields whose change would alter what reaches the venue."""
        return "\x1f".join(
            (
                self.mode.value,
                self.ticker,
                self.outcome.value,
                self.action.value,
                str(self.limit_price_cents),
                str(self.quantity),
                self.time_in_force.value,
                self.client_order_id,
                self.intent_id,
                _iso(self.risk_checked_at),
                _iso(self.authorization_expires_at),
                self.risk_policy_version,
                self.nonce,
                # Provenance is sealed too. It cannot change what reaches the
                # venue, but it is the plan's only record of which signal, quote
                # and account snapshot the approval was computed against, and a
                # rewritable audit trail is not an audit trail.
                _iso(self.signal_timestamp),
                _iso(self.market_data_timestamp),
                _iso(self.account_state_at),
            )
        )

    def compute_seal(self) -> str:
        return hmac.new(
            _SEAL_KEY, self._material().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @classmethod
    def _seal(cls, **fields: Any) -> "AuthorizedExecutionPlan":
        """Stamp a plan. Private: sealing alone does not authorize anything.

        Use :func:`issue_authorization`, which is what the risk gate calls and
        what records the plan as genuinely issued.
        """
        fields.pop("seal_digest", None)
        draft = cls(**fields, seal_digest="")
        return cls(**fields, seal_digest=draft.compute_seal())

    # -------------------------------------------------------------- validation
    def verify_issued(self) -> None:
        """The risk gate must have issued this, and it must be unspent."""
        spent = _ISSUED.get(self.nonce)
        if spent is None:
            raise AuthorizationNotIssued(
                "execution plan was not issued by a risk gate in this process; "
                "sealing a plan does not authorize it"
            )
        if spent:
            raise AuthorizationAlreadyUsed(
                f"authorization {self.client_order_id} has already been used; "
                "reauthorization is required for another attempt"
            )

    def consume(self) -> None:
        """Spend this authorization. Called at the single point of mutation."""
        self.verify_issued()
        _ISSUED[self.nonce] = True

    def verify_seal(self) -> None:
        if not self.seal_digest:
            raise AuthorizationTampered(
                "execution plan carries no seal; it was constructed directly "
                "rather than issued by the risk gate"
            )
        if not hmac.compare_digest(self.seal_digest, self.compute_seal()):
            raise AuthorizationTampered(
                "execution plan was modified after approval: its bound order "
                "semantics do not match the risk gate's seal"
            )

    def assert_current(self, *, now: datetime | None = None) -> None:
        now = now or utcnow()
        if now >= self.authorization_expires_at:
            raise AuthorizationExpired(
                f"risk authorization for {self.client_order_id} expired at "
                f"{_iso(self.authorization_expires_at)}; reauthorization required"
            )

    def assert_usable(
        self,
        *,
        mode: ExecutionMode,
        now: datetime | None = None,
        consume: bool = False,
    ) -> None:
        """Full point-of-use check. Every gateway calls this before acting.

        ``consume`` is set by the one layer that actually performs the mutation,
        so an authorization cannot be replayed into two orders.
        """
        self.verify_seal()
        self.verify_issued()
        self.assert_current(now=now)
        if self.mode is not mode:
            raise AuthorizationInvalid(
                f"plan authorized for mode {self.mode.value} presented to a "
                f"{mode.value} gateway"
            )
        if self.risk_policy_version != RISK_POLICY_VERSION:
            raise AuthorizationInvalid(
                f"plan was approved under risk policy "
                f"{self.risk_policy_version}, current policy is "
                f"{RISK_POLICY_VERSION}; reauthorization required"
            )
        if consume:
            self.consume()

    def as_dict(self) -> dict[str, Any]:
        """Loggable form. The seal is deliberately not included."""
        return {
            "mode": self.mode.value,
            "ticker": self.ticker,
            "outcome": self.outcome.value,
            "action": self.action.value,
            "limit_price_cents": self.limit_price_cents,
            "quantity": self.quantity,
            "time_in_force": self.time_in_force.value,
            "client_order_id": self.client_order_id,
            "intent_id": self.intent_id,
            "risk_checked_at": _iso(self.risk_checked_at),
            "authorization_expires_at": _iso(self.authorization_expires_at),
            "risk_policy_version": self.risk_policy_version,
        }


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def issue_authorization(**fields: Any) -> AuthorizedExecutionPlan:
    """Seal a plan AND record it as issued. Called only by the risk gate.

    Kept a module function rather than a classmethod so that the registry write
    lives in one place that is easy to grep for, and so the public model class
    exposes no way to authorize itself.

    The TTL is clamped here rather than trusted from the caller: an approval
    whose lifetime the caller chooses is not a temporal bound, and a plan issued
    with a ten-year expiry is a permanent licence to place one specific order
    against an exposure snapshot from a decade earlier.
    """
    issued_at: datetime = fields["risk_checked_at"]
    expires: datetime = fields["authorization_expires_at"]
    ceiling = issued_at + MAX_AUTHORIZATION_TTL
    if expires > ceiling:
        expires = ceiling
    fields["authorization_expires_at"] = expires
    fields["nonce"] = secrets.token_hex(16)

    plan = AuthorizedExecutionPlan._seal(**fields)
    if len(_ISSUED) >= _MAX_TRACKED:
        # Drop the oldest tracked nonces. A dropped nonce fails closed: its plan
        # stops verifying, which forces reauthorization rather than allowing an
        # unchecked submit.
        for stale in list(_ISSUED)[: _MAX_TRACKED // 2]:
            _ISSUED.pop(stale, None)
    _ISSUED[plan.nonce] = False
    return plan
