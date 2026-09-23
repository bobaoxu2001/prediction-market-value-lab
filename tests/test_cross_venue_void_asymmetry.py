"""The Kalshi/Polymarket sports pair is not floored, and the reason is in the rulebooks.

The two venues price the same claim about the same game, and on a postponement past
48 hours they do entirely different things:

  Kalshi   "If <game> is suspended or postponed and not resumed within 48 hours of the
            original scheduled start time, all Contracts settle to the last fair market
            price prior to suspension"
  Polymarket
           "If the game is postponed, this market will remain open until the game has
            been completed. If the game is canceled entirely, with no make-up game,
            this market will resolve 50-50."

One leg cashes out at an arbitrary quote; the other stays open for as long as it takes.
The holder is left directional on a position bought precisely so it would not be.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pmvl_markets.settlement.payoff import (
    ContractRule,
    HeldLeg,
    LinkageEvidence,
    LinkageRelation,
    SettlementLinkage,
    StructuralStatus,
    VoidPayout,
    evaluate_payoff,
)
from pmvl_shared.enums import Platform, Side

UTC = timezone.utc
CLOSE = datetime(2026, 9, 22, 21, 40, tzinfo=UTC)


def _rule(name: str, void: VoidPayout) -> ContractRule:
    return ContractRule(
        contract_key=name,
        venue=Platform.KALSHI if name.startswith("K") else Platform.POLYMARKET,
        event_key="LAAATH-26SEP22",
        can_void=True,
        void_payout=void,
        rule_source=f"{name} published terms",
        rule_version="2026-09",
    )


def _legs(kalshi_void: VoidPayout, poly_void: VoidPayout):
    """Buy Polymarket YES and Kalshi NO on the same total - the pair that looked +1c."""
    return [
        HeldLeg(
            contract_key="P-over-9.5",
            side=Side.YES,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0.42"),
            fees_usd=Decimal("0"),
        ),
        HeldLeg(
            contract_key="K-over-9.5",
            side=Side.NO,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0.59"),
            fees_usd=Decimal("1.69"),
        ),
    ], {"P-over-9.5": _rule("P-over-9.5", poly_void),
        "K-over-9.5": _rule("K-over-9.5", kalshi_void)}


IDENTICAL = [
    SettlementLinkage(
        contract_a="P-over-9.5",
        contract_b="K-over-9.5",
        relation=LinkageRelation.IDENTICAL,
        evidence=LinkageEvidence.PROVEN,
        covers_void=False,
        note="both settle on the combined run total of the same scheduled game",
    )
]


def test_the_pair_is_guaranteed_only_while_both_legs_actually_settle():
    """With no void at all, the structure is what it claims to be."""
    legs, rules = _legs(VoidPayout.ZERO, VoidPayout.ZERO)
    for rule in rules.values():
        object.__setattr__(rule, "can_void", False)
    report = evaluate_payoff(legs, rules, IDENTICAL)
    assert report.min_payout_usd == Decimal("100")


def test_the_real_rules_destroy_the_floor():
    """Kalshi cashes out at a quote, Polymarket does not settle. Floor goes to zero."""
    legs, rules = _legs(VoidPayout.MARKET_PRICE, VoidPayout.REMAINS_OPEN)
    report = evaluate_payoff(legs, rules, IDENTICAL)

    assert report.structural_status is not StructuralStatus.GUARANTEED
    assert report.min_payout_usd < Decimal("100")
    #: and the cost was 101.69, so the worst case is a real loss, not a smaller win
    assert report.min_payout_usd < Decimal("101.69")


def test_a_market_price_settlement_is_not_a_refund():
    """The tempting misreading. A refund returns the stake; a quote returns a quote."""
    refunded, rules_r = _legs(VoidPayout.REFUND_COST, VoidPayout.REFUND_COST)
    quoted, rules_q = _legs(VoidPayout.MARKET_PRICE, VoidPayout.MARKET_PRICE)

    refund_report = evaluate_payoff(refunded, rules_r, IDENTICAL)
    quote_report = evaluate_payoff(quoted, rules_q, IDENTICAL)
    assert quote_report.min_payout_usd < refund_report.min_payout_usd


def test_an_open_leg_is_worth_nothing_at_its_partners_settlement():
    """"Remains open" is not a payout. It is the absence of one, plus locked capital."""
    legs, rules = _legs(VoidPayout.ZERO, VoidPayout.REMAINS_OPEN)
    report = evaluate_payoff(legs, rules, IDENTICAL)
    assert report.structural_status is not StructuralStatus.GUARANTEED


def test_both_new_modes_are_flagged_rather_than_silently_scored():
    """A zero that came from ignorance has to look different from a zero that is known."""
    for void in (VoidPayout.MARKET_PRICE, VoidPayout.REMAINS_OPEN):
        legs, rules = _legs(void, VoidPayout.ZERO)
        report = evaluate_payoff(legs, rules, IDENTICAL)
        assert report.structural_status is not StructuralStatus.GUARANTEED, void
