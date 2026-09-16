"""The settlement evaluator, tested against the states that actually lose money.

Each test names a way two markets that "obviously match" fail to pay together. The
evaluator's job is to find those states without being told they exist, from the rules
alone - so every test here constructs rules and positions, and never hands the
evaluator a verdict.
"""

from __future__ import annotations

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
from pmvl_shared.enums import Platform, SettlementResult, Side

D = Decimal


def _rules(**kwargs):
    defaults = dict(can_void=False, void_payout=VoidPayout.UNKNOWN)
    defaults.update(kwargs)
    return {
        "k": ContractRule("k", Platform.KALSHI, "e1", **defaults),
        "p": ContractRule("p", Platform.POLYMARKET, "e1", **defaults),
    }


def _legs(k_qty=D("100"), p_qty=D("100"), k_price=D("0.45"), p_price=D("0.50"), k_fee=D("1.74")):
    legs = []
    if k_qty > 0:
        legs.append(HeldLeg("k", Side.YES, k_qty, k_price, k_fee))
    if p_qty > 0:
        legs.append(HeldLeg("p", Side.NO, p_qty, p_price, D("0")))
    return legs


def _proven(covers_void=True):
    return [
        SettlementLinkage("k", "p", LinkageRelation.IDENTICAL, LinkageEvidence.PROVEN,
                          covers_void=covers_void)
    ]


# ------------------------------------------------------------------- the basics
def test_complementary_legs_on_a_proven_pair_are_guaranteed():
    report = evaluate_payoff(_legs(), _rules(), _proven())
    assert report.structural_status is StructuralStatus.GUARANTEED
    assert report.min_payout_usd == D("100")
    assert report.worst_case_net_usd == D("3.26")
    assert report.exhaustive is True
    # Only (YES,YES) and (NO,NO) survive; the divergent pair is impossible.
    assert report.scenarios_considered == 2
    assert report.scenarios_pruned == 2


def test_the_evaluator_takes_no_verdict_from_its_caller():
    """There is no parameter through which a caller can assert safety."""
    import inspect

    params = set(inspect.signature(evaluate_payoff).parameters)
    assert params == {"legs", "rules", "linkages", "extra_cost_usd"}


# ------------------------------------------------------- the states that lose
def test_divergent_settlement_is_reachable_without_a_proven_linkage():
    report = evaluate_payoff(_legs(), _rules(), [])
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    # Kalshi settles NO while Polymarket settles YES: both legs pay nothing.
    assert report.min_payout_usd == D("0")
    assert report.worst_scenario is not None
    assert dict(report.worst_scenario.results) == {
        "k": SettlementResult.NO,
        "p": SettlementResult.YES,
    }


def test_an_asserted_linkage_prunes_nothing_and_is_named_as_ignored():
    """A title match, an embedding hit, or an LLM's opinion is not a proof."""
    linkages = [
        SettlementLinkage("k", "p", LinkageRelation.IDENTICAL, LinkageEvidence.ASSERTED,
                          covers_void=True)
    ]
    report = evaluate_payoff(_legs(), _rules(), linkages)
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    assert report.scenarios_pruned == 0
    assert any("not proven" in line for line in report.ignored_linkages)


def test_one_venue_voiding_while_the_other_settles_defeats_the_pair():
    """Same question, proven. Cancellation policy, unproven. The gap is the risk."""
    report = evaluate_payoff(
        _legs(), _rules(can_void=True, void_payout=VoidPayout.REFUND_COST),
        _proven(covers_void=False),
    )
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    worst = dict(report.worst_scenario.results)
    assert SettlementResult.VOID in worst.values()
    assert len({*worst.values()}) == 2  # one voids, the other does not


def test_proving_void_handling_too_restores_the_pair_to_a_refund():
    """Aligned voids refund the stake but keep the fee, so this is riskless, not free."""
    report = evaluate_payoff(
        _legs(), _rules(can_void=True, void_payout=VoidPayout.REFUND_COST),
        _proven(covers_void=True),
    )
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    assert report.worst_case_net_usd == D("-1.74")  # exactly the Kalshi fee
    assert dict(report.worst_scenario.results) == {
        "k": SettlementResult.VOID,
        "p": SettlementResult.VOID,
    }


