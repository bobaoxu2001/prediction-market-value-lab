"""Two-leg cross-venue execution, from a clean fill to every way it goes wrong.

The property under test throughout is not "does the happy path work" - it is
**nothing that fails can be recorded as an arbitrage**. Every failure case below
asserts on ``arbitrage_completed`` as well as on the state machine, because the state
machine could be right while the summary that a human reads is wrong, and the summary
is what gets believed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_markets.execution.assessment import (
    AccessStatus,
    EconomicsStatus,
    ExecutionStatus,
    QuoteStatus,
)
from pmvl_markets.execution.funding import BalanceSource, FundingLedger
from pmvl_markets.execution.models import Outcome, OrderStatus
from pmvl_markets.execution.modes import ExecutionMode
from pmvl_markets.execution.simulator import (
    BookBackedVenueClient,
    Fault,
    SimulatedLadder,
)
from pmvl_markets.execution.twoleg import (
    NotASimulation,
    PairCandidate,
    PairState,
    TwoLegExecutor,
    derive_pair_client_order_id,
)
from pmvl_markets.execution.venues import (
    VenueOrderSpec,
    kalshi_contract,
    polymarket_contract,
)
from pmvl_markets.settlement.payoff import (
    ContractRule,
    LinkageEvidence,
    LinkageRelation,
    SettlementLinkage,
    StructuralStatus,
    VoidPayout,
)
from pmvl_shared.enums import Platform
from pmvl_shared.schemas import BookLevel

D = Decimal

KALSHI_TICKER = "KXTESTGAME-26MAR01-A"
POLY_TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
POLY_CONDITION = "0xcondition"


# --------------------------------------------------------------------- fixtures
def _kalshi_yes():
    return kalshi_contract(
        KALSHI_TICKER,
        Outcome.YES,
        fee_rate=D("0.07"),
        fee_type="quadratic",
        source="test",
    )


def _poly_no():
    return polymarket_contract(
        POLY_TOKEN,
        POLY_CONDITION,
        Outcome.NO,
        fee_rate=D("0"),
        tick_size=D("0.01"),
        min_quantity=D("5"),
        min_notional_usd=D("1"),
        quantity_step=D("1"),
        source="test",
    )


def _rules(*, can_void: bool = False):
    """Both contracts settle off the same event.

    ``can_void=False`` is the case where both rule texts define an outcome for every
    state of the world. It is the only shape in which a guaranteed *profit* exists,
    because a void that refunds the stake but keeps the fee loses the fee.
    """
    return {
        KALSHI_TICKER: ContractRule(
            contract_key=KALSHI_TICKER,
            venue=Platform.KALSHI,
            event_key="game-a",
            can_void=can_void,
            void_payout=VoidPayout.REFUND_COST if can_void else VoidPayout.UNKNOWN,
            rule_source="kalshi:rules#resolves-no-if-not-played",
            rule_version="2026-03-01",
        ),
        POLY_TOKEN: ContractRule(
            contract_key=POLY_TOKEN,
            venue=Platform.POLYMARKET,
            event_key="game-a",
            can_void=can_void,
            void_payout=VoidPayout.REFUND_COST if can_void else VoidPayout.UNKNOWN,
            rule_source="polymarket:rules#resolves-no-if-not-played",
            rule_version="2026-03-01",
        ),
    }


def _linkage(*, covers_void: bool = True, evidence=LinkageEvidence.PROVEN):
    return SettlementLinkage(
        contract_a=KALSHI_TICKER,
        contract_b=POLY_TOKEN,
        relation=LinkageRelation.IDENTICAL,
        evidence=evidence,
        covers_void=covers_void,
        note="threshold, comparator, cutoff and source compared term by term",
    )


def _candidate(*, quantity=D("100"), can_void=False, covers_void=True, evidence=LinkageEvidence.PROVEN):
    """Kalshi YES at 45c against Polymarket NO at 50c: 95c buys a dollar."""
    return PairCandidate(
        pair_id="pair-001",
        leg_a=VenueOrderSpec(_kalshi_yes(), D("0.45"), quantity),
        leg_b=VenueOrderSpec(_poly_no(), D("0.50"), quantity),
        rules=_rules(can_void=can_void),
        linkages=[_linkage(covers_void=covers_void, evidence=evidence)],
        source_signal_id="sig-1",
        quote_status=QuoteStatus.FRESH,
        access_status=AccessStatus.PERMITTED,
    )


def _ladders(*, kalshi_depth=D("200"), poly_depth=D("200")):
    return (
        {
            KALSHI_TICKER: SimulatedLadder(
                asks=[BookLevel(price=D("0.45"), size=kalshi_depth)],
                bids=[BookLevel(price=D("0.43"), size=kalshi_depth)],
            )
        },
        {
            POLY_TOKEN: SimulatedLadder(
                asks=[BookLevel(price=D("0.50"), size=poly_depth)],
                bids=[BookLevel(price=D("0.48"), size=poly_depth)],
            )
        },
    )


def _funded(amount=D("1000")):
    funding = FundingLedger()
    funding.declare(Platform.KALSHI, amount, source=BalanceSource.OPERATOR_DECLARED)
    funding.declare(Platform.POLYMARKET, amount, source=BalanceSource.OPERATOR_DECLARED)
    return funding


def _executor(kalshi_faults=None, poly_faults=None, funding=None, **ladder_kwargs):
    k_ladders, p_ladders = _ladders(**ladder_kwargs)
    clients = {
        Platform.KALSHI: BookBackedVenueClient(
            Platform.KALSHI, k_ladders, faults={KALSHI_TICKER: kalshi_faults or []}
        ),
        Platform.POLYMARKET: BookBackedVenueClient(
            Platform.POLYMARKET, p_ladders, faults={POLY_TOKEN: poly_faults or []}
        ),
    }
    return (
        TwoLegExecutor(
            mode=ExecutionMode.SHADOW,
            funding=funding or _funded(),
            clients=clients,
        ),
        clients,
    )


# ------------------------------------------------------------------ happy path
def test_both_legs_fill_and_the_pair_reconciles_as_a_proven_arbitrage():
    executor, _ = _executor()
    journal = executor.run(_candidate())

    assert journal.state is PairState.RECONCILED
    assert [t.new for t in journal.transitions] == [
        PairState.VALIDATED,
        PairState.FUNDS_RESERVED,
        PairState.RECHECKED,
        PairState.SUBMITTING,
        PairState.BOTH_FILLED,
        PairState.RECONCILED,
    ]
    assert journal.arbitrage_completed is True
    assert journal.realised_payoff is not None
    assert journal.realised_payoff.structural_status is StructuralStatus.GUARANTEED
    assert journal.realised_payoff.exhaustive is True
    # 100 shares cost 45 + 50 + 1.74 of Kalshi fee, and pay $100 in every state.
    assert journal.total_cash_out_usd == D("96.74")
    assert journal.realised_pnl_usd == D("3.26")
    assert journal.matched_quantity == D("100")
    assert journal.assessment.execution_status is ExecutionStatus.MATCHED


def test_the_acknowledgement_alone_never_populates_a_fill():
    """A leg's fill must come from ``confirm``, never from ``submit`` returning ok."""
    executor, clients = _executor()
    candidate = _candidate()
    spec = candidate.leg_a
    client = clients[Platform.KALSHI]
    ack = client.submit(
        spec, client_order_id="probe", attempt_id="a1"
    )
    assert ack.accepted is True
    assert not hasattr(ack, "filled_quantity")
    # The venue holds the order, but nothing has been read back yet.
    report = client.confirm(client_order_id="probe")
    assert report.filled_quantity == D("100")


