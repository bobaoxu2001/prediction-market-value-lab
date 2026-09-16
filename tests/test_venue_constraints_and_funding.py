"""Venue constraints and per-venue funding: the two places a second venue leaks in.

Most of these assertions are about *refusing*, because both modules exist to stop a
Kalshi assumption from being applied to Polymarket and to stop money from being spent
twice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_markets.execution.funding import BalanceSource, FundingLedger
from pmvl_markets.execution.models import Outcome, TimeInForce
from pmvl_markets.execution.venues import (
    ContractConstraintError,
    UnknownFeeError,
    VenueOrderSpec,
    kalshi_contract,
    polymarket_contract,
)
from pmvl_shared.enums import Platform

D = Decimal


def _poly(**kwargs):
    defaults = dict(
        fee_rate=D("0"),
        tick_size=D("0.001"),
        min_quantity=D("5"),
        min_notional_usd=D("1"),
        quantity_step=D("0.01"),
    )
    defaults.update(kwargs)
    return polymarket_contract("token-1", "0xcond", Outcome.NO, **defaults)


# ------------------------------------------------------------------ constraints
def test_polymarket_keeps_its_own_price_grid_rather_than_kalshis_cent():
    poly = _poly()
    assert poly.tick_size == D("0.001")
    assert poly.violations(price=D("0.505"), quantity=D("10")) == []
    # The same price would be off-grid on Kalshi, and vice versa.
    kalshi = kalshi_contract("KXT", Outcome.YES, fee_rate=D("0.07"))
    assert "off the 0.01 tick grid" in " ".join(
        kalshi.violations(price=D("0.505"), quantity=D("10"))
    )


def test_fractional_sizes_are_legal_on_polymarket_and_not_on_kalshi():
    assert _poly().violations(price=D("0.500"), quantity=D("10.25")) == []
    kalshi = kalshi_contract("KXT", Outcome.YES, fee_rate=D("0.07"))
    assert "off the 1 size grid" in " ".join(
        kalshi.violations(price=D("0.50"), quantity=D("10.25"))
    )


def test_a_minimum_notional_is_enforced_where_the_venue_publishes_one():
    poly = _poly(min_quantity=D("1"), min_notional_usd=D("5"))
    assert "below the venue minimum $5" in " ".join(
        poly.violations(price=D("0.100"), quantity=D("10"))
    )


def test_an_unknown_fee_is_a_violation_and_raises_when_charged():
    contract = kalshi_contract("KXT", Outcome.YES, fee_rate=None)
    assert "fee rate is unknown" in " ".join(
        contract.violations(price=D("0.50"), quantity=D("10"))
    )
    with pytest.raises(UnknownFeeError):
        contract.fee_for(D("10"), D("0.50"))


def test_a_time_in_force_the_venue_does_not_support_is_rejected_at_construction():
    contract = kalshi_contract("KXT", Outcome.YES, fee_rate=D("0.07"))
    limited = type(contract)(
        venue=contract.venue,
        contract_key=contract.contract_key,
        market_ref=contract.market_ref,
        outcome=contract.outcome,
        fee_rate=contract.fee_rate,
        supported_tif=frozenset({TimeInForce.GOOD_TILL_CANCELLED}),
    )
    with pytest.raises(ContractConstraintError):
        VenueOrderSpec(limited, D("0.50"), D("10"), TimeInForce.FILL_OR_KILL)


def test_a_buy_limit_rounds_up_to_the_grid_so_it_can_actually_trade():
    """Rounding a buy limit down produces an order that rests and never fills."""
    contract = kalshi_contract("KXT", Outcome.YES, fee_rate=D("0.07"))
    assert contract.round_price_for_buy(D("0.4501")) == D("0.46")
    assert contract.round_price_for_buy(D("0.45")) == D("0.45")


def test_sizes_round_down_so_a_reservation_is_never_exceeded():
    assert _poly().round_quantity_down(D("10.259")) == D("10.25")


def test_the_reservation_covers_the_worst_the_order_can_cost():
    contract = kalshi_contract("KXT", Outcome.YES, fee_rate=D("0.07"))
    spec = VenueOrderSpec(contract, D("0.45"), D("100"))
    assert spec.notional_usd == D("45.00")
    assert spec.max_fee_usd == D("1.74")
    assert spec.max_cash_out_usd == D("46.74")


# ---------------------------------------------------------------------- funding
def test_an_unread_balance_is_not_zero_and_not_infinite():
    ledger = FundingLedger()
    assert ledger.spendable_usd(Platform.POLYMARKET) is None
    outcome = ledger.reserve_all([("r", Platform.POLYMARKET, D("1"))])
    assert outcome.approved is False
    assert "never read" in outcome.reasons[0]


def test_reservations_are_all_or_nothing_across_venues():
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("1000"), source=BalanceSource.VENUE_API)
    ledger.declare(Platform.POLYMARKET, D("10"), source=BalanceSource.VENUE_API)
    outcome = ledger.reserve_all(
        [("a", Platform.KALSHI, D("50")), ("b", Platform.POLYMARKET, D("50"))]
    )
    assert outcome.approved is False
    # The Kalshi leg must not be held either, or the next candidate sees less money
    # than it actually has.
    assert ledger.spendable_usd(Platform.KALSHI) == D("1000")
    assert ledger.reservation("a") is None


def test_two_legs_on_one_venue_are_checked_against_the_combined_figure():
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("75"), source=BalanceSource.VENUE_API)
    outcome = ledger.reserve_all(
        [("a", Platform.KALSHI, D("50")), ("b", Platform.KALSHI, D("50"))]
    )
    assert outcome.approved is False
    assert "needs $100" in outcome.reasons[0]


def test_held_money_is_invisible_to_the_next_candidate():
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("100"), source=BalanceSource.VENUE_API)
    assert ledger.reserve_all([("a", Platform.KALSHI, D("70"))]).approved
    assert ledger.spendable_usd(Platform.KALSHI) == D("30")
    assert ledger.reserve_all([("b", Platform.KALSHI, D("40"))]).approved is False


def test_releasing_a_hold_returns_the_money_and_committing_does_not():
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("100"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("70"))])
    ledger.release("a")
    assert ledger.spendable_usd(Platform.KALSHI) == D("100")

    ledger.reserve_all([("b", Platform.KALSHI, D("70"))])
    ledger.commit("b", D("46.74"))
    # Only what was actually spent stays gone; the unused hold comes back.
    assert ledger.spendable_usd(Platform.KALSHI) == D("53.26")


def test_committed_money_cannot_be_released():
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("100"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("70"))])
    ledger.commit("a", D("70"))
    with pytest.raises(ValueError):
        ledger.release("a")


def test_a_commit_larger_than_its_hold_is_recorded_as_an_overrun():
    """Impossible for a limit buy, so it means the cost model and the fill disagree."""
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("100"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("46.74"))])
    reservation = ledger.commit("a", D("48.00"))
    assert reservation.overrun_usd == D("1.26")


def test_a_refreshed_balance_does_not_release_money_in_flight():
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("100"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("70"))])
    ledger.declare(Platform.KALSHI, D("100"), source=BalanceSource.VENUE_API)
    assert ledger.spendable_usd(Platform.KALSHI) == D("30")


def test_a_duplicate_reservation_ref_is_refused():
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("100"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("10"))])
    outcome = ledger.reserve_all([("a", Platform.KALSHI, D("10"))])
    assert outcome.approved is False
    assert "already exists" in outcome.reasons[0]


def test_a_fresh_venue_read_does_not_deduct_already_spent_money_twice():
    """The venue's own figure already has the spend in it.

    Continuing to subtract a committed reservation after the account has been re-read
    deducts the same dollars twice, and the error compounds: every refresh retires
    nothing, so the ledger's idea of spendable capacity drifts steadily below the
    truth until it refuses trades the account can afford.
    """
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("1000"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("50"))])
    ledger.commit("a", D("46.74"))
    assert ledger.spendable_usd(Platform.KALSHI) == D("953.26")

    # The venue now reports the post-trade balance.
    ledger.declare(Platform.KALSHI, D("953.26"), source=BalanceSource.VENUE_API)
    assert ledger.spendable_usd(Platform.KALSHI) == D("953.26")
    # The record of where the money went survives the retirement.
    assert ledger.reservation("a").committed_usd == D("46.74")
    assert len(ledger.snapshot()["retired_reservations"]) == 1


def test_a_fresh_read_still_subtracts_money_that_has_not_filled_yet():
    """A held reservation is invisible to the venue, so it must keep being subtracted."""
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("1000"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("300"))])
    ledger.declare(Platform.KALSHI, D("1000"), source=BalanceSource.VENUE_API)
    assert ledger.spendable_usd(Platform.KALSHI) == D("700")


def test_a_commit_that_landed_after_the_read_is_not_retired():
    """It is not in that number yet, so it still has to come off."""
    from datetime import timedelta

    from pmvl_shared.timeutil import utcnow

    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("1000"), source=BalanceSource.VENUE_API)
    ledger.reserve_all([("a", Platform.KALSHI, D("50"))])
    ledger.commit("a", D("46.74"))
    # A balance sampled *before* that commit settled.
    ledger.declare(
        Platform.KALSHI, D("1000"), source=BalanceSource.VENUE_API,
        as_of=utcnow() - timedelta(minutes=5),
    )
    assert ledger.spendable_usd(Platform.KALSHI) == D("953.26")


def test_an_operator_bankroll_retires_nothing():
    """A notional bankroll says how much to risk, not what the account holds."""
    ledger = FundingLedger()
    ledger.declare(Platform.KALSHI, D("1000"), source=BalanceSource.OPERATOR_DECLARED)
    ledger.reserve_all([("a", Platform.KALSHI, D("50"))])
    ledger.commit("a", D("46.74"))
    ledger.declare(Platform.KALSHI, D("1000"), source=BalanceSource.OPERATOR_DECLARED)
    assert ledger.spendable_usd(Platform.KALSHI) == D("953.26")
