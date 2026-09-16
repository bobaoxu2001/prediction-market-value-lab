"""Enumerate what a basket of legs pays in every settlement state it can reach.

The question this module answers is narrow and mechanical:

    Given these legs, bought at these prices, under these published contract rules,
    what is the *smallest* total payout across the settlement states that have not
    been proven impossible?

Call that ``P_min``. If ``P_min`` exceeds the money already spent, the position
cannot lose, and :attr:`StructuralStatus.GUARANTEED` is earned. If some reachable
state pays less than was spent, the position can lose and the status says so, naming
the state. If the state space could not be enumerated at all, the status is
``UNKNOWN`` - which is not the same as ``NOT_GUARANTEED`` and must not be collapsed
into it.

Three design rules, each of which exists because the opposite produced a wrong
answer somewhere:

**The evaluator never sees the signal.** Its inputs are rules and fills. There is no
parameter through which a caller can assert that a pair is safe.

**An unknown term is bounded, not assumed away.** A contract whose void payout is not
published is scored at $0 for that state - the worst it can be. A bound is still a
proof: if the position clears while assuming the worst, it clears. What is forbidden
is the reverse, treating unknown as benign.

**Pruning requires evidence about the *right* term.** Two markets can be proven to
ask the identical question and still void differently, because "same question" and
"same cancellation policy" are different claims sourced from different sentences.
A linkage therefore has to say explicitly whether it covers void handling; one that
does not leaves the divergent-void states in the space, where they belong.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.enums import Platform, SettlementResult, Side
from pmvl_shared.money import ONE, ZERO, quantize_usd, safe_div

#: The payout of one settled contract unit, for the two states that are not
#: venue-specific. Buying the side that comes true pays $1; the other side pays $0.
_FULL = ONE
_NONE = ZERO
#: A contract split down the middle pays both sides half.
_HALF = Decimal("0.5")


class VoidPayout(StrEnum):
    """What one unit of a contract pays if the contract is voided or cancelled.

    ``UNKNOWN`` is the default on purpose. Neither Kalshi nor Polymarket publishes
    void handling as a structured field, so a rule object that was built without
    someone reading the cancellation sentence should say it does not know, and be
    scored at the worst case.
    """

    #: The contract price comes back; the trading fee does not. This is the usual
    #: shape, and it is why a pair of voidable contracts is never a guaranteed
    #: *profit*: the both-void state returns the stakes and keeps the fees.
    REFUND_COST = "refund_cost"
    #: Price and fee both returned. Only set this where the venue documents it.
    REFUND_TOTAL = "refund_total"
    HALF = "half"                    # resolved 50-50; both sides receive $0.50
    ZERO = "zero"                    # the stake is lost
    UNKNOWN = "unknown"              # not published; bounded at $0 when evaluating


class LinkageRelation(StrEnum):
    """How two contracts' settlement results are claimed to relate."""

    #: Both contracts settle to the same result: YES here means YES there.
    IDENTICAL = "identical"
    #: Inverted mapping: YES here means NO there. Used when one venue words the
    #: question as the negation of the other's.
    COMPLEMENTARY = "complementary"


class LinkageEvidence(StrEnum):
    """How well a claimed linkage is supported.

    Only :attr:`PROVEN` prunes the scenario space. Anything weaker is recorded and
    ignored, so a hopeful match cannot quietly delete the states where it is wrong.
    """

    PROVEN = "proven"          # both rule texts compared term by term, terms agree
    ASSERTED = "asserted"      # claimed by a matcher (title, embedding, LLM) only
    NONE = "none"


