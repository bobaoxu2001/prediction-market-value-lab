"""Same-venue implication structures, decided on a payoff floor rather than an anomaly.

If one contract's truth forces another's, a position holding YES on the weaker claim
and NO on the stronger one cannot be beaten by any settlement the rules allow. Take
``H = [X > b]`` and ``L = [X > a]`` with ``a < b``. ``H`` implies ``L``, so the state
"H true, L false" does not exist, and the remaining three pay:

===================  ==============  ==============  =======
state                YES(L) pays     NO(H) pays      total
===================  ==============  ==============  =======
X <= a               0               1               **1**
a < X <= b           1               1               2
X > b                1               0               **1**
===================  ==============  ==============  =======

The floor is $1 per set. Buy the pair for less than $1 all-in and the structure cannot
lose. That is a statement about arithmetic, not about the market being wrong.

**Why this module exists next to** :mod:`pmvl_markets.arbitrage.logical`. That module
looks for a *monotonicity violation* - the stronger claim's ask quoted above the
weaker claim's ask - and only then prices a hedge. The violation test is a different
question from the one that decides the trade. What matters is whether
``ask_YES(L) + ask_NO(H) < $1``; since ``ask_NO(H) = 1 - bid_YES(H)``, that is
``ask_YES(L) < bid_YES(H)`` - a claim about one book's ask against the *other book's
bid*. Comparing ask to ask, as a violation test does, neither implies it nor follows
from it. So this module tests the payoff floor directly and never consults a
probability anomaly to decide whether to look.

**The implication has to be proven, not assumed.** Two contracts sharing an event
ticker are not thereby about the same observable at the same instant from the same
source on the same measurement basis. :func:`prove_implication` checks each of those
separately and records a per-field verdict, distinguishing a term that *contradicts*
the structure from one the data never carried and from one that does not apply to this
market type at all. Only ``PROVEN`` reaches pricing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds, iso

from ..pricing.fees import taker_fee
from ..pricing.orderbook import executable_quote
from ..settlement.payoff import (
    ContractRule,
    HeldLeg,
    LinkageEvidence,
    LinkageRelation,
    PayoffReport,
    SettlementLinkage,
    StructuralStatus,
    VoidPayout,
    evaluate_payoff,
)

__all__ = [
    "FieldVerdict",
    "ProofStatus",
    "FieldCheck",
    "ImplicationProof",
    "StrikeContract",
    "StructureCertificate",
    "implication_holds",
    "prove_implication",
    "scan_implication_family",
]


class FieldVerdict(StrEnum):
    """Outcome of comparing one settlement term across two contracts.

    Four values, not two, because "these disagree", "nobody recorded this" and "this
    term has no meaning for this market type" call for completely different responses
    and collapsing them into a single failure produces a system that can never
    distinguish a broken parser from a genuine mismatch.
    """

    MATCH = "match"
    MISMATCH = "mismatch"
    #: The field is meaningful here but absent or unparsed on at least one side.
    INSUFFICIENT = "insufficient"
    #: The field does not apply to this market type - a categorical event has no
    #: numeric threshold, and demanding one would fail it forever for no reason.
    NOT_APPLICABLE = "not_applicable"


class ProofStatus(StrEnum):
    PROVEN = "proven"
    #: A term actively contradicts the implication.
    REFUTED = "refuted"
    #: Nothing contradicts it, but a term the proof depends on was never recorded.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    #: This pair is not the kind of thing an implication proof applies to.
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class FieldCheck:
    name: str
    verdict: FieldVerdict
    detail: str = ""
    evidence_a: str = ""
    evidence_b: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.name,
            "verdict": self.verdict.value,
            "detail": self.detail,
            "evidence_antecedent": self.evidence_a,
            "evidence_consequent": self.evidence_b,
        }


@dataclass
class ImplicationProof:
    """Whether ``antecedent`` settling YES forces ``consequent`` to YES, and why."""

    status: ProofStatus
    antecedent_key: str
    consequent_key: str
    checks: list[FieldCheck] = field(default_factory=list)
    reason: str = ""

    @property
    def holds(self) -> bool:
        return self.status is ProofStatus.PROVEN

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "antecedent": self.antecedent_key,
            "consequent": self.consequent_key,
            "reason": self.reason,
            "checks": [c.as_dict() for c in self.checks],
        }


@dataclass(frozen=True)
class StrikeContract:
    """One rung of a threshold ladder, with the book you would actually lift."""

    market: NormalizedMarket
    book: OrderBook
    comparator: str
    threshold: Decimal
    market_id: int | None = None

    @property
    def key(self) -> str:
        return self.market.platform_market_id


#: Comparators this module can reason about. ``between`` is deliberately absent: a
#: bucket market is one rung of an exhaustive basket, not a half-line, and treating it
#: as a threshold would assert an implication that does not hold in either direction.
_UPWARD = {"greater", "greater_or_equal"}
_DOWNWARD = {"less", "less_or_equal"}
SUPPORTED_COMPARATORS = _UPWARD | _DOWNWARD


def implication_holds(
    comp_a: str, a: Decimal, comp_b: str, b: Decimal
) -> tuple[bool, str]:
    """Does ``[X comp_a a]`` imply ``[X comp_b b]``? Returns (holds, why).

    Set containment, done exactly, because the boundary is where this goes wrong.
    For upward comparators the truth sets are half-lines opening right:

    * ``(a, inf)`` subset of ``(b, inf)``   iff ``a >= b``
    * ``(a, inf)`` subset of ``[b, inf)``   iff ``a >= b``
    * ``[a, inf)`` subset of ``(b, inf)``   iff ``a > b``   - strict, and the one
      case where equal strikes do **not** imply
    * ``[a, inf)`` subset of ``[b, inf)``   iff ``a >= b``
    """
    if comp_a in _UPWARD and comp_b in _UPWARD:
        if comp_a == "greater_or_equal" and comp_b == "greater":
            return (a > b, f"[X >= {a}] subset of (X > {b}) requires {a} > {b}")
        return (a >= b, f"[X {comp_a} {a}] subset of [X {comp_b} {b}] requires {a} >= {b}")

    if comp_a in _DOWNWARD and comp_b in _DOWNWARD:
        if comp_a == "less_or_equal" and comp_b == "less":
            return (a < b, f"[X <= {a}] subset of (X < {b}) requires {a} < {b}")
        return (a <= b, f"[X {comp_a} {a}] subset of [X {comp_b} {b}] requires {a} <= {b}")

    # One opens left and the other right. Their truth sets overlap without either
    # containing the other, so neither direction implies.
    return (
        False,
        f"comparators point in opposite directions ({comp_a} vs {comp_b}); "
        "neither truth set contains the other",
    )


def _same_or_insufficient(
    name: str, a: object, b: object, *, label_a: str = "", label_b: str = ""
) -> FieldCheck:
    """MATCH when both sides state the same value, MISMATCH when they differ."""
    sa = "" if a is None else str(a).strip()
    sb = "" if b is None else str(b).strip()
    if not sa or not sb:
        return FieldCheck(
            name, FieldVerdict.INSUFFICIENT,
            f"stated on {'neither side' if not sa and not sb else 'one side only'}",
            label_a or sa, label_b or sb,
        )
    if sa != sb:
        return FieldCheck(name, FieldVerdict.MISMATCH, f"{sa} vs {sb}", sa, sb)
    return FieldCheck(name, FieldVerdict.MATCH, sa, sa, sb)


def prove_implication(
    antecedent: StrikeContract, consequent: StrikeContract
) -> ImplicationProof:
    """Prove that ``antecedent`` YES forces ``consequent`` YES, term by term.

    Everything the payoff floor leans on is checked explicitly. Sharing an event
    ticker is treated as a hint about where to look, never as evidence: two rungs of
    what looks like one ladder can still differ in settlement source or measurement
    basis, and that difference is exactly what would break the floor while leaving the
    arithmetic looking fine.
    """
    a_key, b_key = antecedent.key, consequent.key
    checks: list[FieldCheck] = []

    def refuse(status: ProofStatus, reason: str) -> ImplicationProof:
        return ImplicationProof(status, a_key, b_key, checks, reason)

    # --- applicability ------------------------------------------------------
    if antecedent.market.platform is not consequent.market.platform:
        checks.append(
            FieldCheck("venue", FieldVerdict.MISMATCH,
                       f"{antecedent.market.platform.value} vs {consequent.market.platform.value}")
        )
        return refuse(
            ProofStatus.NOT_APPLICABLE,
            "this route is same-venue only; a cross-venue pair must go through the "
            "equivalence path, which has its own and stricter gate",
        )
    checks.append(FieldCheck("venue", FieldVerdict.MATCH, antecedent.market.platform.value))

    for side, contract in (("antecedent", antecedent), ("consequent", consequent)):
        if contract.comparator not in SUPPORTED_COMPARATORS:
            checks.append(
                FieldCheck("comparator", FieldVerdict.NOT_APPLICABLE,
                           f"{side} uses {contract.comparator!r}, which is not a half-line")
            )
            return refuse(
                ProofStatus.NOT_APPLICABLE,
                f"{side} comparator {contract.comparator!r} does not define a "
                "threshold half-line; bucket and custom markets belong to the basket "
                "route, not this one",
            )

    # --- the implication itself --------------------------------------------
    holds, why = implication_holds(
        antecedent.comparator, antecedent.threshold,
        consequent.comparator, consequent.threshold,
    )
    checks.append(
        FieldCheck(
            "threshold_order",
            FieldVerdict.MATCH if holds else FieldVerdict.MISMATCH,
            why,
            f"{antecedent.comparator} {antecedent.threshold}",
            f"{consequent.comparator} {consequent.threshold}",
        )
    )
    if not holds:
        return refuse(ProofStatus.REFUTED, why)

    # --- the terms the floor depends on ------------------------------------
    # Same observable, same instant, same source, same basis. Any one of these
    # differing means the two contracts can disagree for reasons the strike order
    # says nothing about, and the pruned state becomes reachable again.
    am, cm = antecedent.market, consequent.market
    checks.append(_same_or_insufficient("series", am.series_ticker, cm.series_ticker))
    checks.append(_same_or_insufficient("event", am.platform_event_id, cm.platform_event_id))
    checks.append(
        _same_or_insufficient(
            "cutoff",
            iso(am.close_time) if am.close_time else "",
            iso(cm.close_time) if cm.close_time else "",
        )
    )
    checks.append(
        _same_or_insufficient("settlement_source", am.settlement_source, cm.settlement_source)
    )
    checks.append(
        _same_or_insufficient("resolution_hash_inputs", am.strike_type, cm.strike_type)
    )

    mismatched = [c for c in checks if c.verdict is FieldVerdict.MISMATCH]
    if mismatched:
        return refuse(
            ProofStatus.REFUTED,
            "; ".join(f"{c.name}: {c.detail}" for c in mismatched),
        )

    # A term that decides the payout and was never recorded blocks the proof. It is
    # reported as insufficient rather than as a mismatch, because the two call for
    # different work: one is a data gap to fill, the other is a pair to discard.
    decisive = {"series", "event", "cutoff"}
    missing = [
        c for c in checks if c.name in decisive and c.verdict is FieldVerdict.INSUFFICIENT
    ]
    if missing:
        return refuse(
            ProofStatus.INSUFFICIENT_EVIDENCE,
            "cannot confirm the two contracts observe the same quantity at the same "
            "instant: " + ", ".join(f"{c.name} {c.detail}" for c in missing),
        )

    return ImplicationProof(
        ProofStatus.PROVEN, a_key, b_key, checks,
        f"{antecedent.comparator} {antecedent.threshold} implies "
        f"{consequent.comparator} {consequent.threshold} on the same observable, "
        "cutoff and settlement source",
    )


@dataclass
class StructureCertificate:
    """Everything needed to recompute this verdict without the market present."""

    structure: str
    venue: str
    event_key: str
    proof: ImplicationProof
    #: contract key -> the rule terms relied on.
    rules_used: dict[str, dict[str, object]] = field(default_factory=dict)
    legs: list[dict[str, object]] = field(default_factory=list)
    quantity: Decimal = ZERO
    payoff_floor_usd: Decimal = ZERO
    gross_cost_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    total_cost_usd: Decimal = ZERO
    net_floor_usd: Decimal = ZERO
    structural_status: str = StructuralStatus.UNKNOWN.value
    #: Cheapest the pair can be bought for at top of book, whether or not it clears.
    #: Recorded even on rejection: "the asks summed to $1.0020" and "they summed to
    #: $1.31" are the same verdict but completely different distances from a trade,
    #: and a categorical reject throws that away.
    best_combined_ask: Decimal | None = None
    accepted: bool = False
    rejection_reason: str = ""
    book_observed_at: list[str] = field(default_factory=list)
    worst_book_age_seconds: int | None = None
    payoff_report: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "structure": self.structure,
            "venue": self.venue,
            "event_key": self.event_key,
            "proof": self.proof.as_dict(),
            "rules_used": self.rules_used,
            "legs": self.legs,
            "quantity": str(self.quantity),
            "payoff_floor_usd": str(quantize_usd(self.payoff_floor_usd)),
            "gross_cost_usd": str(quantize_usd(self.gross_cost_usd)),
            "fees_usd": str(quantize_usd(self.fees_usd)),
            "total_cost_usd": str(quantize_usd(self.total_cost_usd)),
            "net_floor_usd": str(quantize_usd(self.net_floor_usd)),
            "structural_status": self.structural_status,
            "best_combined_ask": (
                str(self.best_combined_ask) if self.best_combined_ask is not None else None
            ),
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "book_observed_at": list(self.book_observed_at),
            "worst_book_age_seconds": self.worst_book_age_seconds,
            "payoff_report": self.payoff_report,
        }


#: Per-unit payout a voided Kalshi contract returns. The stake comes back; the trading
#: fee does not, which is why an aligned void costs exactly the fees and no more.
_DEFAULT_VOID = VoidPayout.REFUND_COST


def _rules_for(
    antecedent: StrikeContract, consequent: StrikeContract, *, can_void: bool
) -> dict[str, ContractRule]:
    event = antecedent.market.platform_event_id or antecedent.market.series_ticker or ""
    return {
        c.key: ContractRule(
            contract_key=c.key,
            venue=c.market.platform,
            event_key=event,
            can_void=can_void,
            void_payout=_DEFAULT_VOID if can_void else VoidPayout.UNKNOWN,
            rule_source=(c.market.settlement_rules_raw or "")[:400],
            rule_version=c.market.resolution_hash or "",
        )
        for c in (antecedent, consequent)
    }


def _accretive_size(
    consequent: StrikeContract,
    antecedent: StrikeContract,
    *,
    ceiling: Decimal,
) -> Decimal:
    """Largest size taking only sets whose **marginal** cost stays under the $1 floor.

    The objective is net floor, not size, and those are different maxima. Walking the
    two ask ladders in lockstep and stopping when the next pair of levels costs $1 or
    more takes exactly the units that add edge and refuses the ones that dilute it.

    Sizing instead by "largest size whose *average* cost is under $1" - the obvious
    reading, and what this function did first - keeps buying accretive-on-average
    units long after the marginal unit has turned unprofitable. On a real Solana
    ladder that took 10,000 sets at a blended $0.9934 when the top of book was
    $0.9400: a 0.66c edge per set against a 1.27c fee, turning a structure that clears
    into one that loses $60. The average is not the number that decides whether to buy
    one more.
    """
    yes_levels = list(consequent.book.asks(Side.YES))
    no_levels = list(antecedent.book.asks(Side.NO))
    if not yes_levels or not no_levels:
        return ZERO

    i = j = 0
    left_yes = yes_levels[0].size
    left_no = no_levels[0].size
    sets = ZERO

    while i < len(yes_levels) and j < len(no_levels) and sets < ceiling:
        marginal = yes_levels[i].price + no_levels[j].price
        if marginal >= ONE:
            break
        take = min(left_yes, left_no, ceiling - sets)
        if take <= ZERO:
            break
        sets += take
        left_yes -= take
        left_no -= take
        if left_yes <= ZERO:
            i += 1
            if i < len(yes_levels):
                left_yes = yes_levels[i].size
        if left_no <= ZERO:
            j += 1
            if j < len(no_levels):
                left_no = no_levels[j].size

    return sets


def scan_implication_family(
    contracts: list[StrikeContract],
    *,
    max_sets: Decimal = Decimal("10000"),
    min_net_floor_usd: Decimal = Decimal("0.01"),
    max_quote_age_seconds: int = 300,
    min_notional_usd: Decimal = ZERO,
) -> list[StructureCertificate]:
    """Price every provable implication pair in one ladder and certify the result.

    Returns a certificate for each pair whose implication is **proven**, accepted or
    not. Rejections are kept because "we looked and it did not clear" and "we never
    looked" are different findings, and only the first is evidence about the market.
    Pairs whose proof fails are not certified here - they are counted by the caller,
    since a certificate implies a structure worth recomputing.
    """
    certificates: list[StructureCertificate] = []
    ordered = sorted(contracts, key=lambda c: c.threshold)

    for i, weaker in enumerate(ordered):
        for stronger in ordered[i + 1 :]:
            # For upward comparators the higher strike is the stronger claim; for
            # downward ones the ladder runs the other way. prove_implication decides
            # which direction actually holds rather than trusting the sort.
            antecedent, consequent = stronger, weaker
            proof = prove_implication(antecedent, consequent)
            if not proof.holds:
                flipped = prove_implication(weaker, stronger)
                if not flipped.holds:
                    continue
                antecedent, consequent, proof = weaker, stronger, flipped

            cert = _price_structure(
                antecedent,
                consequent,
                proof,
                max_sets=max_sets,
                min_net_floor_usd=min_net_floor_usd,
                max_quote_age_seconds=max_quote_age_seconds,
                min_notional_usd=min_notional_usd,
            )
            certificates.append(cert)
    return certificates


def _price_structure(
    antecedent: StrikeContract,
    consequent: StrikeContract,
    proof: ImplicationProof,
    *,
    max_sets: Decimal,
    min_net_floor_usd: Decimal,
    max_quote_age_seconds: int,
    min_notional_usd: Decimal,
) -> StructureCertificate:
    """Buy YES on the weaker claim and NO on the stronger one, at real depth."""
    event_key = antecedent.market.platform_event_id or antecedent.market.series_ticker or ""
    cert = StructureCertificate(
        structure="implication:YES(consequent)+NO(antecedent)",
        venue=antecedent.market.platform.value,
        event_key=event_key,
        proof=proof,
    )
    ages = [age_seconds(consequent.book.observed_at), age_seconds(antecedent.book.observed_at)]
    known_ages = [a for a in ages if a is not None]
    cert.worst_book_age_seconds = int(max(known_ages)) if known_ages else None
    cert.book_observed_at = [
        iso(consequent.book.observed_at), iso(antecedent.book.observed_at)
    ]

    top_yes = consequent.book.best_ask(Side.YES)
    top_no = antecedent.book.best_ask(Side.NO)
    if top_yes is not None and top_no is not None:
        cert.best_combined_ask = top_yes + top_no

    sets = _accretive_size(consequent, antecedent, ceiling=max_sets)
    if sets <= ZERO:
        shortfall = (
            f"; cheapest top-of-book pair is ${cert.best_combined_ask}"
            if cert.best_combined_ask is not None
            else "; at least one side has no ask"
        )
        cert.rejection_reason = (
            "no size clears the $1 floor: the two asks sum to at least $1 at every "
            "depth the books support" + shortfall
        )
        return cert

    yes_leg = executable_quote(consequent.book, Side.YES, sets)
    no_leg = executable_quote(antecedent.book, Side.NO, sets)
    if yes_leg is None or no_leg is None:
        cert.rejection_reason = "a leg could not be walked at the chosen size"
        return cert

    fee_yes = taker_fee(
        consequent.market.platform, sets, yes_leg.average_price,
        rate=consequent.market.fee_rate, fee_type=consequent.market.fee_type,
    )
    fee_no = taker_fee(
        antecedent.market.platform, sets, no_leg.average_price,
        rate=antecedent.market.fee_rate, fee_type=antecedent.market.fee_type,
    )
    gross = yes_leg.notional + no_leg.notional
    fees = fee_yes + fee_no

    legs = [
        HeldLeg(consequent.key, Side.YES, sets, yes_leg.average_price, fee_yes),
        HeldLeg(antecedent.key, Side.NO, sets, no_leg.average_price, fee_no),
    ]
    linkage = SettlementLinkage(
        contract_a=antecedent.key,
        contract_b=consequent.key,
        relation=LinkageRelation.IMPLIES,
        evidence=LinkageEvidence.PROVEN,
        # Same venue, same event, same settlement source and cutoff - all checked by
        # the proof - so a void is one event striking both legs at once. The divergent
        # void that defeats a cross-venue pair is not reachable here.
        covers_void=True,
        note=proof.reason,
    )

    normal = evaluate_payoff(legs, _rules_for(antecedent, consequent, can_void=False), [linkage])
    with_void = evaluate_payoff(legs, _rules_for(antecedent, consequent, can_void=True), [linkage])

    cert.quantity = sets
    cert.gross_cost_usd = gross
    cert.fees_usd = fees
    cert.total_cost_usd = normal.total_cost_usd
    cert.payoff_floor_usd = normal.min_payout_usd or ZERO
    cert.net_floor_usd = normal.worst_case_net_usd or ZERO
    cert.structural_status = normal.structural_status.value
    cert.payoff_report = {
        "normal_settlement": normal.as_dict(),
        "including_aligned_void": with_void.as_dict(),
    }
    cert.rules_used = {
        key: {
            "contract_key": rule.contract_key,
            "event_key": rule.event_key,
            "rule_version": rule.rule_version,
            "rule_source_excerpt": rule.rule_source[:200],
        }
        for key, rule in _rules_for(antecedent, consequent, can_void=False).items()
    }
    cert.legs = [
        {
            "contract_key": consequent.key,
            "title": consequent.market.title[:120],
            "role": "consequent (weaker claim)",
            "predicate": f"{consequent.comparator} {consequent.threshold}",
            "side": Side.YES.value,
            "quantity": str(sets),
            "vwap": str(yes_leg.average_price),
            "worst_price": str(yes_leg.worst_price),
            "levels_consumed": yes_leg.levels_consumed,
            "fee_usd": str(quantize_usd(fee_yes)),
        },
        {
            "contract_key": antecedent.key,
            "title": antecedent.market.title[:120],
            "role": "antecedent (stronger claim)",
            "predicate": f"{antecedent.comparator} {antecedent.threshold}",
            "side": Side.NO.value,
            "quantity": str(sets),
            "vwap": str(no_leg.average_price),
            "worst_price": str(no_leg.worst_price),
            "levels_consumed": no_leg.levels_consumed,
            "fee_usd": str(quantize_usd(fee_no)),
        },
    ]

    # ---- acceptance, each condition reported separately --------------------
    reasons: list[str] = []
    if normal.structural_status is not StructuralStatus.GUARANTEED:
        reasons.append(f"structural_status={normal.structural_status.value}")
    if cert.net_floor_usd <= min_net_floor_usd:
        reasons.append(
            f"net floor ${quantize_usd(cert.net_floor_usd)} does not clear the "
            f"${quantize_usd(min_net_floor_usd)} tolerance"
        )
    if cert.worst_book_age_seconds is None:
        reasons.append("book age unknown")
    elif cert.worst_book_age_seconds > max_quote_age_seconds:
        reasons.append(
            f"oldest book is {cert.worst_book_age_seconds}s old, over the "
            f"{max_quote_age_seconds}s limit"
        )
    if cert.total_cost_usd < min_notional_usd:
        reasons.append(
            f"${quantize_usd(cert.total_cost_usd)} of matched depth is below the "
            f"${quantize_usd(min_notional_usd)} minimum"
        )

    cert.accepted = not reasons
    cert.rejection_reason = "; ".join(reasons)
    return cert