# ------------------------------------------------------- one-legged executions
def test_second_leg_rejected_leaves_one_leg_and_is_not_an_arbitrage():
    executor, _ = _executor(poly_faults=[Fault.SUBMIT_REJECT])
    journal = executor.run(_candidate())

    states = [t.new for t in journal.transitions]
    assert PairState.ONE_LEG_FILLED in states
    assert journal.state is PairState.RECONCILED
    assert journal.arbitrage_completed is False
    assert journal.realised_payoff.structural_status is not StructuralStatus.GUARANTEED
    # The Kalshi leg was bought at 45c and sold back into a 43c bid: the failure
    # costs the spread plus both fees, and that number is recorded, not rounded away.
    assert journal.remedy_cost_usd > D("0")
    assert journal.matched_quantity == D("0")
    assert journal.assessment.execution_status is ExecutionStatus.NEUTRALISED


def test_a_one_legged_pair_is_never_reported_as_completed_even_when_the_leg_is_profitable():
    """The exposed leg could win. That does not make it an arbitrage."""
    executor, _ = _executor(poly_faults=[Fault.SUBMIT_REJECT])
    journal = executor.run(_candidate())
    assert journal.arbitrage_completed is False
    assert journal.assessment.structural_status is not StructuralStatus.GUARANTEED