class StructuralStatus(StrEnum):
    """Whether the *structure* of the position can lose money.

    Deliberately says nothing about quote freshness, fill quality, or venue access.
    Those are tracked separately and are not permitted to upgrade this field.

    ``NO_LOSS`` exists because "cannot lose" and "must profit" are different claims
    and collapsing them costs information in both directions. A pair whose worst
    reachable state is an exact refund is riskless and pointless; reporting it as
    ``NOT_GUARANTEED`` hides that it is riskless, and reporting it as ``GUARANTEED``
    would let a zero-edge trade through the gate.
    """

    GUARANTEED = "guaranteed"
    NO_LOSS = "no_loss"
    NOT_GUARANTEED = "not_guaranteed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContractRule:
    """The settlement terms of one venue contract, as published by that venue.

    ``event_key`` names the real-world question. Two rules sharing an ``event_key``
    are *claimed* to be about the same thing; whether that claim prunes anything is
    decided by the linkages, not by the key.
    """

    contract_key: str
    venue: Platform
    event_key: str
    #: Terminal states this contract can reach. ``YES``/``NO`` are always possible.
    #: Include ``VOID`` or ``FIFTY_FIFTY`` when the rule text admits them - or when
    #: it is silent, since silence does not prove they cannot happen.
    #:
    #: ``can_void=False`` is a claim about the *rules*: that they define an outcome
    #: for every state of the world, including the event simply not occurring
    #: ("resolves No if the game is not played by..."). It is not a claim that the
    #: venue can never administratively void a market created in error. That
    #: residual is real, unquantifiable from the rule text, and reported as an
    #: assumption rather than modelled - see :attr:`PayoffReport.assumptions`.
    can_void: bool = True
    can_fifty_fifty: bool = False
    void_payout: VoidPayout = VoidPayout.UNKNOWN
    #: Free text the operator can trace back to the published rule.
    rule_source: str = ""
    rule_version: str = ""

    def possible_results(self) -> tuple[SettlementResult, ...]:
        results: list[SettlementResult] = [SettlementResult.YES, SettlementResult.NO]
        if self.can_void:
            results.append(SettlementResult.VOID)
        if self.can_fifty_fifty:
            results.append(SettlementResult.FIFTY_FIFTY)
        return tuple(results)


@dataclass(frozen=True)
class HeldLeg:
    """One filled position: how much of which side of which contract, at what price.

    Price and fees are kept apart rather than folded into a single cost, because a
    voided contract refunds the *price* and keeps the fee. Storing only a blended
    cost would silently refund the fees too and overstate the void payout by exactly
    the amount the venue keeps.

    These are figures from actual fills, not from a quote.
    """

    contract_key: str
    side: Side
    quantity: Decimal
    #: Contract price actually paid per unit, fees excluded.
    price_per_unit: Decimal
    #: Fees paid on this leg. Sunk: not returned when the contract voids.
    fees_usd: Decimal = ZERO

    @property
    def cost_usd(self) -> Decimal:
        return self.price_per_unit * self.quantity + self.fees_usd


@dataclass(frozen=True)
class SettlementLinkage:
    """A claim that two contracts' results move together, and how well it is backed.

    ``covers_void`` is the field that matters most and is easiest to get wrong. A
    linkage proven from threshold, comparator, cutoff and data source says the two
    contracts answer the same question. It says nothing about what either venue does
    when the underlying event is cancelled, because that lives in a different clause
    that most rule texts omit. Setting ``covers_void=True`` without having read that
    clause on *both* venues is how a pair that loses on both legs gets certified.
    """

    contract_a: str
    contract_b: str
    relation: LinkageRelation = LinkageRelation.IDENTICAL
    evidence: LinkageEvidence = LinkageEvidence.NONE
    covers_void: bool = False
    note: str = ""

    @property
    def prunes(self) -> bool:
        return self.evidence is LinkageEvidence.PROVEN


@dataclass(frozen=True)
class ScenarioPayoff:
    """One joint settlement state and what the whole basket pays in it."""

    #: contract_key -> the result that contract reached in this scenario.
    results: tuple[tuple[str, SettlementResult], ...]
    payout_usd: Decimal
    #: Legs whose payout in this scenario is a conservative bound, not a known value.
    bounded_legs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "results": {k: v.value for k, v in self.results},
            "payout_usd": str(quantize_usd(self.payout_usd)),
            "bounded_legs": list(self.bounded_legs),
        }