def test_even_a_full_refund_makes_a_voidable_pair_riskless_rather_than_profitable():
    """A void that returns price *and* fee gives the stake back and nothing more.

    So no pair whose contracts can void is ever a guaranteed *profit*: the best the
    void branch can do is break even. This is why rule-level void exclusion carries
    so much weight - it is the only way the worst state is a settlement rather than
    a refund.
    """
    report = evaluate_payoff(
        _legs(), _rules(can_void=True, void_payout=VoidPayout.REFUND_TOTAL),
        _proven(covers_void=True),
    )
    assert report.structural_status is StructuralStatus.NO_LOSS
    assert report.worst_case_net_usd == D("0")
    assert report.can_lose is False
    assert report.guaranteed is False


def test_a_fifty_fifty_resolution_is_enumerated_and_pays_the_pair_in_full():
    """Holding both sides means a 50-50 split pays the same as a clean settlement."""
    rules = _rules(can_fifty_fifty=True, can_void=False)
    report = evaluate_payoff(_legs(), rules, _proven(covers_void=True))
    # YES/YES, NO/NO and 50-50/50-50 survive; the six divergent states do not.
    assert report.scenarios_considered == 3
    assert report.scenarios_pruned == 6
    # $0.50 on each of two 100-share legs is the same $100 either side pays alone.
    assert report.min_payout_usd == D("100")
    assert report.structural_status is StructuralStatus.GUARANTEED


def test_a_one_sided_fifty_fifty_halves_the_payout_and_breaks_the_guarantee():
    """Only Polymarket can split. That asymmetry is itself the risk."""
    rules = _rules(can_void=False)
    rules["p"] = ContractRule("p", Platform.POLYMARKET, "e1", can_void=False,
                              can_fifty_fifty=True)
    report = evaluate_payoff(_legs(), rules, _proven(covers_void=True))
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    # Kalshi settles NO (its YES leg pays nothing) while Polymarket splits: $50.
    assert report.min_payout_usd == D("50")


def test_an_unpublished_void_payout_is_bounded_at_zero_not_assumed_free():
    report = evaluate_payoff(
        _legs(), _rules(can_void=True, void_payout=VoidPayout.UNKNOWN),
        _proven(covers_void=True),
    )
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    assert report.min_payout_usd == D("0")
    assert any("bounded at $0" in a for a in report.assumptions)
    assert report.worst_scenario.bounded_legs


def test_excluding_voids_records_the_administrative_residual():
    report = evaluate_payoff(_legs(), _rules(can_void=False), _proven())
    assert report.structural_status is StructuralStatus.GUARANTEED
    assert any("administrative void" in a for a in report.assumptions)


# --------------------------------------------------------- unbalanced baskets
def test_a_single_leg_is_never_guaranteed():
    report = evaluate_payoff(_legs(p_qty=D("0")), _rules(), _proven())
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    assert report.min_payout_usd == D("0")
    assert report.worst_case_net_usd == D("-46.74")


def test_unequal_share_counts_leave_the_difference_naked():
    report = evaluate_payoff(_legs(k_qty=D("100"), p_qty=D("60")), _rules(), _proven())
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    # If it settles NO, the 60 Polymarket NO shares pay $60 against $76.74 spent.
    assert report.min_payout_usd == D("60")


def test_equal_dollars_is_not_a_hedge():
    """$45 of a 45c contract is 100 shares; $45 of a 50c contract is 90.

    Sizing by notional leaves the pair long 10 shares of one outcome, and the
    evaluator finds the state where those 10 pay nothing.
    """
    legs = [
        HeldLeg("k", Side.YES, D("100"), D("0.45"), D("1.74")),
        HeldLeg("p", Side.NO, D("90"), D("0.50"), D("0")),
    ]
    report = evaluate_payoff(legs, _rules(), _proven())
    # The payout is not constant across states - $100 if it settles YES, $90 if NO -
    # which is the definition of a directional position rather than a hedge.
    assert report.min_payout_usd == D("90")
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    assert report.worst_case_net_usd == D("-1.74")


def test_both_legs_on_the_same_side_is_a_doubled_bet_not_an_arbitrage():
    legs = [
        HeldLeg("k", Side.YES, D("100"), D("0.45"), D("0")),
        HeldLeg("p", Side.YES, D("100"), D("0.50"), D("0")),
    ]
    report = evaluate_payoff(legs, _rules(), _proven())
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
    assert report.min_payout_usd == D("0")