def test_unequal_fills_are_hedged_down_to_the_matched_size():
    executor, _ = _executor(poly_faults=[Fault.PARTIAL_FILL])
    journal = executor.run(_candidate())

    states = [t.new for t in journal.transitions]
    assert PairState.PARTIALLY_FILLED in states
    assert PairState.UNWINDING in states
    assert journal.state is PairState.RECONCILED
    assert journal.arbitrage_completed is False
    # Polymarket filled 50 of 100; the Kalshi excess is closed and 50 stay hedged.
    assert journal.matched_quantity == D("50")
    kalshi = journal.leg(f"kalshi:{KALSHI_TICKER}")
    assert kalshi.closed_quantity == D("50")
    assert kalshi.retained_quantity == D("50")


# ------------------------------------------------------------------- unknowns
def test_a_submit_timeout_queries_rather_than_resubmitting():
    executor, clients = _executor(kalshi_faults=[Fault.SUBMIT_TIMEOUT])
    candidate = _candidate()
    journal = executor.run(candidate)

    kalshi = journal.leg(f"kalshi:{KALSHI_TICKER}")
    assert "querying by client_order_id rather than resubmitting" in " ".join(
        kalshi.notes
    )
    states = [t.new for t in journal.transitions]
    assert PairState.UNKNOWN in states
    # The timed-out request had in fact landed, so the query finds a real fill and
    # the pair completes. A resubmission would have produced a second position.
    assert journal.state is PairState.RECONCILED
    assert journal.arbitrage_completed is True
    assert clients[Platform.KALSHI]._next_id == 1


def test_an_unresolvable_leg_escalates_and_keeps_its_funds_reserved():
    funding = _funded()
    executor, _ = _executor(
        kalshi_faults=[Fault.CONFIRM_TIMEOUT, Fault.CONFIRM_TIMEOUT], funding=funding
    )
    journal = executor.run(_candidate())

    assert journal.state is PairState.ESCALATED
    assert journal.arbitrage_completed is False
    assert "must not be guessed" in journal.escalation_reason
    # Cash that might have been spent stays held. Releasing it would let the next
    # candidate spend the same dollars.
    reservation = funding.reservation("pair-001:kalshi")
    assert reservation.state.value == "held"


def test_an_unknown_leg_is_never_auto_unwound():
    """Unwinding a position that may not exist is how one problem becomes two."""
    executor, _ = _executor(
        kalshi_faults=[Fault.CONFIRM_TIMEOUT, Fault.CONFIRM_TIMEOUT]
    )
    journal = executor.run(_candidate())
    assert journal.state is PairState.ESCALATED
    assert all(leg.closed_quantity == D("0") for leg in journal.legs)


def test_a_cancel_that_races_a_fill_is_caught_by_reconciliation():
    """The first read says cancelled; the venue's later record says filled."""
    executor, _ = _executor(poly_faults=[Fault.CANCEL_FILL_RACE])
    journal = executor.run(_candidate())

    states = [t.new for t in journal.transitions]
    # Read one produced a one-legged picture; reconciliation corrected it.
    assert PairState.ONE_LEG_FILLED in states
    assert PairState.UNKNOWN in states
    assert journal.state is PairState.RECONCILED
    assert journal.arbitrage_completed is True
    poly = journal.leg(f"polymarket:{POLY_TOKEN}")
    assert poly.filled_quantity == D("100")
    assert any("reconciliation corrected the fill" in n for n in poly.notes)