@dataclass
class PayoffReport:
    """The verdict, plus everything needed to argue with it."""

    structural_status: StructuralStatus
    total_cost_usd: Decimal
    min_payout_usd: Decimal | None
    #: ``min_payout - total_cost``. Negative means a reachable state loses money.
    worst_case_net_usd: Decimal | None
    #: True only when every joint state was enumerated. False when the caller
    #: supplied a sample, or when enumeration was abandoned because it was too large.
    exhaustive: bool
    scenarios_considered: int
    scenarios_pruned: int
    worst_scenario: ScenarioPayoff | None = None
    reasons: list[str] = field(default_factory=list)
    #: Conservative substitutions made for terms the rules did not publish.
    assumptions: list[str] = field(default_factory=list)
    #: Linkages that were claimed but not strong enough to prune anything.
    ignored_linkages: list[str] = field(default_factory=list)

    @property
    def guaranteed(self) -> bool:
        """True only for a proven *profit*. ``NO_LOSS`` is not enough to trade on."""
        return self.structural_status is StructuralStatus.GUARANTEED

    @property
    def can_lose(self) -> bool:
        """True when a reachable state pays less than was spent, or is unknown.

        ``UNKNOWN`` answers True: an unenumerated space is not evidence of safety.
        """
        return self.structural_status in (
            StructuralStatus.NOT_GUARANTEED,
            StructuralStatus.UNKNOWN,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "structural_status": self.structural_status.value,
            "total_cost_usd": str(quantize_usd(self.total_cost_usd)),
            "min_payout_usd": (
                str(quantize_usd(self.min_payout_usd))
                if self.min_payout_usd is not None
                else None
            ),
            "worst_case_net_usd": (
                str(quantize_usd(self.worst_case_net_usd))
                if self.worst_case_net_usd is not None
                else None
            ),
            "exhaustive": self.exhaustive,
            "scenarios_considered": self.scenarios_considered,
            "scenarios_pruned": self.scenarios_pruned,
            "worst_scenario": self.worst_scenario.as_dict() if self.worst_scenario else None,
            "reasons": list(self.reasons),
            "assumptions": list(self.assumptions),
            "ignored_linkages": list(self.ignored_linkages),
        }


#: Above this many joint states, enumeration is abandoned rather than run. A basket
#: this wide is not the two-leg trade this round is about, and silently sampling it
#: would produce a ``min`` over an arbitrary subset that reads like a proof.
_MAX_SCENARIOS = 4096