# ------------------------------------------------------------------ polarity
def test_a_complementary_linkage_inverts_which_pairing_is_hedged():
    """When one venue words the question as the negation, YES/YES is the hedge."""
    legs = [
        HeldLeg("k", Side.YES, D("100"), D("0.45"), D("0")),
        HeldLeg("p", Side.YES, D("100"), D("0.50"), D("0")),
    ]
    linkages = [
        SettlementLinkage("k", "p", LinkageRelation.COMPLEMENTARY,
                          LinkageEvidence.PROVEN, covers_void=True)
    ]
    report = evaluate_payoff(legs, _rules(), linkages)
    assert report.structural_status is StructuralStatus.GUARANTEED
    assert report.min_payout_usd == D("100")


# ----------------------------------------------------------- refusing to answer
def test_a_missing_rule_is_unknown_not_unguaranteed():
    report = evaluate_payoff(_legs(), {"k": _rules()["k"]}, _proven())
    assert report.structural_status is StructuralStatus.UNKNOWN
    assert report.exhaustive is False
    assert report.min_payout_usd is None
    assert any("no settlement rule supplied for p" in r for r in report.reasons)


def test_unknown_is_not_treated_as_safe():
    report = evaluate_payoff(_legs(), {"k": _rules()["k"]}, _proven())
    assert report.guaranteed is False
    assert report.can_lose is True


def test_contradictory_linkages_report_inconsistency_rather_than_a_proof():
    linkages = [
        SettlementLinkage("k", "p", LinkageRelation.IDENTICAL, LinkageEvidence.PROVEN,
                          covers_void=True),
        SettlementLinkage("k", "p", LinkageRelation.COMPLEMENTARY,
                          LinkageEvidence.PROVEN, covers_void=True),
    ]
    report = evaluate_payoff(_legs(), _rules(), linkages)
    assert report.structural_status is StructuralStatus.UNKNOWN
    assert any("inconsistent" in r for r in report.reasons)


def test_no_legs_is_unknown_rather_than_a_vacuous_guarantee():
    report = evaluate_payoff([], _rules(), _proven())
    assert report.structural_status is StructuralStatus.UNKNOWN


def test_a_linkage_naming_an_absent_contract_is_ignored_not_applied():
    linkages = [
        SettlementLinkage("k", "elsewhere", LinkageRelation.IDENTICAL,
                          LinkageEvidence.PROVEN, covers_void=True)
    ]
    report = evaluate_payoff(_legs(), _rules(), linkages)
    assert report.scenarios_pruned == 0
    assert any("not in the basket" in line for line in report.ignored_linkages)


# ---------------------------------------------------------------- extra costs
def test_costs_outside_the_legs_count_against_the_payout():
    report = evaluate_payoff(_legs(), _rules(), _proven(), extra_cost_usd=D("4"))
    assert report.total_cost_usd == D("100.74")
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED


def test_break_even_is_reported_as_no_loss_rather_than_as_a_guarantee():
    legs = [
        HeldLeg("k", Side.YES, D("100"), D("0.50"), D("0")),
        HeldLeg("p", Side.NO, D("100"), D("0.50"), D("0")),
    ]
    report = evaluate_payoff(legs, _rules(), _proven())
    assert report.structural_status is StructuralStatus.NO_LOSS
    assert report.worst_case_net_usd == D("0")
    assert report.guaranteed is False
    assert report.can_lose is False


def test_a_void_claim_contradicted_by_the_rules_is_withdrawn_not_believed():
    """The linkage says cancellation matches; the rules say only one can split.

    The rules win. The YES/NO pruning the linkage proved from the question survives,
    but the void claim is dropped and the asymmetric states stay in the space - which
    is where the money is lost.
    """
    rules = _rules(can_void=False)
    rules["p"] = ContractRule("p", Platform.POLYMARKET, "e1", can_void=False,
                              can_fifty_fifty=True)
    report = evaluate_payoff(_legs(), rules, _proven(covers_void=True))

    assert any("void claim was withdrawn" in line for line in report.ignored_linkages)
    # (YES,NO) and (NO,YES) are still pruned - the question was genuinely proven.
    assert report.scenarios_pruned == 2
    assert report.structural_status is StructuralStatus.NOT_GUARANTEED
