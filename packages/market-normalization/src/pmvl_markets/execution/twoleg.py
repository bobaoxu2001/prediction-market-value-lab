"""Two-leg cross-venue execution: the state machine, and what it refuses to claim.

Buying one contract on Kalshi and its complement on Polymarket is two independent
orders on two independent systems. There is no cross-venue atomicity to fall back on,
and no combination of each venue's fill-or-kill produces one: two separate FOKs can
still resolve one filled and one killed, which is precisely the state a single FOK is
supposed to prevent. The structure is therefore *designed around* the half-filled
case rather than hoping it does not happen.

Three rules do most of the work.

**An acknowledgement is not a fill.** :meth:`SimulatedVenueClient.submit` returns an
ack and nothing else. Filled quantity only ever enters through
:meth:`SimulatedVenueClient.confirm`, a separate call against the venue's own record.
A transport that returns HTTP 200 has told us the request arrived, which is a claim
about the network, not about the book.

**A timeout is unknown, not failed.** When submission times out the pair moves to
``UNKNOWN`` and the executor *queries* by the derived ``client_order_id``. It never
resubmits. Resubmitting on a timeout is how one intended position becomes two, and
the second one is unhedged.

**Success is recomputed from fills, not inherited from the plan.**
:attr:`PairJournal.arbitrage_completed` ignores the signal that proposed the pair and
re-runs :func:`~pmvl_markets.settlement.payoff.evaluate_payoff` against the quantities
and prices that actually filled. A pair whose second leg filled short is then a
smaller, *unbalanced* position that the evaluator scores on its own merits - which is
how a one-legged outcome becomes a recorded loss rather than a recorded arbitrage.

Nothing here can reach a real venue. The clients are simulations, and the mode gates
in :mod:`pmvl_markets.execution.modes` still apply above them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Mapping, Protocol

from pmvl_shared.enums import Platform
from pmvl_shared.money import ZERO, quantize_usd, safe_div
from pmvl_shared.timeutil import utcnow

from ..settlement.payoff import (
    ContractRule,
    HeldLeg,
    PayoffReport,
    SettlementLinkage,
    StructuralStatus,
    evaluate_payoff,
)
from .assessment import (
    AccessStatus,
    CandidateAssessment,
    EconomicsStatus,
    ExecutionStatus,
    QuoteStatus,
)
from .modes import ExecutionMode, assert_mode_may_write
from .models import OrderStatus, TimeInForce
from .funding import FundingLedger
from .venues import VenueOrderSpec

#: Re-confirms an opportunity between funding and submission. Returns
#: ``(still_good, reason)``; the reason is journalled either way, so a run records
#: why it proceeded as well as why it stopped.
RecheckFn = Callable[["PairCandidate"], tuple[bool, str]]

__all__ = [
    "RecheckFn",
    "PairState",
    "PAIR_TRANSITIONS",
    "is_valid_pair_transition",
    "LegTimeout",
    "SubmitAck",
    "LegReport",
    "SimulatedVenueClient",
    "PairCandidate",
    "LegRecord",
    "PairTransition",
    "PairJournal",
    "RemedyAction",
    "RemedyDecision",
    "RemedyPolicy",
    "TwoLegExecutor",
    "LegRecord",
    "NotASimulation",
    "UnwindThenEscalate",
    "derive_pair_client_order_id",
    "PAIR_IDEMPOTENCY_VERSION",
    "TERMINAL_PAIR_STATES",
]


class PairState(StrEnum):
    """Lifecycle of one cross-venue pair.

    ``ABANDONED`` and ``NO_FILL`` are additions to the obvious happy path, and both
    earn their place: a candidate rejected before any order left the process is a
    materially different event from one that reached a venue and came back empty, and
    collapsing them loses the ability to tell "the gate worked" from "the market
    moved".
    """

    DETECTED = "detected"
    VALIDATED = "validated"
    FUNDS_RESERVED = "funds_reserved"
    RECHECKED = "rechecked"
    SUBMITTING = "submitting"

    BOTH_FILLED = "both_filled"
    PARTIALLY_FILLED = "partially_filled"     # both legs touched, sizes unequal
    ONE_LEG_FILLED = "one_leg_filled"         # one leg live, the other empty
    NO_FILL = "no_fill"
    UNKNOWN = "unknown"                       # a venue's answer has not been obtained

    HEDGED = "hedged"                         # exposure closed, possibly at a loss
    UNWINDING = "unwinding"
    ESCALATED = "escalated"                   # needs a human; never auto-resolved

    RECONCILED = "reconciled"
    ABANDONED = "abandoned"                   # refused before anything was submitted


#: Transitions the machine permits. Anything else raises rather than being written,
#: so a journal cannot contain a path that the design does not allow.
PAIR_TRANSITIONS: Mapping[PairState, frozenset[PairState]] = {
    PairState.DETECTED: frozenset({PairState.VALIDATED, PairState.ABANDONED}),
    PairState.VALIDATED: frozenset({PairState.FUNDS_RESERVED, PairState.ABANDONED}),
    PairState.FUNDS_RESERVED: frozenset({PairState.RECHECKED, PairState.ABANDONED}),
    PairState.RECHECKED: frozenset({PairState.SUBMITTING, PairState.ABANDONED}),
    PairState.SUBMITTING: frozenset(
        {
            PairState.BOTH_FILLED,
            PairState.PARTIALLY_FILLED,
            PairState.ONE_LEG_FILLED,
            PairState.NO_FILL,
            PairState.UNKNOWN,
        }
    ),
    # A resolved-unknown can land on any outcome, including a late fill that arrives
    # after the timeout. It can also stay unresolvable, which is an escalation.
    PairState.UNKNOWN: frozenset(
        {
            PairState.BOTH_FILLED,
            PairState.PARTIALLY_FILLED,
            PairState.ONE_LEG_FILLED,
            PairState.NO_FILL,
            PairState.ESCALATED,
        }
    ),
    PairState.BOTH_FILLED: frozenset({PairState.RECONCILED, PairState.UNKNOWN}),
    PairState.PARTIALLY_FILLED: frozenset(
        {PairState.HEDGED, PairState.UNWINDING, PairState.ESCALATED, PairState.UNKNOWN}
    ),
    PairState.ONE_LEG_FILLED: frozenset(
        {PairState.HEDGED, PairState.UNWINDING, PairState.ESCALATED, PairState.UNKNOWN}
    ),
    # NO_FILL can go back to UNKNOWN because reconciliation can discover a fill that
    # arrived after the venue said there was none. A state that looks terminal but is
    # contradicted by the venue's own record is not terminal; it is wrong.
    PairState.NO_FILL: frozenset({PairState.RECONCILED, PairState.UNKNOWN}),
    PairState.UNWINDING: frozenset(
        {PairState.HEDGED, PairState.ESCALATED, PairState.RECONCILED}
    ),
    PairState.HEDGED: frozenset({PairState.RECONCILED, PairState.ESCALATED}),
    PairState.ESCALATED: frozenset({PairState.RECONCILED}),
    PairState.RECONCILED: frozenset(),
    PairState.ABANDONED: frozenset(),
}

#: States from which nothing further happens on its own.
TERMINAL_PAIR_STATES = frozenset({PairState.RECONCILED, PairState.ABANDONED})


def is_valid_pair_transition(old: PairState, new: PairState) -> bool:
    if old == new:
        return True
    return new in PAIR_TRANSITIONS.get(old, frozenset())


class LegTimeout(RuntimeError):
    """The venue did not answer in time. Says nothing about whether it acted."""


@dataclass(frozen=True, slots=True)
class SubmitAck:
    """The venue accepted the request. Carries no fill information, on purpose."""

    accepted: bool
    venue_order_id: str | None = None
    reject_reason: str = ""


@dataclass(frozen=True, slots=True)
class LegReport:
    """The venue's own record of an order: the only source of filled quantity."""

    status: OrderStatus
    filled_quantity: Decimal = ZERO
    average_price: Decimal | None = None
    fees_usd: Decimal | None = None
    venue_order_id: str | None = None
    raw_status: str = ""

    @property
    def is_filled_at_all(self) -> bool:
        return self.filled_quantity > ZERO


