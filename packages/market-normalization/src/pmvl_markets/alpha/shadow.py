"""Shadow decisions: what the system would have done, frozen before the outcome.

A shadow decision is the only kind of evidence this repository can produce that
is neither a backtest nor a live trade. It is a claim published *before* the
answer exists, which is what makes it evidence at all - and the entire value of
it rests on one property:

    **A published shadow decision is never rewritten.**

Outcomes are *appended*. When a market settles, or a proposed order would have
filled, that fact becomes a new row pointing at the original decision. The
original row does not learn anything.

Why that matters more than it sounds
------------------------------------
The failure it prevents is not fraud, it is drift. A decision record that can be
updated will be updated: by a schema migration that backfills a field, by a
rerun that "corrects" a stale profile, by a well-meant fix that recomputes an old
signal under the current factor version. Each is individually reasonable and each
destroys the only thing the record was for. The existing
``recommendation_snapshots`` table made exactly this choice for exactly this
reason, and this is the same contract for a different kind of claim.

So :class:`ShadowDecision` is frozen, it carries a content hash over its own
fields, and :func:`verify_unchanged` recomputes that hash. A record whose hash no
longer matches its content is reported, not repaired.

This phase records only. Nothing here places an order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from pmvl_shared.enums import Side
from pmvl_shared.timeutil import ensure_utc

from .models import AlphaScore, Liquidity, OutcomeEstimate, ProposedOrder
from .registry import FeatureVector
from .strategy import Signal


class ShadowStatus(str, Enum):
    """Where a shadow decision is in its life.

    ``PROPOSED`` and ``NO_TRADE`` are both terminal for the *decision*; the
    difference is whether an order was proposed at all. Recording the no-trades
    is not bookkeeping - a strategy that declines 400 times and trades 3 is a
    different object from one that trades 3 out of 3, and only the recorded
    refusals distinguish them.
    """

    PROPOSED = "proposed"
    NO_TRADE = "no_trade"


#: Fields that make up a decision's identity. Order is fixed and the hash is
#: computed over exactly these, so adding a field to the dataclass later cannot
#: silently change the hash of records already written.
_HASHED_FIELDS = (
    "decision_id",
    "strategy",
    "strategy_version",
    "market_key",
    "decision_time",
    "status",
    "side",
    "liquidity",
    "model_probability",
    "model_probability_low",
    "model_probability_high",
    "conservative_probability",
    "executable_probability",
    "alpha_expected_move",
    "alpha_confidence",
    "proposed_price",
    "proposed_size",
    "expected_edge_per_contract",
    "information_cutoff",
    "outcome_model_version",
    "alpha_model_version",
    "factor_versions",
    "feature_values",
    "source_values",
    "rationale",
)


@dataclass(frozen=True)
class ShadowDecision:
    """One frozen decision. Immutable by construction and by contract.

    Everything the research plan requires a shadow signal to freeze is a field
    here: the decision timestamp, the information set (via ``information_cutoff``
    and the frozen ``source_values``), the feature vector, the model probability,
    the alpha score, the executable market state, the proposed order and size,
    and the version identifiers that make the whole thing reproducible.
    """

    decision_id: str
    strategy: str
    strategy_version: str
    market_key: str
    decision_time: datetime
    status: ShadowStatus
    side: Side
    liquidity: Liquidity

    model_probability: Decimal
    model_probability_low: Decimal
    model_probability_high: Decimal
    conservative_probability: Decimal
    executable_probability: Decimal

    alpha_expected_move: Decimal
    alpha_confidence: Decimal

    proposed_price: Decimal | None
    proposed_size: Decimal | None
    expected_edge_per_contract: Decimal

    #: The latest ``available_at`` the decision was permitted to see. Anything
    #: after this instant was not part of the information set, and a later audit
    #: can check that claim against the observation rows.
    information_cutoff: datetime

    outcome_model_version: str
    alpha_model_version: str
    #: factor name -> version, for every factor that contributed.
    factor_versions: Mapping[str, str] = field(default_factory=dict)
    #: The full feature vector as computed, values and refusals alike.
    feature_values: Mapping[str, Any] = field(default_factory=dict)
    #: The source observations behind it: kind -> {value, available_at, source}.
    source_values: Mapping[str, Any] = field(default_factory=dict)
    rationale: tuple[str, ...] = ()
    content_hash: str = ""

    def compute_hash(self) -> str:
        """Deterministic digest over the identity fields.

        ``sort_keys`` and a fixed separator set, so a dict whose insertion order
        differs between two runs hashes identically. Without that the hash would
        detect Python's iteration order rather than a change in content.
        """
        payload = {name: _jsonable(getattr(self, name)) for name in _HASHED_FIELDS}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def sealed(self) -> "ShadowDecision":
        """Return this decision with its hash filled in.

        A separate step rather than a ``__post_init__`` so that reading a stored
        record back does not silently recompute - and therefore repair - a hash
        that should have been checked.
        """
        from dataclasses import replace  # noqa: PLC0415

        return replace(self, content_hash=self.compute_hash())

    def as_dict(self) -> dict[str, Any]:
        out = {name: _jsonable(getattr(self, name)) for name in _HASHED_FIELDS}
        out["content_hash"] = self.content_hash
        return out


class ImmutabilityError(AssertionError):
    """A published shadow decision no longer matches its own hash."""


def verify_unchanged(decision: ShadowDecision) -> None:
    """Raise if a decision's content has moved since it was sealed.

    Raise, not repair. A record that has drifted is evidence of a bug somewhere
    upstream, and recomputing the hash would delete the only trace of it.
    """
    if not decision.content_hash:
        raise ImmutabilityError(
            f"shadow decision {decision.decision_id} was never sealed; an "
            "unsealed record proves nothing about when it was written"
        )
    actual = decision.compute_hash()
    if actual != decision.content_hash:
        raise ImmutabilityError(
            f"shadow decision {decision.decision_id} has been modified since "
            f"publication: stored hash {decision.content_hash[:16]}... but its "
            f"content now hashes to {actual[:16]}..."
        )


@dataclass(frozen=True)
class ShadowOutcome:
    """Something that happened *after* a decision. Appended, never merged.

    A separate record with its own timestamp, pointing at a decision by ID. The
    decision does not gain a ``result`` field, because a decision with a mutable
    result field is a decision that can be edited after the fact and there is no
    way for a reader to tell whether it was.
    """

    decision_id: str
    observed_at: datetime
    kind: str
    #: What actually happened: a fill, a settlement, a price at the horizon.
    value: Decimal | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "observed_at": self.observed_at.isoformat(),
            "kind": self.kind,
            "value": None if self.value is None else str(self.value),
            "detail": dict(self.detail),
        }


def record_decision(
    *,
    decision_id: str,
    strategy: str,
    strategy_version: str,
    signal: Signal,
    outcome: OutcomeEstimate,
    alpha: AlphaScore,
    features: FeatureVector,
    factor_versions: Mapping[str, str],
    order: ProposedOrder | None,
    information_cutoff: datetime,
    source_values: Mapping[str, Any] | None = None,
) -> ShadowDecision:
    """Build and seal a shadow decision from a live evaluation.

    Records a ``NO_TRADE`` when no order was proposed. Recording only the trades
    would make the shadow record a highlight reel: a strategy's refusals are the
    larger part of its behaviour and the part that determines whether its hit
    rate means anything.
    """
    return ShadowDecision(
        decision_id=decision_id,
        strategy=strategy,
        strategy_version=strategy_version,
        market_key=signal.market_key,
        decision_time=ensure_utc(signal.decision_time),
        status=ShadowStatus.PROPOSED if order else ShadowStatus.NO_TRADE,
        side=signal.side,
        liquidity=signal.liquidity,
        model_probability=outcome.probability,
        model_probability_low=outcome.low,
        model_probability_high=outcome.high,
        conservative_probability=signal.conservative_probability,
        executable_probability=signal.executable_probability,
        alpha_expected_move=alpha.expected_move,
        alpha_confidence=alpha.confidence,
        proposed_price=order.limit_price if order else None,
        proposed_size=order.size if order else None,
        expected_edge_per_contract=signal.expected_edge_per_contract,
        information_cutoff=ensure_utc(information_cutoff),
        outcome_model_version=outcome.model_version,
        alpha_model_version=alpha.model_version,
        factor_versions=dict(factor_versions),
        feature_values=features.as_dict()["values"],
        source_values=dict(source_values or {}),
        rationale=tuple(signal.reasons)
        + ((signal.declined_reason,) if signal.declined_reason else ()),
    ).sealed()


class ShadowLedger:
    """An append-only in-memory ledger, for tests and for a dry run.

    The database-backed store is :class:`ShadowStore`. This one exists so the
    immutability contract can be tested without a database, and so a research
    run can produce shadow records before anybody has decided to persist them.
    """

    def __init__(self) -> None:
        self._decisions: dict[str, ShadowDecision] = {}
        self._outcomes: list[ShadowOutcome] = []

    def publish(self, decision: ShadowDecision) -> ShadowDecision:
        verify_unchanged(decision)
        if decision.decision_id in self._decisions:
            # Not an overwrite and not an idempotent no-op: a second publish
            # under one ID means two different decisions are competing for one
            # identity, and silently keeping either loses the other.
            raise ImmutabilityError(
                f"shadow decision {decision.decision_id} is already published; "
                "a published decision is never replaced"
            )
        self._decisions[decision.decision_id] = decision
        return decision

    def append_outcome(self, outcome: ShadowOutcome) -> ShadowOutcome:
        if outcome.decision_id not in self._decisions:
            raise KeyError(
                f"no published decision {outcome.decision_id} for this outcome"
            )
        decision = self._decisions[outcome.decision_id]
        if ensure_utc(outcome.observed_at) < decision.decision_time:
            # An outcome that predates its decision is either a clock problem or
            # a backfill, and both would make the record claim foreknowledge.
            raise ValueError(
                f"outcome observed at {outcome.observed_at.isoformat()} predates "
                f"decision {outcome.decision_id} at "
                f"{decision.decision_time.isoformat()}"
            )
        self._outcomes.append(outcome)
        return outcome

    def decisions(self) -> tuple[ShadowDecision, ...]:
        return tuple(
            sorted(self._decisions.values(), key=lambda d: (d.decision_time, d.decision_id))
        )

    def outcomes_for(self, decision_id: str) -> tuple[ShadowOutcome, ...]:
        return tuple(
            sorted(
                (o for o in self._outcomes if o.decision_id == decision_id),
                key=lambda o: o.observed_at,
            )
        )

    def audit(self) -> list[str]:
        """Re-verify every published decision. Empty means the ledger is intact."""
        problems: list[str] = []
        for decision in self.decisions():
            try:
                verify_unchanged(decision)
            except ImmutabilityError as exc:
                problems.append(str(exc))
        return problems


def decision_id_for(
    strategy: str, market_key: str, decision_time: datetime, model_version: str
) -> str:
    """A deterministic ID: same decision, same identifier.

    Deterministic rather than random so that re-running a research pass produces
    the same IDs and a duplicate publish is *detected* rather than creating a
    second copy of the same decision under a new name.
    """
    raw = f"{strategy}|{market_key}|{ensure_utc(decision_time).isoformat()}|{model_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def frozen_source_values(
    observations: Iterable[Any], kinds: Sequence[str] = ()
) -> dict[str, Any]:
    """Freeze the source observations behind a decision, for the record.

    Values *and* their four timestamps. Storing only the value would make a later
    audit unable to check the availability claim, which is the one thing a
    shadow record most needs to be checkable on.
    """
    wanted = set(kinds)
    out: dict[str, Any] = {}
    for obs in observations:
        if wanted and obs.kind not in wanted:
            continue
        out[f"{obs.kind}:{obs.source_id}"] = {
            "value": _jsonable(obs.value),
            "available_at": _jsonable(obs.available_at),
            "observed_at": _jsonable(obs.observed_at),
            "event_time": _jsonable(obs.event_time),
        }
    return out