# ------------------------------------------------------------------ price move
def test_a_price_jump_after_submission_is_priced_into_the_realised_payoff():
    """The limit tolerates one tick of slippage, so the leg fills - just worse."""
    candidate = _candidate()
    candidate.leg_b = VenueOrderSpec(_poly_no(), D("0.52"), D("100"))
    executor, _ = _executor(poly_faults=[Fault.PRICE_JUMP])
    journal = executor.run(candidate)

    poly = journal.leg(f"polymarket:{POLY_TOKEN}")
    assert poly.average_price == D("0.51")
    # 45 + 51 + 1.74 of Kalshi fee = 97.74 against a $100 payout in every state.
    assert journal.total_cash_out_usd == D("97.74")
    assert journal.realised_pnl_usd == D("2.26")

    # The same pair without the jump keeps the full dollar of edge. The difference is
    # exactly the tick the book moved, and it lands in the realised figure rather than
    # in the plan's.
    clean, _ = _executor()
    assert clean.run(_candidate()).realised_pnl_usd == D("3.26")


def test_a_price_jump_that_erases_the_edge_still_reports_honestly():
    """The fill is worse than the limit allows, so the leg simply does not fill."""
    k_ladders, p_ladders = _ladders()
    p_ladders[POLY_TOKEN] = SimulatedLadder(
        asks=[BookLevel(price=D("0.50"), size=D("200"))],
        bids=[BookLevel(price=D("0.30"), size=D("200"))],
    )
    clients = {
        Platform.KALSHI: BookBackedVenueClient(Platform.KALSHI, k_ladders),
        Platform.POLYMARKET: BookBackedVenueClient(
            Platform.POLYMARKET, p_ladders, faults={POLY_TOKEN: [Fault.PRICE_JUMP]}
        ),
    }
    candidate = _candidate()
    candidate.leg_b = VenueOrderSpec(_poly_no(), D("0.50"), D("100"))
    executor = TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=_funded(), clients=clients
    )
    journal = executor.run(candidate)
    # 0.51 is above the 0.50 limit, so nothing fills on Polymarket.
    assert journal.leg(f"polymarket:{POLY_TOKEN}").filled_quantity == D("0")
    assert journal.arbitrage_completed is False


# ----------------------------------------------------------------- crash/restart
def test_a_restart_queries_the_same_orders_instead_of_opening_new_ones():
    k_ladders, p_ladders = _ladders()
    clients = {
        Platform.KALSHI: BookBackedVenueClient(Platform.KALSHI, k_ladders),
        Platform.POLYMARKET: BookBackedVenueClient(Platform.POLYMARKET, p_ladders),
    }
    candidate = _candidate()
    first = TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=_funded(), clients=clients
    )
    original = first.run(candidate)
    assert original.arbitrage_completed is True
    depth_left = k_ladders[KALSHI_TICKER].asks[0].size

    # The process dies. A new one starts with the same candidate and the same clients.
    second = TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=_funded(), clients=clients
    )
    recovered = second.recover(candidate)

    assert recovered.state is PairState.RECONCILED
    assert recovered.arbitrage_completed is True
    # No new liquidity was consumed: recovery is a read, not a trade.
    assert k_ladders[KALSHI_TICKER].asks[0].size == depth_left
    assert all("no order was submitted" in " ".join(leg.notes) for leg in recovered.legs)
    assert [leg.client_order_id for leg in recovered.legs] == [
        leg.client_order_id for leg in original.legs
    ]


def test_the_idempotency_key_is_stable_across_processes_and_changes_with_the_order():
    candidate = _candidate()
    first = derive_pair_client_order_id(candidate, candidate.leg_a)
    assert first == derive_pair_client_order_id(_candidate(), _candidate().leg_a)

    amended = _candidate()
    amended.revision = 1
    assert derive_pair_client_order_id(amended, amended.leg_a) != first

    resized = _candidate(quantity=D("50"))
    assert derive_pair_client_order_id(resized, resized.leg_a) != first
    # The two legs of one pair must never collide onto one key.
    assert derive_pair_client_order_id(candidate, candidate.leg_b) != first


# --------------------------------------------------------- refusing to trade
def test_an_unproven_linkage_is_refused_before_any_order_is_sent():
    executor, clients = _executor()
    journal = executor.run(_candidate(evidence=LinkageEvidence.ASSERTED))

    assert journal.state is PairState.ABANDONED
    assert journal.legs == []
    assert "structural_status=not_guaranteed" in journal.abandon_reason
    # Nothing was consumed from either book.
    assert clients[Platform.KALSHI]._orders == {}