class SimulatedVenueClient(Protocol):
    """A venue, simulated. Two calls, because an ack and a fill are different facts.

    A future live adapter would implement this same protocol, which is why ``submit``
    deliberately cannot return a fill: a real adapter that wanted to would have to
    change the protocol, in a diff, in review.
    """

    venue: Platform

    def submit(
        self, spec: VenueOrderSpec, *, client_order_id: str, attempt_id: str
    ) -> SubmitAck:
        """Send the order. May raise :class:`LegTimeout`."""

    def confirm(self, *, client_order_id: str) -> LegReport:
        """Ask the venue what it did with this idempotency key.

        Keyed on ``client_order_id`` rather than a venue order id, because after a
        timeout there is no venue order id and the whole point is to find out whether
        one exists.
        """

    def close_out(
        self, spec: VenueOrderSpec, quantity: Decimal, *, client_order_id: str
    ) -> LegReport:
        """Sell back a position this account already holds, at whatever the book pays.

        Closing a long is not shorting: it disposes of contracts already owned. No
        path here opens a short.
        """


@dataclass
class PairCandidate:
    """Everything needed to attempt one pair, and to judge it independently.

    ``rules`` and ``linkages`` go straight to the settlement evaluator. The candidate
    carries no ``guaranteed`` flag and no scanner label, because the executor is not
    permitted to inherit a conclusion it did not derive.
    """

    pair_id: str
    leg_a: VenueOrderSpec
    leg_b: VenueOrderSpec
    rules: dict[str, ContractRule]
    linkages: list[SettlementLinkage] = field(default_factory=list)

    #: Settlement shares the pair is built around. Both legs must reach this for the
    #: complementary structure to hold; anything less is an unbalanced position.
    target_quantity: Decimal = ZERO

    strategy: str = "cross_venue_pair"
    strategy_version: str = "1"
    source_signal_id: str = ""
    revision: int = 0
    detected_at: datetime = field(default_factory=utcnow)

    #: Costs outside the legs themselves - bridge, withdrawal, gas.
    extra_cost_usd: Decimal = ZERO

    #: Freshness and access are facts about the environment, supplied by the caller
    #: that read them. Defaults are the blocking values, never the permissive ones.
    quote_status: QuoteStatus = QuoteStatus.MISSING
    access_status: AccessStatus = AccessStatus.UNVERIFIED

    def __post_init__(self) -> None:
        if self.target_quantity <= ZERO:
            self.target_quantity = min(self.leg_a.quantity, self.leg_b.quantity)

    @property
    def legs(self) -> tuple[VenueOrderSpec, VenueOrderSpec]:
        return (self.leg_a, self.leg_b)

    def leg_label(self, spec: VenueOrderSpec) -> str:
        return f"{spec.contract.venue.value}:{spec.contract.contract_key}"


