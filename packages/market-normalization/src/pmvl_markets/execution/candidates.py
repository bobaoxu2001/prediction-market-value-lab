"""Turning a scanner hit into something the two-leg executor will accept.

The scanner produces an :class:`~pmvl_shared.schemas.ArbitrageResult` with a label on
it. This module deliberately throws that label away and rebuilds the case from
primary sources: the two books, the two markets' published constraints, the two rule
texts, and whatever a human has attested about their cancellation terms. The
executor then re-derives the payoff a third time from actual fills.

Deriving it three times from three different inputs sounds wasteful until one of them
disagrees, which is the entire reason for the arrangement.

Two decisions here are worth stating because they are easy to get backwards.

**The limit price is the worst level the walk touches, not the average.** Submitting
at the average guarantees the back half of the intended size does not fill, which
turns a sized pair into a lopsided one - the exact failure the pair structure is
built to avoid.

**Size is the smaller of the two venues' depth, snapped down to both grids.** Equal
settlement shares is the invariant; a venue that cannot supply them sets the size for
both. Rounding down twice is not paranoia, because Kalshi's whole contract and
Polymarket's fractional share do not divide each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import ZERO
from pmvl_shared.schemas import NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds

from ..arbitrage.coverage import CoverageFunnel, Stage
from ..matching.cancellation import AttestationStore, VoidHandling
from ..matching.equivalence import EquivalenceScore, EquivalenceVerdict
from ..pricing.orderbook import executable_quote
from ..settlement.payoff import (
    ContractRule,
    LinkageEvidence,
    LinkageRelation,
    SettlementLinkage,
    VoidPayout,
)
from .assessment import AccessStatus, QuoteStatus
from .models import Outcome
from .twoleg import PairCandidate
from .venues import VenueContract, VenueOrderSpec, kalshi_contract, polymarket_contract

__all__ = [
    "CandidateRejection",
    "build_pair_candidate",
    "contract_rule_for",
    "linkage_for",
    "verify_outcome_token_mapping",
]

#: Outcome labels that unambiguously mean "the proposition holds" / "it does not".
#: Deliberately short. ``Over``/``Under`` and ``Up``/``Down`` are *not* here: they are
#: affirmative and negative of some proposition, but which proposition is a fact about
#: the question text, not about the label, and getting it backwards inverts the leg.
_AFFIRMATIVE = {"yes", "y", "true"}
_NEGATIVE = {"no", "n", "false"}


def verify_outcome_token_mapping(market: NormalizedMarket) -> str | None:
    """Check that ``yes_token_id`` really addresses the YES outcome. Returns an error.

    The Polymarket provider assigns ``clobTokenIds[0]`` to YES positionally, which is
    the documented convention and is right for a market whose outcomes are listed as
    ``["Yes", "No"]``. Slightly under half of the open Polymarket book is listed that
    way. The rest are ``["Over", "Under"]``, ``["Up", "Down"]`` or two team names,
    where index 0 is still the first outcome but "YES" is not a property of the label.

    An inverted mapping does not fail loudly. It buys the opposite side at a price
    that looks right, and a pair that should lose becomes a pair that appears to be
    free money - so this refuses rather than guesses, and names the labels it saw so
    the funnel can count what the refusal costs.
    """
    if market.platform is not Platform.POLYMARKET:
        return None
    outcomes = [str(o).strip().lower() for o in (market.outcomes or [])]
    if len(outcomes) != 2:
        return (
            f"polymarket market {market.platform_market_id} lists {len(outcomes)} "
            "outcomes; a binary token mapping cannot be verified"
        )
    if outcomes[0] in _AFFIRMATIVE and outcomes[1] in _NEGATIVE:
        return None
    return (
        "outcome-to-token mapping is unverified: outcomes are "
        f"{market.outcomes!r}, so index 0 cannot be assumed to be YES"
    )


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    """Why a pair did not become a candidate, at which stage."""

    stage: Stage
    reason: str

    def __bool__(self) -> bool:
        return False


def contract_rule_for(
    market: NormalizedMarket,
    *,
    attestations: AttestationStore,
) -> ContractRule:
    """Build the settlement rule for one market from what is actually known.

    Without a live attestation the contract is modelled as able to void with an
    unpublished payout, which bounds the void state at $0 and makes the pair
    unprovable. That is not a placeholder to be tuned away: it is the accurate
    description of a market whose cancellation clause nobody has read.
    """
    attestation = attestations.get(
        market.platform, market.platform_market_id, market.resolution_hash
    )
    if attestation is None:
        return ContractRule(
            contract_key=market.platform_market_id,
            venue=market.platform,
            event_key=market.resolution_hash or market.platform_market_id,
            can_void=True,
            void_payout=VoidPayout.UNKNOWN,
            rule_source="unattested",
            rule_version=market.resolution_hash,
        )

    handling = attestation.handling
    return ContractRule(
        contract_key=market.platform_market_id,
        venue=market.platform,
        event_key=market.resolution_hash or market.platform_market_id,
        can_void=handling is VoidHandling.VOIDS,
        can_fifty_fifty=handling is VoidHandling.SPLITS,
        void_payout=(
            VoidPayout.REFUND_COST if handling is VoidHandling.VOIDS else VoidPayout.UNKNOWN
        ),
        rule_source=f"attested by {attestation.reviewed_by}: {attestation.evidence_quote}"[:400],
        rule_version=attestation.resolution_hash,
    )


def linkage_for(
    market_a: NormalizedMarket,
    market_b: NormalizedMarket,
    score: EquivalenceScore,
    *,
    attestations: AttestationStore,
) -> SettlementLinkage:
    """Translate an equivalence verdict into a settlement linkage, conservatively.

    ``PROVEN`` requires ``VERIFIED_EQUIVALENT_STRICT`` - every payout term compared
    and agreeing. ``covers_void`` requires something stronger still: a live
    attestation on *both* markets. A matcher can establish that two markets ask the
    same question; only a person reading two rule pages establishes that they cancel
    the same way.
    """
    both_attested = all(
        attestations.get(m.platform, m.platform_market_id, m.resolution_hash) is not None
        for m in (market_a, market_b)
    )
    proven = score.verdict in (
        EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT,
        EquivalenceVerdict.VERIFIED_EQUIVALENT_STANDARD,
    )
    return SettlementLinkage(
        contract_a=market_a.platform_market_id,
        contract_b=market_b.platform_market_id,
        relation=(
            LinkageRelation.COMPLEMENTARY
            if score.polarity_inverted
            else LinkageRelation.IDENTICAL
        ),
        evidence=LinkageEvidence.PROVEN if proven else LinkageEvidence.ASSERTED,
        covers_void=both_attested,
        note=f"equivalence verdict {score.verdict.value}",
    )


def _venue_contract(market: NormalizedMarket, side: Side) -> VenueContract | str:
    """Build the venue contract for one side, or say what is missing."""
    outcome = Outcome.from_side(side)
    if market.platform is Platform.KALSHI:
        return kalshi_contract(
            market.platform_market_id,
            outcome,
            fee_rate=market.fee_rate,
            fee_type=market.fee_type or "",
            source=f"kalshi:{market.platform_market_id}",
        )
    if market.platform is Platform.POLYMARKET:
        mapping_error = verify_outcome_token_mapping(market)
        if mapping_error:
            return mapping_error
        token_id = market.yes_token_id if side is Side.YES else market.no_token_id
        if not token_id:
            return (
                f"polymarket market {market.platform_market_id} has no {side.value} "
                "token id; the order would have nothing to address"
            )
        if not market.condition_id:
            return (
                f"polymarket market {market.platform_market_id} has no condition id; "
                "the two sides cannot be tied to one question"
            )
        if market.tick_size is None or market.tick_size <= ZERO:
            return f"polymarket market {market.platform_market_id} has no tick size"
        return polymarket_contract(
            token_id,
            market.condition_id,
            outcome,
            fee_rate=market.fee_rate,
            tick_size=market.tick_size,
            min_quantity=market.min_order_size or Decimal("1"),
            min_notional_usd=ZERO,
            quantity_step=market.tick_size if market.tick_size < Decimal("1") else Decimal("1"),
            source=f"polymarket:{market.condition_id}",
        )
    return f"{market.platform.value} has no execution adapter"


def build_pair_candidate(
    *,
    pair_id: str,
    market_a: NormalizedMarket,
    book_a: OrderBook,
    side_a: Side,
    market_b: NormalizedMarket,
    book_b: OrderBook,
    side_b: Side,
    score: EquivalenceScore,
    attestations: AttestationStore,
    max_quote_age_seconds: int,
    access_status: AccessStatus = AccessStatus.UNVERIFIED,
    max_sets: Decimal = Decimal("10000"),
    extra_cost_usd: Decimal = ZERO,
    funnel: CoverageFunnel | None = None,
) -> PairCandidate | CandidateRejection:
    """Rebuild one pair from primary sources, or say why it cannot be built.

    Returns a :class:`CandidateRejection` rather than raising, because a pair that
    cannot be executed is the normal case and the reason is data worth counting.
    """

    def reject(stage: Stage, reason: str) -> CandidateRejection:
        if funnel is not None:
            funnel.dropped(stage, reason)
        return CandidateRejection(stage, reason)

    contract_a = _venue_contract(market_a, side_a)
    if isinstance(contract_a, str):
        return reject(Stage.EQUIVALENCE_VERIFIED, contract_a)
    contract_b = _venue_contract(market_b, side_b)
    if isinstance(contract_b, str):
        return reject(Stage.EQUIVALENCE_VERIFIED, contract_b)

    # ---- freshness, before anything is priced off the books -----------------
    ages = [age_seconds(book.observed_at) for book in (book_a, book_b)]
    worst_age = max([a for a in ages if a is not None], default=None)
    if book_a.is_empty or book_b.is_empty:
        return reject(Stage.BOOKS_FRESH, "at least one venue had no book")
    if worst_age is None:
        quote_status = QuoteStatus.MISSING
    elif worst_age > max_quote_age_seconds:
        quote_status = QuoteStatus.STALE
    else:
        quote_status = QuoteStatus.FRESH
    if quote_status is not QuoteStatus.FRESH:
        return reject(Stage.BOOKS_FRESH, f"quote status {quote_status.value}")

    # ---- size: equal settlement shares on both grids -------------------------
    depth_a = sum((lvl.size for lvl in book_a.asks(side_a)), ZERO)
    depth_b = sum((lvl.size for lvl in book_b.asks(side_b)), ZERO)
    target = min(depth_a, depth_b, max_sets)
    target = contract_a.round_quantity_down(contract_b.round_quantity_down(target))
    if target <= ZERO:
        return reject(Stage.BOOKS_FRESH, "matched depth rounds to zero on one grid")

    quote_a = executable_quote(book_a, side_a, target)
    quote_b = executable_quote(book_b, side_b, target)
    if quote_a is None or quote_b is None:
        return reject(Stage.BOOKS_FRESH, "one side could not be walked at the target size")
    if not (quote_a.fully_filled and quote_b.fully_filled):
        return reject(Stage.BOOKS_FRESH, "depth fell short of the matched size")

    # ---- the limit is the worst level touched, not the average --------------
    spec_a = VenueOrderSpec(contract_a, quote_a.worst_price, target)
    spec_b = VenueOrderSpec(contract_b, quote_b.worst_price, target)
    violations = spec_a.violations() + spec_b.violations()
    if violations:
        return reject(Stage.NET_POSITIVE, "; ".join(violations))

    rules = {
        market_a.platform_market_id: contract_rule_for(market_a, attestations=attestations),
        market_b.platform_market_id: contract_rule_for(market_b, attestations=attestations),
    }
    # Contract keys must match what the specs address. On Polymarket the order is
    # placed against a token id while the rule belongs to the market, so the rule is
    # re-keyed onto the token rather than the two quietly failing to line up.
    rules = {
        spec.contract.contract_key: _rekey(
            rules[market.platform_market_id], spec.contract.contract_key
        )
        for spec, market in ((spec_a, market_a), (spec_b, market_b))
    }
    linkage = linkage_for(market_a, market_b, score, attestations=attestations)
    linkage = SettlementLinkage(
        contract_a=spec_a.contract.contract_key,
        contract_b=spec_b.contract.contract_key,
        relation=linkage.relation,
        evidence=linkage.evidence,
        covers_void=linkage.covers_void,
        note=linkage.note,
    )

    candidate = PairCandidate(
        pair_id=pair_id,
        leg_a=spec_a,
        leg_b=spec_b,
        rules=rules,
        linkages=[linkage],
        target_quantity=target,
        source_signal_id=f"{market_a.platform_market_id}|{market_b.platform_market_id}",
        extra_cost_usd=extra_cost_usd,
        quote_status=quote_status,
        access_status=access_status,
    )
    if funnel is not None:
        funnel.survived(Stage.BOOKS_FRESH)
    return candidate


def _rekey(rule: ContractRule, contract_key: str) -> ContractRule:
    return ContractRule(
        contract_key=contract_key,
        venue=rule.venue,
        event_key=rule.event_key,
        can_void=rule.can_void,
        can_fifty_fifty=rule.can_fifty_fifty,
        void_payout=rule.void_payout,
        rule_source=rule.rule_source,
        rule_version=rule.rule_version,
    )