def test_unverified_void_handling_blocks_the_pair():
    """Same question, proven. Cancellation handling, not proven. That is enough to stop."""
    journal, _ = _run(_candidate(can_void=True, covers_void=False))
    assert journal.state is PairState.ABANDONED
    assert "structural_status=not_guaranteed" in journal.abandon_reason


def test_a_stale_quote_blocks_even_when_the_structure_is_proven():
    candidate = _candidate()
    candidate.quote_status = QuoteStatus.STALE
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED
    assert "quote_status=stale" in journal.abandon_reason


def test_unverified_account_access_blocks_the_pair():
    candidate = _candidate()
    candidate.access_status = AccessStatus.UNVERIFIED
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED
    assert "access_status=unverified" in journal.abandon_reason


def test_unequal_share_counts_are_refused_rather_than_silently_rebalanced():
    candidate = _candidate()
    candidate.leg_b = VenueOrderSpec(_poly_no(), D("0.50"), D("90"))
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED
    assert "unequal settlement shares" in journal.abandon_reason


def test_an_unknown_fee_blocks_the_pair_instead_of_counting_as_free():
    candidate = _candidate()
    unpriced = kalshi_contract(KALSHI_TICKER, Outcome.YES, fee_rate=None)
    candidate.leg_a = VenueOrderSpec(unpriced, D("0.45"), D("100"))
    journal, _ = _run(candidate)
    assert journal.state is PairState.ABANDONED
    assert "fee rate is unknown" in journal.abandon_reason


def test_a_failed_recheck_releases_the_reservations():
    funding = _funded()
    executor, _ = _executor(funding=funding)
    journal = executor.run(
        _candidate(), recheck=lambda c: (False, "the Kalshi ask moved to 48c")
    )
    assert journal.state is PairState.ABANDONED
    assert "the Kalshi ask moved to 48c" in journal.abandon_reason
    assert funding.spendable_usd(Platform.KALSHI) == D("1000")
    assert funding.spendable_usd(Platform.POLYMARKET) == D("1000")


# ------------------------------------------------------------------- funding
def test_a_pair_that_cannot_fund_both_legs_takes_neither():
    funding = FundingLedger()
    funding.declare(Platform.KALSHI, D("1000"), source=BalanceSource.OPERATOR_DECLARED)
    funding.declare(Platform.POLYMARKET, D("10"), source=BalanceSource.OPERATOR_DECLARED)
    executor, clients = _executor(funding=funding)
    journal = executor.run(_candidate())

    assert journal.state is PairState.ABANDONED
    assert "polymarket" in journal.abandon_reason
    assert clients[Platform.KALSHI]._orders == {}
    assert funding.spendable_usd(Platform.KALSHI) == D("1000")


def test_an_unread_balance_blocks_rather_than_assuming_solvency():
    funding = FundingLedger()
    funding.declare(Platform.KALSHI, D("1000"), source=BalanceSource.OPERATOR_DECLARED)
    executor, _ = _executor(funding=funding)
    journal = executor.run(_candidate())
    assert journal.state is PairState.ABANDONED
    assert "never read" in journal.abandon_reason


def test_two_candidates_cannot_reserve_the_same_dollars():
    funding = _funded(D("60"))
    executor, _ = _executor(funding=funding, kalshi_depth=D("400"), poly_depth=D("400"))
    first = executor.run(_candidate())
    assert first.state is PairState.RECONCILED

    second = _candidate()
    second.pair_id = "pair-002"
    journal = executor.run(second)
    assert journal.state is PairState.ABANDONED
    assert "unreserved" in journal.abandon_reason


def test_every_reservation_is_settled_by_the_end_of_a_completed_run():
    funding = _funded()
    executor, _ = _executor(funding=funding)
    executor.run(_candidate())
    snapshot = funding.snapshot()
    assert all(r["state"] == "committed" for r in snapshot["reservations"])
    # Committed cash equals what the legs actually spent.
    assert Decimal(snapshot["venues"]["kalshi"]["committed_usd"]) == D("46.7400")
    assert Decimal(snapshot["venues"]["polymarket"]["committed_usd"]) == D("50.0000")