@dataclass
class LegRecord:
    """One leg's own story: what was asked, what the venue said, what it cost."""

    label: str
    venue: Platform
    contract_key: str
    client_order_id: str
    requested_quantity: Decimal
    requested_price: Decimal
    status: OrderStatus = OrderStatus.INTENDED
    filled_quantity: Decimal = ZERO
    average_price: Decimal | None = None
    fees_usd: Decimal | None = None
    venue_order_id: str | None = None
    #: Every HTTP attempt, for tracing. Length > 1 means a query happened, never a
    #: second submission.
    attempts: list[str] = field(default_factory=list)
    reservation_ref: str = ""
    notes: list[str] = field(default_factory=list)

    #: Quantity sold back to close an unbalanced position, and what that realised.
    #: Kept apart from the opening fill so the journal shows both the position that
    #: was taken and the position that was kept - collapsing them into a net number
    #: would hide that an unwind happened at all.
    closed_quantity: Decimal = ZERO
    close_proceeds_usd: Decimal = ZERO
    close_fees_usd: Decimal = ZERO

    @property
    def gross_cash_out_usd(self) -> Decimal:
        """Money this leg spent opening: fills at their price, plus fees.

        A leg whose fee the venue never reported contributes ``fees`` of zero *and*
        a note, rather than silently understating the cost - the note is what the
        reconciliation reads.
        """
        price = self.average_price if self.average_price is not None else ZERO
        return price * self.filled_quantity + (self.fees_usd or ZERO)

    @property
    def net_cash_out_usd(self) -> Decimal:
        """Opening cost, less whatever came back from closing part of it."""
        return self.gross_cash_out_usd - self.close_proceeds_usd + self.close_fees_usd

    @property
    def retained_quantity(self) -> Decimal:
        """What is still held after any unwind. This is what settles."""
        return self.filled_quantity - self.closed_quantity

    @property
    def unwind_loss_usd(self) -> Decimal:
        """Cost of the unwind: what the closed units cost, less what they fetched."""
        if self.closed_quantity <= ZERO:
            return ZERO
        basis = (self.average_price or ZERO) * self.closed_quantity
        return basis - self.close_proceeds_usd + self.close_fees_usd

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "venue": self.venue.value,
            "contract_key": self.contract_key,
            "client_order_id": self.client_order_id,
            "requested_quantity": str(self.requested_quantity),
            "requested_price": str(self.requested_price),
            "status": self.status.value,
            "filled_quantity": str(self.filled_quantity),
            "average_price": str(self.average_price) if self.average_price is not None else None,
            "fees_usd": str(self.fees_usd) if self.fees_usd is not None else None,
            "venue_order_id": self.venue_order_id,
            "attempts": list(self.attempts),
            "reservation_ref": self.reservation_ref,
            "closed_quantity": str(self.closed_quantity),
            "retained_quantity": str(self.retained_quantity),
            "close_proceeds_usd": str(quantize_usd(self.close_proceeds_usd)),
            "close_fees_usd": str(quantize_usd(self.close_fees_usd)),
            "unwind_loss_usd": str(quantize_usd(self.unwind_loss_usd)),
            "gross_cash_out_usd": str(quantize_usd(self.gross_cash_out_usd)),
            "net_cash_out_usd": str(quantize_usd(self.net_cash_out_usd)),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class PairTransition:
    at: datetime
    old: PairState
    new: PairState
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "from": self.old.value,
            "to": self.new.value,
            "reason": self.reason,
        }


class RemedyAction(StrEnum):
    """What to do about a pair that ended up lopsided."""

    #: Sell back the contracts that filled, accepting whatever the book pays.
    UNWIND = "unwind"
    #: Leave the exposure and hand it to a human. The safe default when unwinding
    #: would itself be a guess.
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class RemedyDecision:
    action: RemedyAction
    reason: str


class RemedyPolicy(Protocol):
    def decide(self, journal: "PairJournal") -> RemedyDecision: ...


class UnwindThenEscalate:
    """Unwind a lopsided pair; escalate if any part of it cannot be closed.

    There is no "complete the missing leg" option here. Chasing the second leg after
    the first has filled means buying at whatever the book has moved to, which is a
    new trade at a new price with no guarantee it still has an edge - and it is
    exactly the behaviour that turns a small execution failure into a large directional
    position.
    """

    def decide(self, journal: "PairJournal") -> RemedyDecision:
        if journal.state is PairState.UNKNOWN:
            return RemedyDecision(
                RemedyAction.ESCALATE,
                "a leg's status is unknown; unwinding could close a position that "
                "does not exist or double one that does",
            )
        return RemedyDecision(
            RemedyAction.UNWIND, "legs are unbalanced; closing the exposed quantity"
        )


