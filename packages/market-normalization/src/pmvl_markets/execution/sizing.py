"""How many contracts, decided by something that is allowed to decide it.

Three quantities are routinely confused, and collapsing any two of them produces
an order nobody authorized:

``capacity_ceiling``
    What the *book* can absorb before the marginal contract stops being
    profitable. Produced by ``max_profitable_size`` and stored on a
    recommendation as ``recommended_position_cap``. It is an upper bound
    discovered from depth - not a position anybody recommended. Reading it as a
    quantity asked for 213-1829 contracts on real rows, roughly 250x anything the
    model sized.

``requested_quantity``
    What a *sizing policy* asked for. This is the number a human would have to
    defend. It is always the output of a named, versioned policy, and it is never
    inferred from a ceiling or from a risk cap.

``approved_quantity``
    What the risk gate permitted. The gate may **reduce** a request; it may never
    raise one. A cap of five contracts is a limit, not a recommendation to buy
    five.

The invariant, whenever all three exist::

    approved_quantity <= requested_quantity <= capacity_ceiling

What this module guarantees, and what it does not
-------------------------------------------------
Phase 5A shipped a public ``create_order`` that consulted no risk gate. Phase 5B
replaced it with a public ``seal()`` that consulted no risk gate. Both times a
docstring asserted a unique safe path and a public helper reproduced the
privileged artifact. So this one states its limits rather than its ambitions.

**Guaranteed.** A quantity never travels alone: it carries a named, versioned
policy label, the capacity ceiling it was bounded by, and a seal. Editing any of
that after issuance is detected. A hand-constructed :class:`SizingDecision` has no
registered nonce and is refused. The risk gate cross-checks that the intent's
quantity is the one sizing actually authorized, and never approves more than was
requested.

**Not guaranteed.** That only blessed code issued it. :func:`issue_sizing` is
reachable by any caller who defines a class, and Python provides no way to
prevent that. The label is therefore an *audit* claim, not an authorization -
which is why the quantity still has to survive every risk cap regardless of who
claims to have chosen it.

The one label that is protected is ``STRATEGY_SIZED``: it asserts a validated
sizing policy chose the number, no such policy exists, and issuing it is refused
outright rather than left as something a caller can simply declare.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import utcnow

from .models import ExecutionMode
from .modes import ExecutionModeError

log = get_logger(__name__)

__all__ = [
    "SizingPolicy",
    "SizingDecision",
    "SizingRefused",
    "SizingNotIssued",
    "DemoExecutionProbePolicy",
    "StrategySizingPolicy",
    "DEMO_PROBE_SIZE",
    "SIZING_POLICY_VERSION",
]

#: One contract. Enough to exercise the venue's order lifecycle, small enough
#: that it expresses no view. Deliberately not named ``recommended_size`` or
#: ``optimal_size``: it is neither.
DEMO_PROBE_SIZE = 1

SIZING_POLICY_VERSION = "5c0.1"

_SEAL_KEY = secrets.token_bytes(32)
_ISSUED: dict[str, bool] = {}
_MAX_TRACKED = 4096


class SizingRefused(ExecutionModeError):
    """A policy declined to size this order. Not an error - a refusal."""


class SizingNotIssued(ExecutionModeError):
    """A sizing decision that no policy in this process actually issued."""


class SizingPolicy(StrEnum):
    """Why a quantity is what it is.

    Every value here is a truthful answer to "why N contracts?". The answers that
    are deliberately *absent* are the dangerous ones: a capacity ceiling read as a
    size, a risk cap read as a size, or an unexplained number.
    """

    #: A validated strategy sizing policy said so. Nothing in this repository
    #: currently produces this: the research layer publishes a Kelly *fraction*
    #: of a bankroll that does not exist, and a capacity ceiling. Reserved so the
    #: enum does not have to change when one arrives.
    STRATEGY_SIZED = "strategy_sized"

    #: One contract, to test venue mechanics. Carries no view and is DEMO-only.
    DEMO_EXECUTION_PROBE = "demo_execution_probe"

    #: A human typed a number for a one-off demo smoke test.
    OPERATOR_SPECIFIED_DEMO_SMOKE = "operator_specified_demo_smoke"


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """A quantity plus the reason it is that quantity.

    Mint via a policy, never by hand: a directly constructed instance has no
    registered nonce and :meth:`assert_issued` refuses it.
    """

    requested_quantity: int
    policy: SizingPolicy
    policy_version: str
    mode: ExecutionMode
    ticker: str
    decided_at: datetime
    capacity_ceiling: int | None = None
    detail: str = ""
    nonce: str = ""
    seal_digest: str = ""

    def _material(self) -> str:
        return "\x1f".join(
            (
                str(self.requested_quantity),
                self.policy.value,
                self.policy_version,
                self.mode.value,
                self.ticker,
                "" if self.capacity_ceiling is None else str(self.capacity_ceiling),
                self.nonce,
            )
        )

    def compute_seal(self) -> str:
        return hmac.new(
            _SEAL_KEY, self._material().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def assert_issued(self, *, mode: ExecutionMode) -> None:
        """Everything execution needs to trust this number."""
        if not self.seal_digest or not hmac.compare_digest(
            self.seal_digest, self.compute_seal()
        ):
            raise SizingNotIssued(
                "sizing decision was modified after issuance, or was constructed "
                "directly rather than by a sizing policy"
            )
        if _ISSUED.get(self.nonce) is None:
            raise SizingNotIssued(
                "sizing decision was not issued by a policy in this process; "
                "constructing one does not authorize a quantity"
            )
        if self.mode is not mode:
            raise SizingNotIssued(
                f"sizing decision is for {self.mode.value}, presented in "
                f"{mode.value}"
            )
        # The probe exists to test venue mechanics in a sandbox. Binding it to
        # DEMO here as well as at the policy means a decision that somehow
        # reached another mode still cannot be spent there.
        if (
            str(self.policy) == SizingPolicy.DEMO_EXECUTION_PROBE.value
            and mode is not ExecutionMode.DEMO
        ):
            raise SizingNotIssued(
                "demo execution-probe sizing is valid only in the demo sandbox"
            )
        if self.requested_quantity <= 0:
            raise SizingNotIssued(
                f"non-positive requested quantity {self.requested_quantity}"
            )
        if (
            self.capacity_ceiling is not None
            and self.requested_quantity > self.capacity_ceiling
        ):
            raise SizingNotIssued(
                f"requested {self.requested_quantity} exceeds the capacity "
                f"ceiling {self.capacity_ceiling}"
            )
        if _ISSUED.get(self.nonce):
            raise SizingNotIssued(
                "this sizing decision has already been used; the audit columns "
                "must distinguish 'the policy decided N times' from 'one "
                "decision was replayed N times'"
            )

    def consume(self) -> None:
        """Spend this decision so it underwrites exactly one order."""
        if _ISSUED.get(self.nonce) is None:
            raise SizingNotIssued("cannot consume a decision that was not issued")
        _ISSUED[self.nonce] = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_quantity": self.requested_quantity,
            "policy": self.policy.value,
            "policy_version": self.policy_version,
            "mode": self.mode.value,
            "capacity_ceiling": self.capacity_ceiling,
            "detail": self.detail,
        }


def issue_sizing(**fields: Any) -> SizingDecision:
    """Mint a sizing decision. Called by policies, not by callers.

    Honest about what this does and does not guarantee. It guarantees that a
    quantity always travels with a named, versioned policy label that cannot be
    changed afterwards without detection. It does **not** guarantee that only
    blessed code issued it - any caller can define a class and call this, and
    Python offers no way to prevent that.

    What it does refuse is the one label that would be a lie today:
    ``STRATEGY_SIZED`` asserts that a validated sizing policy chose this number,
    and none exists. Reserving the label keeps "the strategy sized this" from
    becoming a thing a caller can simply declare.
    """
    policy = fields.get("policy")
    # Compared by value, and required to be a genuine member. An `is` check was
    # bypassed by a look-alike StrEnum defining the same value, which then
    # reached the ledger's audit column as `strategy_sized` - the one label this
    # guard exists to reserve.
    if not isinstance(policy, SizingPolicy):
        raise SizingRefused(
            f"policy must be a SizingPolicy member, got {type(policy).__name__}"
        )
    if str(policy.value) == SizingPolicy.STRATEGY_SIZED.value:
        raise SizingRefused(
            "STRATEGY_SIZED cannot be issued: no validated strategy sizing "
            "policy exists in this repository"
        )
    # The probe label's entire meaning is "exactly one contract, expresses no
    # view". Binding the size only inside the policy object meant bypassing the
    # policy bypassed the size, and a 5-contract order could carry a label that
    # reads in the audit trail as a 1-contract mechanics test.
    if (
        str(policy.value) == SizingPolicy.DEMO_EXECUTION_PROBE.value
        and fields.get("requested_quantity") != DEMO_PROBE_SIZE
    ):
        raise SizingRefused(
            f"{SizingPolicy.DEMO_EXECUTION_PROBE.value} is exactly "
            f"{DEMO_PROBE_SIZE} contract; refusing to label "
            f"{fields.get('requested_quantity')} as a mechanics probe"
        )
    fields["nonce"] = secrets.token_hex(16)
    fields.pop("seal_digest", None)
    draft = SizingDecision(**fields, seal_digest="")
    decision = SizingDecision(**fields, seal_digest=draft.compute_seal())
    if len(_ISSUED) >= _MAX_TRACKED:
        for stale in list(_ISSUED)[: _MAX_TRACKED // 2]:
            _ISSUED.pop(stale, None)
    _ISSUED[decision.nonce] = False
    return decision


class StrategySizingPolicy:
    """The policy that would size on alpha. It does not exist yet, and says so.

    The research record publishes ``fractional_kelly`` - a fraction of a bankroll
    the research layer does not model - and ``recommended_position_cap``, a depth
    ceiling. Neither is a position size, and converting one into contracts here
    would be inventing a sizing policy inside an adapter, which is exactly the
    move that makes a backtest and a live system disagree.

    So this refuses, loudly, until a validated policy is built and evaluated.
    """

    policy = SizingPolicy.STRATEGY_SIZED

    def decide(
        self, *, mode: ExecutionMode, ticker: str, capacity_ceiling: int | None
    ) -> SizingDecision:
        raise SizingRefused(
            "no validated strategy sizing policy exists: research publishes a "
            "Kelly fraction of an unmodelled bankroll and a depth ceiling, "
            "neither of which is a position size"
        )


class DemoExecutionProbePolicy:
    """One contract, in the demo sandbox, to test that the venue loop works.

    This is **not** a strategy. It expresses no view about the market, and no
    result obtained with it says anything about sizing quality or profitability.
    Its only claim is that an order of this size can be placed, observed,
    reconciled and cancelled.

    Structurally bound to DEMO in three places: this constructor, the issued
    decision's own mode field, and :meth:`SizingDecision.assert_issued`.
    """

    policy = SizingPolicy.DEMO_EXECUTION_PROBE

    def __init__(self, *, purpose: str = "EXECUTION_MECHANICS_VALIDATION") -> None:
        self.purpose = purpose

    def decide(
        self,
        *,
        mode: ExecutionMode,
        ticker: str,
        capacity_ceiling: int | None = None,
        now: datetime | None = None,
    ) -> SizingDecision:
        if mode is not ExecutionMode.DEMO:
            raise SizingRefused(
                f"demo execution-probe sizing refused in {mode.value}: it is a "
                "sandbox mechanics test and carries no view"
            )
        if capacity_ceiling is not None and capacity_ceiling < DEMO_PROBE_SIZE:
            raise SizingRefused(
                f"capacity ceiling {capacity_ceiling} cannot absorb even one "
                "contract"
            )
        return issue_sizing(
            requested_quantity=DEMO_PROBE_SIZE,
            policy=self.policy,
            policy_version=SIZING_POLICY_VERSION,
            mode=ExecutionMode.DEMO,
            ticker=ticker,
            decided_at=now or utcnow(),
            capacity_ceiling=capacity_ceiling,
            detail=f"DEMO_PROBE_SIZE; purpose={self.purpose}",
        )
