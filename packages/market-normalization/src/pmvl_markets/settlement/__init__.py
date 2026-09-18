"""Settlement payoff evaluation, independent of whatever found the opportunity.

The scanner in :mod:`pmvl_markets.arbitrage` decides *what looks profitable*. This
package decides *what actually pays*, and it is deliberately unable to see the
scanner's conclusion: :func:`evaluate_payoff` takes contract rules and held legs, and
nothing else. A caller cannot hand it ``guaranteed=True``.

That separation exists because the failure mode being defended against is a scanner
that is confidently wrong. If the same code both proposes a pair and certifies it,
one bug produces a certified loss.
"""

from .payoff import (
    ContractRule,
    ExclusiveGroup,
    HeldLeg,
    LinkageEvidence,
    LinkageRelation,
    PayoffReport,
    ScenarioPayoff,
    SettlementLinkage,
    StructuralStatus,
    VoidPayout,
    evaluate_payoff,
)

__all__ = [
    "ContractRule",
    "ExclusiveGroup",
    "HeldLeg",
    "LinkageEvidence",
    "LinkageRelation",
    "PayoffReport",
    "ScenarioPayoff",
    "SettlementLinkage",
    "StructuralStatus",
    "VoidPayout",
    "evaluate_payoff",
]