@dataclass
class PairJournal:
    """The auditable record of one pair attempt, from detection to reconciliation."""

    pair_id: str
    mode: ExecutionMode
    state: PairState = PairState.DETECTED
    transitions: list[PairTransition] = field(default_factory=list)
    legs: list[LegRecord] = field(default_factory=list)
    assessment: CandidateAssessment = field(default_factory=CandidateAssessment)
    #: Payoff scored against the *plan*, before anything was submitted.
    planned_payoff: PayoffReport | None = None
    #: Payoff scored against what actually filled. This is the one that decides.
    realised_payoff: PayoffReport | None = None
    #: Costs outside the legs - bridge, gas, withdrawal - carried from the candidate.
    extra_cost_usd: Decimal = ZERO
    abandon_reason: str = ""
    escalation_reason: str = ""
    residual_risks: list[str] = field(default_factory=list)

    def transition(self, new: PairState, reason: str) -> None:
        if not is_valid_pair_transition(self.state, new):
            raise ValueError(
                f"{self.pair_id}: {self.state.value} -> {new.value} is not a legal "
                "pair transition"
            )
        self.transitions.append(
            PairTransition(at=utcnow(), old=self.state, new=new, reason=reason)
        )
        self.state = new

    def leg(self, label: str) -> LegRecord | None:
        return next((leg for leg in self.legs if leg.label == label), None)

    @property
    def remedy_cost_usd(self) -> Decimal:
        """Money lost unwinding, derived from the legs rather than accumulated.

        Derived on purpose: a running total that some code path forgets to add to is
        a silent understatement of the loss, and it would be invisible because the
        number still looks plausible.
        """
        return sum((leg.unwind_loss_usd for leg in self.legs), ZERO)

    @property
    def matched_quantity(self) -> Decimal:
        """Settlement shares held on *every* leg. This is the size that is hedged."""
        if not self.legs:
            return ZERO
        return min(leg.retained_quantity for leg in self.legs)

    @property
    def total_cash_out_usd(self) -> Decimal:
        return (
            sum((leg.net_cash_out_usd for leg in self.legs), ZERO) + self.extra_cost_usd
        )

    @property
    def arbitrage_completed(self) -> bool:
        """True only for a pair that reached settlement-proof profit on real fills.

        Four independent conditions, all required:

        1. the machine ended in ``BOTH_FILLED`` or ``RECONCILED`` from it;
        2. every leg filled its full requested size;
        3. the settlement evaluator, re-run on the *filled* quantities and prices,
           returns ``GUARANTEED``;
        4. nothing was spent unwinding.

        Condition 3 is the one that cannot be satisfied by accident: it re-derives
        the payoff from the rules and the fills, so a leg that filled short fails it
        even if every status field upstream said the pair was fine.
        """
        if self.state not in (PairState.BOTH_FILLED, PairState.RECONCILED):
            return False
        if not self.legs or any(
            leg.filled_quantity < leg.requested_quantity for leg in self.legs
        ):
            return False
        if self.remedy_cost_usd != ZERO:
            return False
        report = self.realised_payoff
        return report is not None and report.structural_status is StructuralStatus.GUARANTEED

    @property
    def realised_pnl_usd(self) -> Decimal | None:
        """Worst-case realised profit: guaranteed payout minus every dollar spent.

        ``None`` while the settlement state space is unknown. A position that was
        unwound reports the unwind's cost as a realised loss, which is what it is.
        """
        if self.realised_payoff is None:
            return None
        if self.realised_payoff.min_payout_usd is None:
            return None
        return self.realised_payoff.min_payout_usd - self.total_cash_out_usd

    def as_dict(self) -> dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "mode": self.mode.value,
            "state": self.state.value,
            "arbitrage_completed": self.arbitrage_completed,
            "transitions": [t.as_dict() for t in self.transitions],
            "legs": [leg.as_dict() for leg in self.legs],
            "assessment": self.assessment.as_dict(),
            "planned_payoff": self.planned_payoff.as_dict() if self.planned_payoff else None,
            "realised_payoff": self.realised_payoff.as_dict() if self.realised_payoff else None,
            "total_cash_out_usd": str(quantize_usd(self.total_cash_out_usd)),
            "remedy_cost_usd": str(quantize_usd(self.remedy_cost_usd)),
            "matched_quantity": str(self.matched_quantity),
            "extra_cost_usd": str(quantize_usd(self.extra_cost_usd)),
            "realised_pnl_usd": (
                str(quantize_usd(self.realised_pnl_usd))
                if self.realised_pnl_usd is not None
                else None
            ),
            "abandon_reason": self.abandon_reason,
            "escalation_reason": self.escalation_reason,
            "residual_risks": list(self.residual_risks),
        }


# --------------------------------------------------------------------- identity
#: Bumping invalidates every previously derived pair order id. Separate from the
#: single-leg ``IDEMPOTENCY_VERSION`` because the material differs; sharing a version
#: string between two derivations that hash different fields would let a change to
#: one silently imply a claim about the other.
PAIR_IDEMPOTENCY_VERSION = "p1"