def evaluate_payoff(
    legs: list[HeldLeg],
    rules: dict[str, ContractRule],
    linkages: list[SettlementLinkage] | None = None,
    *,
    extra_cost_usd: Decimal = ZERO,
) -> PayoffReport:
    """Score a basket over its full settlement state space.

    ``extra_cost_usd`` covers money spent outside the legs themselves - bridge fees,
    withdrawal costs - so the comparison is against total cash out, not just fills.
    """
    linkages = list(linkages or [])
    total_cost = sum((leg.cost_usd for leg in legs), ZERO) + extra_cost_usd

    if not legs:
        return PayoffReport(
            structural_status=StructuralStatus.UNKNOWN,
            total_cost_usd=total_cost,
            min_payout_usd=None,
            worst_case_net_usd=None,
            exhaustive=False,
            scenarios_considered=0,
            scenarios_pruned=0,
            reasons=["no legs supplied; there is nothing to evaluate"],
        )

    missing = sorted({leg.contract_key for leg in legs} - set(rules))
    if missing:
        return PayoffReport(
            structural_status=StructuralStatus.UNKNOWN,
            total_cost_usd=total_cost,
            min_payout_usd=None,
            worst_case_net_usd=None,
            exhaustive=False,
            scenarios_considered=0,
            scenarios_pruned=0,
            reasons=[
                "no settlement rule supplied for "
                + ", ".join(missing)
                + "; the state space cannot be enumerated"
            ],
        )

    # One axis per distinct contract, not per leg: two legs on the same contract
    # settle together and must not be varied independently.
    contract_keys = sorted({leg.contract_key for leg in legs})
    axes = [rules[key].possible_results() for key in contract_keys]

    size = 1
    for axis in axes:
        size *= len(axis)
    if size > _MAX_SCENARIOS:
        return PayoffReport(
            structural_status=StructuralStatus.UNKNOWN,
            total_cost_usd=total_cost,
            min_payout_usd=None,
            worst_case_net_usd=None,
            exhaustive=False,
            scenarios_considered=0,
            scenarios_pruned=0,
            reasons=[
                f"state space of {size} joint outcomes exceeds the {_MAX_SCENARIOS} "
                "enumeration limit; no exhaustive claim is made"
            ],
        )

    pruning, ignored = _partition_linkages(linkages, contract_keys)
    pruning, demoted = _demote_contradicted_void_claims(pruning, rules)
    ignored.extend(demoted)

    assumptions: list[str] = []
    for key in contract_keys:
        rule = rules[key]
        if rule.can_void and rule.void_payout is VoidPayout.UNKNOWN:
            assumptions.append(
                f"{key}: void payout is not published; bounded at $0 per unit"
            )
        if not rule.can_void:
            assumptions.append(
                f"{key}: the rules define an outcome for every state, so no void "
                "state was enumerated; an administrative void by the venue is "
                "outside this model"
            )

    legs_by_contract: dict[str, list[HeldLeg]] = {}
    for leg in legs:
        legs_by_contract.setdefault(leg.contract_key, []).append(leg)

    considered: list[ScenarioPayoff] = []
    pruned = 0
    for combo in itertools.product(*axes):
        assignment = dict(zip(contract_keys, combo))
        if _contradicts_linkage(assignment, pruning):
            pruned += 1
            continue
        payout = ZERO
        bounded: list[str] = []
        for key, result in assignment.items():
            rule = rules[key]
            for leg in legs_by_contract[key]:
                per_unit, is_bound = _leg_unit_payout(leg, rule, result)
                payout += per_unit * leg.quantity
                if is_bound:
                    bounded.append(f"{key}:{leg.side.value}")
        considered.append(
            ScenarioPayoff(
                results=tuple(sorted(assignment.items())),
                payout_usd=payout,
                bounded_legs=tuple(bounded),
            )
        )

    if not considered:
        # Every joint state was pruned. That means the linkages contradict each other
        # - e.g. A identical to B and A complementary to B - and the position has no
        # reachable settlement at all, which is a bug in the inputs, not a proof.
        return PayoffReport(
            structural_status=StructuralStatus.UNKNOWN,
            total_cost_usd=total_cost,
            min_payout_usd=None,
            worst_case_net_usd=None,
            exhaustive=False,
            scenarios_considered=0,
            scenarios_pruned=pruned,
            reasons=["linkages pruned every settlement state; they are inconsistent"],
            assumptions=assumptions,
            ignored_linkages=ignored,
        )

    worst = min(considered, key=lambda s: s.payout_usd)
    net = worst.payout_usd - total_cost
    if net > ZERO:
        status = StructuralStatus.GUARANTEED
    elif net == ZERO:
        status = StructuralStatus.NO_LOSS
    else:
        status = StructuralStatus.NOT_GUARANTEED

    reasons: list[str] = []
    if status is StructuralStatus.NO_LOSS:
        detail = ", ".join(f"{k}={v.value}" for k, v in worst.results)
        reasons.append(
            f"worst reachable state ({detail}) returns exactly the "
            f"${quantize_usd(total_cost)} spent: riskless, but no edge"
        )
    if status is StructuralStatus.NOT_GUARANTEED:
        detail = ", ".join(f"{k}={v.value}" for k, v in worst.results)
        reasons.append(
            f"worst reachable state ({detail}) pays "
            f"${quantize_usd(worst.payout_usd)} against ${quantize_usd(total_cost)} spent"
        )
        if worst.bounded_legs:
            reasons.append(
                "that state's payout is a conservative bound for "
                + ", ".join(sorted(set(worst.bounded_legs)))
                + "; publishing the void terms could change it"
            )
    if ignored:
        reasons.append(
            f"{len(ignored)} claimed linkage(s) were not proven and pruned nothing"
        )

    return PayoffReport(
        structural_status=status,
        total_cost_usd=total_cost,
        min_payout_usd=worst.payout_usd,
        worst_case_net_usd=net,
        exhaustive=True,
        scenarios_considered=len(considered),
        scenarios_pruned=pruned,
        worst_scenario=worst,
        reasons=reasons,
        assumptions=assumptions,
        ignored_linkages=ignored,
    )


def _partition_linkages(
    linkages: list[SettlementLinkage], contract_keys: list[str]
) -> tuple[list[SettlementLinkage], list[str]]:
    """Split linkages into the ones that may prune and the ones merely recorded."""
    known = set(contract_keys)
    pruning: list[SettlementLinkage] = []
    ignored: list[str] = []
    for link in linkages:
        if link.contract_a not in known or link.contract_b not in known:
            ignored.append(
                f"{link.contract_a}~{link.contract_b}: names a contract not in the basket"
            )
            continue
        if not link.prunes:
            ignored.append(
                f"{link.contract_a}~{link.contract_b}: evidence is "
                f"{link.evidence.value}, not proven"
            )
            continue
        pruning.append(link)
    return pruning, ignored


