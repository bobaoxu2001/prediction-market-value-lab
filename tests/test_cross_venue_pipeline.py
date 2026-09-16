"""From two normalized markets to a shadow-executed pair, or an honest refusal.

These are the labelled synthetic fixtures: every input is constructed, so the expected
answer is known, and the assertions are about *which stage* rejects a pair rather than
only about the final verdict. A pair rejected at the wrong stage is still rejected,
and would pass a test that only checked the outcome.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmvl_markets.arbitrage.coverage import CoverageFunnel, Stage
from pmvl_markets.execution.assessment import AccessStatus
from pmvl_markets.execution.candidates import CandidateRejection, build_pair_candidate
from pmvl_markets.execution.funding import BalanceSource, FundingLedger
from pmvl_markets.execution.modes import ExecutionMode
from pmvl_markets.execution.simulator import BookBackedVenueClient, ladder_from_book
from pmvl_markets.execution.twoleg import PairState, TwoLegExecutor
from pmvl_markets.matching.cancellation import (
    AttestationStore,
    RuleAttestation,
    VoidHandling,
)
from pmvl_markets.matching.equivalence import EquivalenceScore, EquivalenceVerdict
from pmvl_shared.enums import DataProvenance, Platform, RuleCompatibility, Side
from pmvl_shared.schemas import BookLevel, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import utcnow

D = Decimal

KALSHI_ID = "KXGAME-26MAR01-A"
POLY_ID = "0xmarket"
POLY_CONDITION = "0xcondition"
POLY_YES = "token-yes"
POLY_NO = "token-no"
RULE_HASH_K = "hash-kalshi-v1"
RULE_HASH_P = "hash-poly-v1"


def _kalshi_market(**kwargs) -> NormalizedMarket:
    defaults = dict(
        platform=Platform.KALSHI,
        platform_market_id=KALSHI_ID,
        title="Will team A win on March 1?",
        resolution_hash=RULE_HASH_K,
        fee_rate=D("0.07"),
        fee_type="quadratic",
        tick_size=D("0.01"),
        accepting_orders=True,
    )
    defaults.update(kwargs)
    return NormalizedMarket(**defaults)


def _poly_market(**kwargs) -> NormalizedMarket:
    defaults = dict(
        platform=Platform.POLYMARKET,
        platform_market_id=POLY_ID,
        title="Will team A win on March 1?",
        resolution_hash=RULE_HASH_P,
        fee_rate=D("0"),
        fee_type="quadratic",
        tick_size=D("0.01"),
        min_order_size=D("5"),
        condition_id=POLY_CONDITION,
        yes_token_id=POLY_YES,
        no_token_id=POLY_NO,
        accepting_orders=True,
    )
    defaults.update(kwargs)
    return NormalizedMarket(**defaults)


def _book(platform, market_id, *, yes_ask=D("0.45"), no_ask=D("0.50"), size=D("200"), age=0):
    observed = utcnow() - timedelta(seconds=age)
    return OrderBook(
        platform=platform,
        platform_market_id=market_id,
        observed_at=observed,
        yes_asks=[BookLevel(price=yes_ask, size=size)],
        no_asks=[BookLevel(price=no_ask, size=size)],
        yes_bids=[BookLevel(price=yes_ask - D("0.02"), size=size)],
        no_bids=[BookLevel(price=no_ask - D("0.02"), size=size)],
        provenance=DataProvenance.FIXTURE,
    )


def _score(verdict=EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT, inverted=False):
    return EquivalenceScore(
        verdict=verdict,
        components=[],
        rule_compatibility=RuleCompatibility.IDENTICAL,
        match_confidence=D("0.95"),
        polarity_inverted=inverted,
        outcome_mapping={"yes": "yes", "no": "no"},
        resolution_hash_a=RULE_HASH_K,
        resolution_hash_b=RULE_HASH_P,
    )


def _attested(*, kalshi=True, poly=True, handling=VoidHandling.RESOLVES_ANYWAY,
              kalshi_hash=RULE_HASH_K, poly_hash=RULE_HASH_P) -> AttestationStore:
    store = AttestationStore()
    if kalshi:
        store.record(
            RuleAttestation(
                venue=Platform.KALSHI, market_ref=KALSHI_ID,
                resolution_hash=kalshi_hash, handling=handling,
                reviewed_by="ops@example", reviewed_at="2026-09-16T00:00:00Z",
                evidence_quote="If the game is not played by March 3, resolves to No.",
            )
        )
    if poly:
        store.record(
            RuleAttestation(
                venue=Platform.POLYMARKET, market_ref=POLY_ID,
                resolution_hash=poly_hash, handling=handling,
                reviewed_by="ops@example", reviewed_at="2026-09-16T00:00:00Z",
                evidence_quote="If the game is not played, this market resolves to No.",
            )
        )
    return store


def _build(*, attestations=None, kalshi=None, poly=None, book_k=None, book_p=None,
           score=None, funnel=None, max_age=120):
    kalshi = kalshi or _kalshi_market()
    poly = poly or _poly_market()
    return build_pair_candidate(
        pair_id="pair-x",
        market_a=kalshi,
        book_a=book_k or _book(Platform.KALSHI, KALSHI_ID),
        side_a=Side.YES,
        market_b=poly,
        book_b=book_p or _book(Platform.POLYMARKET, POLY_ID),
        side_b=Side.NO,
        score=score or _score(),
        attestations=attestations if attestations is not None else _attested(),
        max_quote_age_seconds=max_age,
        access_status=AccessStatus.PERMITTED,
        funnel=funnel,
    )


def _run(candidate):
    books = {
        (Platform.KALSHI, KALSHI_ID): _book(Platform.KALSHI, KALSHI_ID),
        (Platform.POLYMARKET, POLY_ID): _book(Platform.POLYMARKET, POLY_ID),
    }
    clients = {
        Platform.KALSHI: BookBackedVenueClient(
            Platform.KALSHI,
            {KALSHI_ID: ladder_from_book(books[(Platform.KALSHI, KALSHI_ID)], Side.YES)},
        ),
        Platform.POLYMARKET: BookBackedVenueClient(
            Platform.POLYMARKET,
            {POLY_NO: ladder_from_book(books[(Platform.POLYMARKET, POLY_ID)], Side.NO)},
        ),
    }
    funding = FundingLedger()
    funding.declare(Platform.KALSHI, D("10000"), source=BalanceSource.OPERATOR_DECLARED)
    funding.declare(Platform.POLYMARKET, D("10000"), source=BalanceSource.OPERATOR_DECLARED)
    executor = TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=funding, clients=clients
    )
    return executor.run(candidate), funding


# ------------------------------------------------------------------ the shape
def test_the_limit_price_is_the_worst_level_walked_not_the_average():
    deep = OrderBook(
        platform=Platform.KALSHI,
        platform_market_id=KALSHI_ID,
        observed_at=utcnow(),
        yes_asks=[
            BookLevel(price=D("0.44"), size=D("50")),
            BookLevel(price=D("0.46"), size=D("150")),
        ],
        yes_bids=[BookLevel(price=D("0.42"), size=D("200"))],
        no_asks=[BookLevel(price=D("0.54"), size=D("200"))],
        no_bids=[BookLevel(price=D("0.52"), size=D("200"))],
        provenance=DataProvenance.FIXTURE,
    )
    candidate = _build(book_k=deep)
    assert not isinstance(candidate, CandidateRejection)
    # VWAP across the two levels is 0.455; submitting there would leave the deeper
    # level unfilled and the pair lopsided.
    assert candidate.leg_a.limit_price == D("0.46")


def test_both_legs_are_sized_to_the_same_number_of_settlement_shares():
    thin_poly = _book(Platform.POLYMARKET, POLY_ID, size=D("60"))
    candidate = _build(book_p=thin_poly)
    assert candidate.leg_a.quantity == candidate.leg_b.quantity == D("60")


def test_a_polymarket_leg_addresses_a_token_and_a_kalshi_leg_a_ticker():
    candidate = _build()
    assert candidate.leg_a.contract.contract_key == KALSHI_ID
    assert candidate.leg_b.contract.contract_key == POLY_NO
    # The settlement rules are keyed to the same things the orders address.
    assert set(candidate.rules) == {KALSHI_ID, POLY_NO}
    assert candidate.linkages[0].contract_a == KALSHI_ID
    assert candidate.linkages[0].contract_b == POLY_NO


# --------------------------------------------------------- what makes it provable
def test_an_attested_strict_pair_executes_and_is_a_proven_arbitrage():
    candidate = _build()
    journal, funding = _run(candidate)
    assert journal.state is PairState.RECONCILED
    assert journal.arbitrage_completed is True
    assert journal.realised_pnl_usd > 0
    assert all(r["state"] == "committed" for r in funding.snapshot()["reservations"])


def test_an_unattested_pair_builds_but_will_not_trade():
    """The matcher proved the question. Nobody read the cancellation clause."""
    candidate = _build(attestations=AttestationStore())
    assert not isinstance(candidate, CandidateRejection)
    assert candidate.linkages[0].covers_void is False
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED
    assert "structural_status=not_guaranteed" in journal.abandon_reason


def test_attesting_only_one_venue_is_not_enough():
    candidate = _build(attestations=_attested(poly=False))
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED


def test_an_attestation_lapses_when_the_venue_edits_its_rules():
    """The hash covers the payout terms, so edited rules invalidate the reading."""
    stale = _attested(kalshi_hash="hash-kalshi-v0")
    candidate = _build(attestations=stale)
    assert candidate.linkages[0].covers_void is False
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED


def test_a_merely_probable_match_is_asserted_evidence_not_proven():
    candidate = _build(score=_score(EquivalenceVerdict.PROBABLE_MATCH))
    assert candidate.linkages[0].evidence.value == "asserted"
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED


def test_a_venue_that_voids_is_modelled_as_voidable_and_blocks_the_pair():
    candidate = _build(attestations=_attested(handling=VoidHandling.VOIDS))
    assert candidate.rules[KALSHI_ID].can_void is True
    journal, _ = _run(candidate)
    # An aligned void refunds the stake but keeps the Kalshi fee, so the worst state
    # loses exactly that fee.
    assert journal.state is PairState.ABANDONED
    assert journal.planned_payoff.worst_case_net_usd < 0


# ----------------------------------------------------------------- rejections
def test_a_stale_book_is_rejected_before_it_is_priced():
    funnel = CoverageFunnel()
    result = _build(book_k=_book(Platform.KALSHI, KALSHI_ID, age=600), funnel=funnel)
    assert isinstance(result, CandidateRejection)
    assert result.stage is Stage.BOOKS_FRESH
    assert funnel.drops[Stage.BOOKS_FRESH]["quote status stale"] == 1


def test_an_empty_book_is_rejected_rather_than_treated_as_zero_depth():
    empty = OrderBook(
        platform=Platform.POLYMARKET,
        platform_market_id=POLY_ID,
        observed_at=utcnow(),
        provenance=DataProvenance.FIXTURE,
    )
    result = _build(book_p=empty)
    assert isinstance(result, CandidateRejection)
    assert "no book" in result.reason


def test_a_polymarket_market_without_a_token_id_cannot_be_addressed():
    result = _build(poly=_poly_market(no_token_id=None))
    assert isinstance(result, CandidateRejection)
    assert "no no token id" in result.reason


def test_the_published_fee_rate_and_type_reach_the_order_unchanged():
    """The bridge must not substitute a rate of its own.

    A known gap sits behind this test: ``NormalizedMarket.fee_rate`` is a plain
    Decimal with a zero default, so the schema cannot express "this venue did not
    publish a rate". The Polymarket provider fills the hole with a per-category
    fallback and the Kalshi provider assumes a fee multiplier of 1. Both err high,
    which cannot manufacture an edge - but neither is the venue's own figure, and
    the distinction is currently invisible downstream. The unknown-fee refusal in
    :mod:`pmvl_markets.execution.venues` is therefore defence in depth rather than a
    path this bridge can reach today.
    """
    candidate = _build(kalshi=_kalshi_market(fee_rate=D("0.14"), fee_type="quadratic"))
    assert candidate.leg_a.contract.fee_rate == D("0.14")
    assert candidate.leg_a.contract.fee_type == "quadratic"
    # 0.14 x 200 contracts x 0.45 x 0.55, ceiled to the cent - twice the $3.47 the
    # published 0.07 rate would charge, and the reservation has to cover all of it.
    assert candidate.leg_a.max_fee_usd == D("6.93")
    assert _build().leg_a.max_fee_usd == D("3.47")


def test_depth_that_falls_short_of_the_matched_size_is_rejected():
    tiny = _book(Platform.POLYMARKET, POLY_ID, size=D("3"))
    result = _build(poly=_poly_market(min_order_size=D("5")), book_p=tiny)
    assert isinstance(result, CandidateRejection)


# -------------------------------------------------------------------- funnel
def test_the_funnel_reports_survivors_and_the_reasons_for_every_drop():
    funnel = CoverageFunnel(label="synthetic", observation_window="fixture")
    funnel.survived(Stage.MARKETS_LOADED, 2)
    funnel.survived(Stage.PAIRS_PROPOSED, 1)
    _build(book_k=_book(Platform.KALSHI, KALSHI_ID, age=600), funnel=funnel)
    _build(funnel=funnel)

    assert funnel.entered(Stage.BOOKS_FRESH) == 2
    assert funnel.survivors[Stage.BOOKS_FRESH] == 1
    rendered = funnel.render()
    assert "quote status stale" in rendered
    assert "recall ceiling" in rendered


def test_an_unreached_stage_is_shown_as_unreached_not_as_zero():
    """Zero survivors and "never ran" are different claims about the market."""
    funnel = CoverageFunnel()
    funnel.survived(Stage.MARKETS_LOADED, 5)
    rendered = funnel.render()
    assert "stage not reached" in rendered
    assert funnel.entered(Stage.SHADOW_EXECUTED) == 0


# ------------------------------------------------- outcome/token verification
def test_a_yes_no_market_verifies_its_token_mapping():
    from pmvl_markets.execution.candidates import verify_outcome_token_mapping

    assert verify_outcome_token_mapping(_poly_market(outcomes=["Yes", "No"])) is None


def test_over_under_and_up_down_markets_refuse_rather_than_assume_index_zero_is_yes():
    """Roughly half the open Polymarket book is labelled this way.

    Index 0 is genuinely the first outcome, but whether "Over" is the YES of the
    Kalshi counterpart is a fact about the question, not about the label. Guessing
    inverts the leg, and an inverted leg looks like free money rather than failing.
    """
    from pmvl_markets.execution.candidates import verify_outcome_token_mapping

    for labels in (["Over", "Under"], ["Up", "Down"], ["Chiefs", "Broncos"]):
        error = verify_outcome_token_mapping(_poly_market(outcomes=labels))
        assert error is not None
        assert "index 0 cannot be assumed to be YES" in error


def test_an_unverifiable_mapping_stops_the_pair_and_is_counted():
    funnel = CoverageFunnel()
    result = _build(poly=_poly_market(outcomes=["Over", "Under"]), funnel=funnel)
    assert isinstance(result, CandidateRejection)
    assert result.stage is Stage.EQUIVALENCE_VERIFIED
    assert sum(funnel.drops[Stage.EQUIVALENCE_VERIFIED].values()) == 1


def test_a_non_binary_outcome_list_is_refused():
    from pmvl_markets.execution.candidates import verify_outcome_token_mapping

    error = verify_outcome_token_mapping(_poly_market(outcomes=["A", "B", "C"]))
    assert "3 outcomes" in error


def test_kalshi_markets_are_not_subject_to_the_token_check():
    """Kalshi has one ticker with two sides; there is no token index to get wrong."""
    from pmvl_markets.execution.candidates import verify_outcome_token_mapping

    assert verify_outcome_token_mapping(_kalshi_market(outcomes=["Up", "Down"])) is None