def derive_pair_client_order_id(
    candidate: PairCandidate, spec: VenueOrderSpec
) -> str:
    """Deterministic idempotency key for one leg of one pair.

    Binds the pair, so the two legs cannot collide; binds price, size and
    time-in-force, so a retry that quietly changed any of them is a different order
    rather than a silent substitution; and binds ``revision``, so deciding to replace
    an order is distinguishable from retrying it.

    Excludes wall-clock time and attempt counters. That is the whole point: after a
    timeout, the query has to arrive at the same key the submission used.
    """

    material = "\x1f".join(
        (
            PAIR_IDEMPOTENCY_VERSION,
            candidate.pair_id,
            candidate.strategy,
            candidate.strategy_version,
            candidate.source_signal_id or candidate.pair_id,
            spec.contract.venue.value,
            spec.contract.contract_key,
            spec.contract.outcome.value,
            str(spec.limit_price),
            str(spec.quantity),
            spec.time_in_force.value,
            str(candidate.revision),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"pmvl-pair-{PAIR_IDEMPOTENCY_VERSION}-{digest}"


class NotASimulation(RuntimeError):
    """Raised when a client that is not a declared simulation is handed to the executor."""


class TwoLegExecutor:
    """Drives one candidate through the pair state machine. Simulation only.

    The executor refuses any client that does not declare ``is_simulation``. That is
    a structural gate, not a courtesy check: a future live adapter reaching this code
    would have to assert it is a simulation, in a diff, where a reviewer can see it -
    rather than arriving because a config value defaulted the wrong way.
    """

    def __init__(
        self,
        *,
        mode: ExecutionMode,
        funding: FundingLedger,
        clients: Mapping[Platform, SimulatedVenueClient],
        remedy: RemedyPolicy | None = None,
    ) -> None:
        if mode is not ExecutionMode.SHADOW and mode is not ExecutionMode.DEMO:
            raise ValueError(
                f"{mode.value} is not a mode this executor runs in; "
                "cross-venue pairs are shadow/demo only in this round"
            )
        for venue, client in clients.items():
            if not getattr(client, "is_simulation", False):
                raise NotASimulation(
                    f"{venue.value} client does not declare is_simulation; refusing "
                    "to run a pair against it"
                )
        self._mode = mode
        self._funding = funding
        self._clients = dict(clients)
        self._remedy: RemedyPolicy = remedy or UnwindThenEscalate()

    # --------------------------------------------------------------------- run
    def run(
        self,
        candidate: PairCandidate,
        *,
        recheck: RecheckFn | None = None,
    ) -> PairJournal:
        """Take one candidate as far as it legitimately goes, and record why it stopped."""
        journal = PairJournal(
            pair_id=candidate.pair_id,
            mode=self._mode,
            extra_cost_usd=candidate.extra_cost_usd,
        )

        if not self._validate(candidate, journal):
            return journal
        if not self._reserve(candidate, journal):
            return journal
        if not self._recheck(candidate, journal, recheck):
            return journal

        journal.transition(
            PairState.SUBMITTING, "both legs funded and revalidated; sending orders"
        )
        self._submit_legs(candidate, journal)
        self._classify(candidate, journal)
        if journal.state is PairState.UNKNOWN:
            self._resolve_unknown(journal)
        self._reconcile_legs(candidate, journal)
        if journal.state in (PairState.PARTIALLY_FILLED, PairState.ONE_LEG_FILLED, PairState.UNKNOWN):
            self._apply_remedy(candidate, journal)
        self._settle_funding(journal)
        self._score_realised(candidate, journal)
        self._finish(journal)
        return journal

    def recover(self, candidate: PairCandidate) -> PairJournal:
        """Rebuild a pair's state after a crash, without sending anything.

        The derived ``client_order_id`` is the whole mechanism: it depends only on the
        candidate and the leg, so a restarted process asks the venue about the *same*
        order the dead process submitted. Recovery is therefore a read, and a process
        that dies between the two submissions cannot come back and open a third
        position.
        """
        journal = PairJournal(
            pair_id=candidate.pair_id,
            mode=self._mode,
            extra_cost_usd=candidate.extra_cost_usd,
        )
        journal.transition(PairState.VALIDATED, "recovering a pair from a prior process")
        journal.transition(PairState.FUNDS_RESERVED, "reservations restored from the ledger")
        journal.transition(PairState.RECHECKED, "no recheck on recovery; nothing new is sent")
        journal.transition(PairState.SUBMITTING, "querying the venues for prior orders")

        for spec in candidate.legs:
            record = LegRecord(
                label=candidate.leg_label(spec),
                venue=spec.contract.venue,
                contract_key=spec.contract.contract_key,
                client_order_id=derive_pair_client_order_id(candidate, spec),
                requested_quantity=spec.quantity,
                requested_price=spec.limit_price,
                reservation_ref=f"{candidate.pair_id}:{spec.contract.venue.value}",
            )
            record.notes.append("recovered by query; no order was submitted")
            journal.legs.append(record)
            client = self._clients.get(spec.contract.venue)
            if client is None:
                record.status = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
                record.notes.append("no client for this venue; state is unknown")
                continue
            self._confirm_leg(record, client)

        self._classify(candidate, journal)
        if journal.state is PairState.UNKNOWN:
            self._resolve_unknown(journal)
        if journal.state in (
            PairState.PARTIALLY_FILLED,
            PairState.ONE_LEG_FILLED,
            PairState.UNKNOWN,
        ):
            self._apply_remedy(candidate, journal)
        self._score_realised(candidate, journal)
        self._finish(journal)
        return journal

    # ---------------------------------------------------------------- validate
    def _validate(self, candidate: PairCandidate, journal: PairJournal) -> bool:
        assessment = journal.assessment
        assessment.quote_status = candidate.quote_status
        assessment.access_status = candidate.access_status

        problems: list[str] = []
        for spec in candidate.legs:
            for violation in spec.violations():
                problems.append(f"{candidate.leg_label(spec)}: {violation}")

        # Equal settlement shares, not equal dollars. Two legs sized to the same
        # notional are not a hedge: the cheaper side buys more shares and the pair is
        # long whichever outcome that side pays on.
        if candidate.leg_a.quantity != candidate.leg_b.quantity:
            problems.append(
                f"legs request unequal settlement shares "
                f"({candidate.leg_a.quantity} vs {candidate.leg_b.quantity}); the "
                "complementary structure requires the same number on both sides"
            )

        planned_cost = ZERO
        try:
            planned_legs = [
                HeldLeg(
                    contract_key=spec.contract.contract_key,
                    side=spec.contract.outcome.to_side(),
                    quantity=spec.quantity,
                    price_per_unit=spec.limit_price,
                    fees_usd=spec.max_fee_usd,
                )
                for spec in candidate.legs
            ]
            planned_cost = sum((leg.cost_usd for leg in planned_legs), ZERO)
        except Exception as exc:  # UnknownFeeError, and anything else the fee model raises
            problems.append(f"cost could not be bounded: {exc}")
            planned_legs = []

        if planned_legs:
            journal.planned_payoff = evaluate_payoff(
                planned_legs,
                candidate.rules,
                candidate.linkages,
                extra_cost_usd=candidate.extra_cost_usd,
            )
            assessment.structural_status = journal.planned_payoff.structural_status
            net = journal.planned_payoff.worst_case_net_usd
            if net is None:
                assessment.economics_status = EconomicsStatus.UNKNOWN_COSTS
            elif net > ZERO:
                assessment.economics_status = EconomicsStatus.NET_POSITIVE
            else:
                assessment.economics_status = EconomicsStatus.NET_NEGATIVE
            journal.residual_risks.extend(journal.planned_payoff.assumptions)
            journal.residual_risks.extend(journal.planned_payoff.ignored_linkages)
        else:
            assessment.structural_status = StructuralStatus.UNKNOWN
            assessment.economics_status = EconomicsStatus.UNKNOWN_COSTS

        assessment.residual_risks = list(journal.residual_risks)

        if problems:
            journal.abandon_reason = "; ".join(problems)
            journal.transition(PairState.ABANDONED, journal.abandon_reason)
            return False
        if not assessment.tradeable:
            journal.abandon_reason = (
                "candidate is not tradeable: " + ", ".join(assessment.blocking_reasons())
            )
            journal.transition(PairState.ABANDONED, journal.abandon_reason)
            return False

        assert journal.planned_payoff is not None
        journal.transition(
            PairState.VALIDATED,
            f"every reachable settlement state nets at least "
            f"${quantize_usd(journal.planned_payoff.worst_case_net_usd or ZERO)} on "
            f"${quantize_usd(planned_cost + candidate.extra_cost_usd)} committed",
        )
        return True

    # ----------------------------------------------------------------- funding
    def _reserve(self, candidate: PairCandidate, journal: PairJournal) -> bool:
        requests = []
        for spec in candidate.legs:
            client_order_id = derive_pair_client_order_id(candidate, spec)
            record = LegRecord(
                label=candidate.leg_label(spec),
                venue=spec.contract.venue,
                contract_key=spec.contract.contract_key,
                client_order_id=client_order_id,
                requested_quantity=spec.quantity,
                requested_price=spec.limit_price,
                reservation_ref=f"{candidate.pair_id}:{spec.contract.venue.value}",
            )
            journal.legs.append(record)
            requests.append(
                (record.reservation_ref, spec.contract.venue, spec.max_cash_out_usd)
            )

        outcome = self._funding.reserve_all(requests)
        if not outcome.approved:
            journal.abandon_reason = "funding refused: " + "; ".join(outcome.reasons)
            journal.transition(PairState.ABANDONED, journal.abandon_reason)
            return False
        journal.transition(
            PairState.FUNDS_RESERVED,
            "reserved "
            + ", ".join(
                f"${quantize_usd(amount)} on {venue.value}"
                for _ref, venue, amount in requests
            ),
        )
        return True

    # ----------------------------------------------------------------- recheck
    def _recheck(
        self,
        candidate: PairCandidate,
        journal: PairJournal,
        recheck: RecheckFn | None,
    ) -> bool:
        """Re-confirm the opportunity after funding, before anything is sent.

        Time passes between detecting a candidate and being ready to submit it, and
        the book does not wait. Skipping this step means submitting against prices
        that were true when the scan ran, which on a fast-moving pair is most of the
        reason a leg fills alone.
        """
        ok, reason = (True, "no recheck supplied") if recheck is None else recheck(candidate)
        if not ok:
            for leg in journal.legs:
                self._funding.release(leg.reservation_ref)
            journal.abandon_reason = f"recheck failed: {reason}"
            journal.transition(PairState.ABANDONED, journal.abandon_reason)
            return False
        journal.transition(PairState.RECHECKED, reason)
        return True

    # ------------------------------------------------------------------ submit
    def _submit_legs(self, candidate: PairCandidate, journal: PairJournal) -> None:
        from .idempotency import new_attempt_id

        for spec in candidate.legs:
            record = journal.leg(candidate.leg_label(spec))
            assert record is not None
            client = self._clients.get(spec.contract.venue)
            if client is None:
                record.status = OrderStatus.REJECTED
                record.notes.append("no client configured for this venue")
                continue

            attempt = new_attempt_id()
            record.attempts.append(attempt)
            record.status = OrderStatus.SUBMITTING
            try:
                ack = client.submit(
                    spec,
                    client_order_id=record.client_order_id,
                    attempt_id=attempt,
                )
            except LegTimeout as exc:
                # The request may or may not have landed. Ask; never resend.
                record.status = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
                record.notes.append(
                    f"submit timed out ({exc}); status is unknown, querying by "
                    "client_order_id rather than resubmitting"
                )
                continue

            if not ack.accepted:
                record.status = OrderStatus.REJECTED
                record.notes.append(f"venue rejected: {ack.reject_reason}")
                continue

            record.venue_order_id = ack.venue_order_id
            record.notes.append(
                "venue acknowledged the request; this is not evidence of a fill"
            )
            self._confirm_leg(record, client)

    def _confirm_leg(self, record: LegRecord, client: SimulatedVenueClient) -> None:
        """Read the venue's own record. The only place fills enter the journal."""
        from .idempotency import new_attempt_id

        attempt = new_attempt_id()
        record.attempts.append(attempt)
        try:
            report = client.confirm(client_order_id=record.client_order_id)
        except LegTimeout as exc:
            record.status = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            record.notes.append(f"confirm timed out ({exc}); status remains unknown")
            return
        self._absorb(record, report)

    def _absorb(self, record: LegRecord, report: LegReport) -> None:
        record.status = report.status
        record.filled_quantity = report.filled_quantity
        record.average_price = report.average_price
        record.fees_usd = report.fees_usd
        if report.venue_order_id:
            record.venue_order_id = report.venue_order_id
        if report.is_filled_at_all and report.fees_usd is None:
            record.notes.append(
                "venue reported a fill with no fee; cost is understated by an "
                "unknown amount"
            )

    # ---------------------------------------------------------------- classify
    def _classify(self, candidate: PairCandidate, journal: PairJournal) -> None:
        if any(
            leg.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
            for leg in journal.legs
        ):
            journal.transition(
                PairState.UNKNOWN,
                "at least one leg's outcome was never obtained from the venue",
            )
            return
        journal.transition(*self._outcome_for(candidate, journal))

    def _outcome_for(
        self, candidate: PairCandidate, journal: PairJournal
    ) -> tuple[PairState, str]:
        filled = [leg.filled_quantity for leg in journal.legs]
        target = candidate.target_quantity
        if all(q >= target and q > ZERO for q in filled):
            return PairState.BOTH_FILLED, f"both legs filled {target} settlement shares"
        if all(q > ZERO for q in filled):
            return (
                PairState.PARTIALLY_FILLED,
                "both legs filled, but at different sizes: "
                + ", ".join(
                    f"{leg.label}={leg.filled_quantity}" for leg in journal.legs
                ),
            )
        if any(q > ZERO for q in filled):
            exposed = next(leg for leg in journal.legs if leg.filled_quantity > ZERO)
            return (
                PairState.ONE_LEG_FILLED,
                f"only {exposed.label} filled ({exposed.filled_quantity}); the "
                "position is directional, not hedged",
            )
        return PairState.NO_FILL, "neither leg filled; no position was taken"

    # ----------------------------------------------------------------- unknown
    def _resolve_unknown(self, journal: PairJournal) -> None:
        """Query every unresolved leg once more, by its idempotency key.

        Never resubmits. If the venue still will not say, the pair escalates: a
        position that might exist is a human's problem, and guessing either way is
        how it becomes an expensive one.
        """
        for record in journal.legs:
            if record.status is not OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED:
                continue
            client = self._clients.get(record.venue)
            if client is None:
                continue
            self._confirm_leg(record, client)

        still_unknown = [
            leg.label
            for leg in journal.legs
            if leg.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
        ]
        if still_unknown:
            journal.escalation_reason = (
                "unresolved after re-querying: "
                + ", ".join(still_unknown)
                + "; a position may or may not exist and must not be guessed"
            )
            journal.transition(PairState.ESCALATED, journal.escalation_reason)
            return

        journal.transition(
            *self._outcome_for_unknown_resolution(journal)
        )

    def _outcome_for_unknown_resolution(
        self, journal: PairJournal
    ) -> tuple[PairState, str]:
        filled = [leg.filled_quantity for leg in journal.legs]
        requested = [leg.requested_quantity for leg in journal.legs]
        if all(f >= r and f > ZERO for f, r in zip(filled, requested)):
            return (
                PairState.BOTH_FILLED,
                "re-query resolved every leg as filled; the earlier timeout was a "
                "transport failure, not a trading failure",
            )
        if all(f > ZERO for f in filled):
            return PairState.PARTIALLY_FILLED, "re-query found unequal fills"
        if any(f > ZERO for f in filled):
            return PairState.ONE_LEG_FILLED, "re-query found a single filled leg"
        return PairState.NO_FILL, "re-query confirmed no leg filled"

    # ------------------------------------------------------------- reconciliation
    def _reconcile_legs(self, candidate: PairCandidate, journal: PairJournal) -> None:
        """Re-read every leg from the venue and believe the venue over the journal.

        This is what catches the cancel/fill race. A first read that says "cancelled"
        while a fill is still in flight is not a lie, it is a read taken too early -
        and code that treats the first read as final ends up holding a position it
        has recorded as closed. So every leg is read again at the end of the attempt,
        and any disagreement sends the pair back through ``UNKNOWN`` to be
        re-classified rather than being patched in place.
        """
        if journal.state in TERMINAL_PAIR_STATES or journal.state is PairState.ESCALATED:
            return

        changed: list[str] = []
        for spec in candidate.legs:
            record = journal.leg(candidate.leg_label(spec))
            if record is None:
                continue
            client = self._clients.get(record.venue)
            if client is None:
                continue
            try:
                report = client.confirm(client_order_id=record.client_order_id)
            except LegTimeout as exc:
                record.notes.append(
                    f"reconciliation read timed out ({exc}); the journal's figure is "
                    "unconfirmed"
                )
                record.status = OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED
                changed.append(f"{record.label}: could not be re-read")
                continue
            if report.filled_quantity != record.filled_quantity:
                changed.append(
                    f"{record.label}: journal said {record.filled_quantity}, venue "
                    f"says {report.filled_quantity}"
                )
                record.notes.append(
                    "reconciliation corrected the fill from "
                    f"{record.filled_quantity} to {report.filled_quantity}"
                )
                self._absorb(record, report)

        if not changed:
            return

        journal.residual_risks.extend(changed)
        journal.transition(
            PairState.UNKNOWN,
            "venue records disagree with the journal: " + "; ".join(changed),
        )
        self._resolve_unknown(journal)

    # ------------------------------------------------------------------ remedy
    def _apply_remedy(self, candidate: PairCandidate, journal: PairJournal) -> None:
        decision = self._remedy.decide(journal)
        if decision.action is RemedyAction.ESCALATE:
            journal.escalation_reason = decision.reason
            journal.transition(PairState.ESCALATED, decision.reason)
            return

        journal.transition(PairState.UNWINDING, decision.reason)
        matched = min((leg.filled_quantity for leg in journal.legs), default=ZERO)
        failures: list[str] = []
        for spec in candidate.legs:
            record = journal.leg(candidate.leg_label(spec))
            assert record is not None
            excess = record.filled_quantity - matched
            if excess <= ZERO:
                continue
            client = self._clients.get(record.venue)
            if client is None:
                failures.append(f"{record.label}: no client to close through")
                continue
            try:
                report = client.close_out(
                    spec, excess, client_order_id=f"{record.client_order_id}-close"
                )
            except LegTimeout as exc:
                failures.append(f"{record.label}: close timed out ({exc})")
                continue
            if report.filled_quantity < excess:
                failures.append(
                    f"{record.label}: only {report.filled_quantity} of {excess} "
                    "could be closed"
                )
            record.closed_quantity = report.filled_quantity
            record.close_proceeds_usd = (
                report.average_price or ZERO
            ) * report.filled_quantity
            record.close_fees_usd = report.fees_usd or ZERO
            record.notes.append(
                f"closed {report.filled_quantity} unmatched units at "
                f"{report.average_price}"
            )

        if failures:
            journal.escalation_reason = "; ".join(failures)
            journal.transition(PairState.ESCALATED, journal.escalation_reason)
            return
        journal.transition(
            PairState.HEDGED,
            f"unmatched quantity closed at a cost of "
            f"${quantize_usd(journal.remedy_cost_usd)}; {journal.matched_quantity} "
            "settlement shares remain hedged",
        )

    # ----------------------------------------------------------------- funding
    def _settle_funding(self, journal: PairJournal) -> None:
        """Turn every hold into a commit or a release. No hold may survive the run."""
        for leg in journal.legs:
            if not leg.reservation_ref:
                continue
            reservation = self._funding.reservation(leg.reservation_ref)
            if reservation is None or reservation.state.value != "held":
                continue
            if leg.status is OrderStatus.UNKNOWN_RECONCILIATION_REQUIRED:
                # Money that may or may not have been spent stays held. Releasing it
                # would let another candidate spend the same dollars.
                leg.notes.append(
                    "reservation stays held: the leg is unresolved, so the cash is "
                    "neither confirmed spent nor confirmed available"
                )
                continue
            if leg.filled_quantity > ZERO:
                self._funding.commit(leg.reservation_ref, leg.net_cash_out_usd)
            else:
                self._funding.release(leg.reservation_ref)

    # ---------------------------------------------------------------- realised
    def _score_realised(self, candidate: PairCandidate, journal: PairJournal) -> None:
        """Re-run the settlement evaluator against what is actually held.

        This is the step that makes a one-legged outcome impossible to describe as an
        arbitrage: the evaluator sees a single leg, enumerates the state where that
        leg pays nothing, and returns ``NOT_GUARANTEED``. No status field upstream
        can override it, because none of them are inputs here.
        """
        held: list[HeldLeg] = []
        for spec in candidate.legs:
            record = journal.leg(candidate.leg_label(spec))
            if record is None or record.retained_quantity <= ZERO:
                continue
            retained = record.retained_quantity
            # Fees are apportioned to the retained units. The fee on units that were
            # unwound belongs to the unwind cost, not to the position that remains.
            share = safe_div(retained, record.filled_quantity)
            held.append(
                HeldLeg(
                    contract_key=record.contract_key,
                    side=spec.contract.outcome.to_side(),
                    quantity=retained,
                    price_per_unit=record.average_price or ZERO,
                    fees_usd=(record.fees_usd or ZERO) * share,
                )
            )

        extra = candidate.extra_cost_usd + journal.remedy_cost_usd
        journal.realised_payoff = evaluate_payoff(
            held, candidate.rules, candidate.linkages, extra_cost_usd=extra
        )
        journal.assessment.structural_status = journal.realised_payoff.structural_status

        if journal.state in (PairState.BOTH_FILLED,):
            journal.assessment.execution_status = ExecutionStatus.MATCHED
        elif journal.state is PairState.HEDGED:
            journal.assessment.execution_status = ExecutionStatus.NEUTRALISED
        elif journal.state is PairState.ESCALATED:
            journal.assessment.execution_status = ExecutionStatus.UNRESOLVED
        elif journal.state is PairState.NO_FILL:
            journal.assessment.execution_status = ExecutionStatus.FAILED
        else:
            journal.assessment.execution_status = ExecutionStatus.UNMATCHED
        journal.assessment.residual_risks = list(journal.residual_risks) + list(
            journal.realised_payoff.assumptions
        )

    # ------------------------------------------------------------------ finish
    def _finish(self, journal: PairJournal) -> None:
        if journal.state in TERMINAL_PAIR_STATES:
            return
        if journal.state is PairState.ESCALATED:
            # Escalations are closed by a human acknowledging them, not by the loop
            # that created them. Left open deliberately.
            return
        journal.transition(
            PairState.RECONCILED,
            f"every leg accounted for; ${quantize_usd(journal.total_cash_out_usd)} "
            f"spent against {journal.matched_quantity} hedged shares",
        )