def _demote_contradicted_void_claims(
    linkages: list[SettlementLinkage], rules: dict[str, ContractRule]
) -> tuple[list[SettlementLinkage], list[str]]:
    """Strip ``covers_void`` from any linkage the rules themselves contradict.

    A linkage claiming that two contracts handle cancellation identically is refuted
    by its own inputs if the two rules admit *different* non-binary outcomes - one
    that can resolve 50-50 against one that cannot, say. Believing the claim over the
    rules would prune the states where exactly that asymmetry costs money, which is
    the one thing a settlement evaluator must never do.

    The linkage is not discarded: its YES/NO pruning still holds, because that part
    was proven from the question, not from the cancellation clause. Only the void
    claim is withdrawn, and the withdrawal is reported.
    """
    kept: list[SettlementLinkage] = []
    demoted: list[str] = []
    for link in linkages:
        if not link.covers_void:
            kept.append(link)
            continue
        undecided_a = set(rules[link.contract_a].possible_results()) - {
            SettlementResult.YES,
            SettlementResult.NO,
        }
        undecided_b = set(rules[link.contract_b].possible_results()) - {
            SettlementResult.YES,
            SettlementResult.NO,
        }
        if undecided_a == undecided_b:
            kept.append(link)
            continue
        kept.append(
            SettlementLinkage(
                contract_a=link.contract_a,
                contract_b=link.contract_b,
                relation=link.relation,
                evidence=link.evidence,
                covers_void=False,
                note=link.note,
            )
        )
        demoted.append(
            f"{link.contract_a}~{link.contract_b}: claims matched void handling, but "
            f"the rules admit different non-binary outcomes "
            f"({sorted(r.value for r in undecided_a) or 'none'} vs "
            f"{sorted(r.value for r in undecided_b) or 'none'}); the void claim was "
            "withdrawn and those states left reachable"
        )
    return kept, demoted


def _contradicts_linkage(
    assignment: dict[str, SettlementResult], linkages: list[SettlementLinkage]
) -> bool:
    """True when this joint state is ruled out by a proven linkage.

    Only the YES/NO axis is ever pruned, and only when the linkage says it covers
    void handling do the mixed void states get pruned too. A linkage that proves the
    two contracts ask the same question still leaves ``(VOID, YES)`` reachable,
    because a venue voiding while the other settles is precisely the divergence the
    question-level proof does not address.
    """
    for link in linkages:
        a = assignment[link.contract_a]
        b = assignment[link.contract_b]
        a_decided = a in (SettlementResult.YES, SettlementResult.NO)
        b_decided = b in (SettlementResult.YES, SettlementResult.NO)

        if a_decided and b_decided:
            if link.relation is LinkageRelation.IDENTICAL and a != b:
                return True
            if link.relation is LinkageRelation.COMPLEMENTARY and a == b:
                return True
            continue

        # At least one side is void or split. Without a proof that covers
        # cancellation, both the aligned and the divergent versions stay reachable.
        if not link.covers_void:
            continue
        if a_decided != b_decided:
            # One settles, the other does not - impossible once void handling is
            # proven to match.
            return True
        if a != b:
            # Both undecided but in different ways (void vs 50-50).
            return True
    return False


def _leg_unit_payout(
    leg: HeldLeg, rule: ContractRule, result: SettlementResult
) -> tuple[Decimal, bool]:
    """Per-unit payout of one leg in one settlement state, and whether it is a bound."""
    if result is SettlementResult.YES:
        return (_FULL if leg.side is Side.YES else _NONE), False
    if result is SettlementResult.NO:
        return (_FULL if leg.side is Side.NO else _NONE), False
    if result is SettlementResult.FIFTY_FIFTY:
        return _HALF, False

    # VOID.
    if rule.void_payout is VoidPayout.REFUND_COST:
        # The stake comes back; the fee does not. That asymmetry is why a basket of
        # two refunding legs still nets a small loss rather than breaking even.
        return leg.price_per_unit, False
    if rule.void_payout is VoidPayout.REFUND_TOTAL:
        return leg.price_per_unit + safe_div(leg.fees_usd, leg.quantity), False
    if rule.void_payout is VoidPayout.HALF:
        return _HALF, False
    if rule.void_payout is VoidPayout.ZERO:
        return _NONE, False
    # UNKNOWN: bound at the worst it can be, and say so.
    return _NONE, True