# ----------------------------------------------------------------- structural
def test_a_live_client_cannot_be_handed_to_the_executor():
    class PretendLiveClient:
        venue = Platform.KALSHI

    with pytest.raises(NotASimulation):
        TwoLegExecutor(
            mode=ExecutionMode.SHADOW,
            funding=_funded(),
            clients={Platform.KALSHI: PretendLiveClient()},
        )


def test_the_executor_refuses_a_production_mode():
    with pytest.raises(ValueError):
        TwoLegExecutor(
            mode=ExecutionMode.PRODUCTION_READ_ONLY,
            funding=_funded(),
            clients={},
        )


def test_an_illegal_transition_raises_rather_than_being_written():
    from pmvl_markets.execution.twoleg import PairJournal

    journal = PairJournal(pair_id="p", mode=ExecutionMode.SHADOW)
    with pytest.raises(ValueError):
        journal.transition(PairState.BOTH_FILLED, "skipping the entire lifecycle")


# ------------------------------------------------------------------- helper
def _run(candidate):
    executor, clients = _executor()
    return executor.run(candidate), clients


def test_recovery_settles_a_rehydrated_ledger_and_tolerates_a_missing_one():
    """Recovery must work whether or not the funding state survived the crash.

    A ledger that came back holds reservations the dead process took, and they have to
    reach a terminal state. A ledger that did not come back has nothing to settle, and
    that must not be an error - it is the case recovery exists for.
    """
    k_ladders, p_ladders = _ladders()
    clients = {
        Platform.KALSHI: BookBackedVenueClient(Platform.KALSHI, k_ladders),
        Platform.POLYMARKET: BookBackedVenueClient(Platform.POLYMARKET, p_ladders),
    }
    candidate = _candidate()
    funding = _funded()
    TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=funding, clients=clients
    ).run(candidate)

    # A rehydrated ledger: same reservations, already committed by the first run.
    recovered = TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=funding, clients=clients
    ).recover(candidate)
    assert recovered.state is PairState.RECONCILED
    assert all(
        r["state"] in ("committed", "released")
        for r in funding.snapshot()["reservations"]
    )

    # A ledger that did not survive: nothing to settle, and no crash.
    blank = TwoLegExecutor(
        mode=ExecutionMode.SHADOW, funding=_funded(), clients=clients
    ).recover(candidate)
    assert blank.state is PairState.RECONCILED
    assert blank.arbitrage_completed is True


# ------------------------------------------------------- when the remedy fails
def test_a_close_out_that_times_out_escalates_instead_of_claiming_it_is_hedged():
    """An unwind that did not answer leaves a position nobody has confirmed closed.

    Calling that HEDGED would record an exposure as neutralised on the strength of a
    request that was never acknowledged - the same mistake as treating a submit ack
    as a fill, made at the other end of the trade.
    """
    executor, _ = _executor(
        poly_faults=[Fault.SUBMIT_REJECT], kalshi_faults=[Fault.CLOSE_TIMEOUT]
    )
    journal = executor.run(_candidate())

    assert journal.state is PairState.ESCALATED
    assert "close timed out" in journal.escalation_reason
    assert journal.arbitrage_completed is False
    # Nothing was recorded as closed, so the exposure is still on the books.
    kalshi = journal.leg(f"kalshi:{KALSHI_TICKER}")
    assert kalshi.closed_quantity == D("0")
    assert kalshi.retained_quantity == D("100")


def test_a_partial_close_escalates_and_keeps_the_residual_exposure_visible():
    executor, _ = _executor(
        poly_faults=[Fault.SUBMIT_REJECT], kalshi_faults=[Fault.CLOSE_PARTIAL]
    )
    journal = executor.run(_candidate())

    assert journal.state is PairState.ESCALATED
    assert "only 50 of 100 could be closed" in journal.escalation_reason
    kalshi = journal.leg(f"kalshi:{KALSHI_TICKER}")
    assert kalshi.closed_quantity == D("50")
    # 50 shares of a one-legged position remain, and the payoff is scored on them.
    assert kalshi.retained_quantity == D("50")
    assert journal.realised_payoff.structural_status is StructuralStatus.NOT_GUARANTEED
    assert journal.arbitrage_completed is False
