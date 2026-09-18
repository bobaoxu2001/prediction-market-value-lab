"""Sequential execution: what a floor is worth when the legs cannot fill together.

The atomic number is the one everything upstream computes, and it describes a fill the
venue does not offer. These tests pin the gap between it and what ordering the fills
actually costs, and pin that a structure which only works atomically is labelled
THEORETICAL_ONLY rather than promoted.
"""

from __future__ import annotations

from decimal import Decimal

from pmvl_markets.arbitrage.execution_model import (
    ExecutionModel,
    LegPlan,
    evaluate_sequential,
)
from pmvl_shared.enums import Platform, Side
from pmvl_shared.schemas import BookLevel, OrderBook
from pmvl_shared.timeutil import utcnow

D = Decimal


def _book(mid, asks, bids, side):
    kw = (
        {"yes_asks": asks, "yes_bids": bids}
        if side is Side.YES
        else {"no_asks": asks, "no_bids": bids}
    )
    return OrderBook(
        platform=Platform.KALSHI, platform_market_id=mid, observed_at=utcnow(), **kw
    )


def _leg(mid, side, asks, bids, qty=D("100"), fee_rate=D("0.07")):
    return LegPlan(mid, Platform.KALSHI, _book(mid, asks, bids, side), side, qty,
                   fee_rate, "quadratic")


def _lvl(price, size):
    return BookLevel(price=D(price), size=D(str(size)))


def _pair(thin_top=100, second_level_yes="0.34", second_level_no="0.66"):
    return [
        _leg("LOW", Side.YES,
             [_lvl("0.30", thin_top), _lvl(second_level_yes, 900)], [_lvl("0.28", 500)]),
        _leg("HIGH", Side.NO,
             [_lvl("0.60", thin_top), _lvl(second_level_no, 900)], [_lvl("0.58", 500)]),
    ]


def test_one_adverse_level_eats_most_of_the_edge():
    out = evaluate_sequential(_pair(), D("100"))
    assert out.atomic_net_floor > D("6")
    assert out.sequential_net_floor < out.atomic_net_floor
    # A $6.85 edge survives as under a dollar once the top of the second leg is gone.
    assert D("0") < out.sequential_net_floor < D("2")


def test_a_failed_second_leg_costs_the_spread_and_both_fees():
    out = evaluate_sequential(_pair(), D("100"))
    assert out.failure_rescue_floor < 0
    # Losing is several times larger than winning, which is the number that decides
    # whether a thin edge is worth attempting at all.
    assert abs(out.failure_rescue_floor) > out.sequential_net_floor


def test_a_structure_that_only_works_atomically_is_labelled_theoretical_only():
    """Second level priced so the sequential completion no longer clears."""
    legs = _pair(second_level_yes="0.55", second_level_no="0.80")
    out = evaluate_sequential(legs, D("100"))
    assert out.atomic_net_floor > 0
    assert out.sequential_net_floor is None or out.sequential_net_floor <= 0
    assert out.theoretical_only is True


def test_depth_is_what_makes_sequencing_survivable():
    """A single deep level absorbs the adverse fill; the floor barely moves.

    This test caught a modelling error: an earlier version consumed whole price
    levels, so one resting 5,000 lots counted as entirely gone while a thin two-level
    book survived - punishing exactly the depth that makes sequencing safe. The
    adverse move is a quantity, not a level.
    """
    legs = [
        _leg("LOW", Side.YES, [_lvl("0.30", 5000)], [_lvl("0.29", 5000)]),
        _leg("HIGH", Side.NO, [_lvl("0.60", 5000)], [_lvl("0.59", 5000)]),
    ]
    out = evaluate_sequential(legs, D("100"))
    assert out.sequential_net_floor == out.atomic_net_floor
    assert out.theoretical_only is False


def test_the_worst_fill_ordering_is_the_one_reported():
    """Which leg goes first changes the answer; the honest number is the worse one."""
    legs = [
        # Cheap to lift first, brutal to complete second.
        _leg("LOW", Side.YES, [_lvl("0.30", 100), _lvl("0.31", 900)], [_lvl("0.29", 500)]),
        _leg("HIGH", Side.NO, [_lvl("0.60", 100), _lvl("0.69", 900)], [_lvl("0.50", 500)]),
    ]
    out = evaluate_sequential(legs, D("100"))
    # Leading with LOW forces the expensive HIGH completion, so that ordering governs.
    assert out.sequential_net_floor < out.atomic_net_floor - D("5")


def test_a_leg_with_no_bid_depth_cannot_be_rescued():
    legs = [
        _leg("LOW", Side.YES, [_lvl("0.30", 100), _lvl("0.34", 900)], []),
        _leg("HIGH", Side.NO, [_lvl("0.60", 100), _lvl("0.66", 900)], [_lvl("0.58", 500)]),
    ]
    out = evaluate_sequential(legs, D("100"))
    assert any("cannot be unwound" in n for n in out.notes)


def test_an_unknown_fee_rate_refuses_rather_than_pricing_it_free():
    legs = _pair()
    legs[0] = LegPlan(
        legs[0].contract_key, legs[0].venue, legs[0].book, legs[0].side,
        legs[0].quantity, None, "quadratic",
    )
    out = evaluate_sequential(legs, D("100"))
    assert out.atomic_net_floor is None
    assert out.model is ExecutionModel.TAKER_SEQUENTIAL_WORST_CASE


def test_insufficient_depth_after_the_adverse_move_is_reported_not_ignored():
    legs = [
        _leg("LOW", Side.YES, [_lvl("0.30", 100)], [_lvl("0.28", 500)]),
        _leg("HIGH", Side.NO, [_lvl("0.60", 100)], [_lvl("0.58", 500)]),
    ]
    out = evaluate_sequential(legs, D("100"))
    # Exactly enough for one fill: once the structure's own size is lifted, nothing
    # is left to complete against.
    assert out.sequential_net_floor is None
    assert out.notes
    assert out.theoretical_only is True
