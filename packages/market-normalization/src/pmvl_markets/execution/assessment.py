"""Six independent verdicts about one candidate, and the rule that keeps them apart.

A single ``label`` field forces unrelated facts to compete for one slot, and the last
writer wins. That is how a pair whose settlement rules were never verified ends up
described as ``STALE_QUOTE`` - true, but it buries the worse problem - and how
re-reading the book at a fresher timestamp appears to *fix* it.

So the axes are stored separately and answer different questions:

``structural_status``
    Can this position lose money if both venues behave exactly as published? Decided
    only by :mod:`pmvl_markets.settlement.payoff`, from rules and fills.
``quote_status``
    Is the market data recent and deep enough to believe the prices?
``economics_status``
    After every fee and cost, is there still an edge worth the trouble?
``execution_status``
    Did the two legs actually end up matched, and is anything still open?
``access_status``
    Is this account permitted to trade these markets at all?
``residual_risks``
    Everything true and unquantified. Never empty just because the others are green.

:meth:`CandidateAssessment.tradeable` requires all five axes to pass at once. It is
written as an ``and`` of five explicit checks rather than a score, because a score
lets four green axes outvote ``structural_status=NOT_GUARANTEED``, and that one is
not outvotable: no amount of freshness makes an unverified settlement rule safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..settlement.payoff import StructuralStatus

__all__ = [
    "QuoteStatus",
    "EconomicsStatus",
    "ExecutionStatus",
    "AccessStatus",
    "CandidateAssessment",
]


class QuoteStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    THIN = "thin"                # priced, but the depth does not support the size
    MISSING = "missing"


class EconomicsStatus(StrEnum):
    NET_POSITIVE = "net_positive"
    NET_NEGATIVE = "net_negative"
    BELOW_MIN_EDGE = "below_min_edge"     # positive, but under the required margin
    UNKNOWN_COSTS = "unknown_costs"       # a fee or cost could not be determined


class ExecutionStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    MATCHED = "matched"                   # both legs filled in the intended ratio
    UNMATCHED = "unmatched"               # one leg is exposed
    UNRESOLVED = "unresolved"             # a venue's answer is still unknown
    NEUTRALISED = "neutralised"           # exposure closed out, possibly at a loss
    FAILED = "failed"


class AccessStatus(StrEnum):
    #: Confirmed by reading the account's own permissions from the venue.
    PERMITTED = "permitted"
    #: The venue says this account may not trade this market or from this region.
    RESTRICTED = "restricted"
    #: Never checked. Treated as blocking, not as permission.
    UNVERIFIED = "unverified"


@dataclass
class CandidateAssessment:
    """The six axes, plus the single question that combines them."""

    structural_status: StructuralStatus = StructuralStatus.UNKNOWN
    quote_status: QuoteStatus = QuoteStatus.MISSING
    economics_status: EconomicsStatus = EconomicsStatus.UNKNOWN_COSTS
    execution_status: ExecutionStatus = ExecutionStatus.NOT_ATTEMPTED
    access_status: AccessStatus = AccessStatus.UNVERIFIED
    residual_risks: list[str] = field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        """Whether every axis independently permits an attempt.

        ``structural_status`` must be ``GUARANTEED``: ``NO_LOSS`` is riskless but has
        no edge, and ``UNKNOWN`` is an unenumerated state space, which is not a form
        of safety.
        """
        return (
            self.structural_status is StructuralStatus.GUARANTEED
            and self.quote_status is QuoteStatus.FRESH
            and self.economics_status is EconomicsStatus.NET_POSITIVE
            and self.access_status is AccessStatus.PERMITTED
            and self.execution_status
            in (ExecutionStatus.NOT_ATTEMPTED, ExecutionStatus.MATCHED)
        )

    def blocking_reasons(self) -> list[str]:
        """Each axis that is not permitting, named. Empty iff :attr:`tradeable`."""
        reasons: list[str] = []
        if self.structural_status is not StructuralStatus.GUARANTEED:
            reasons.append(f"structural_status={self.structural_status.value}")
        if self.quote_status is not QuoteStatus.FRESH:
            reasons.append(f"quote_status={self.quote_status.value}")
        if self.economics_status is not EconomicsStatus.NET_POSITIVE:
            reasons.append(f"economics_status={self.economics_status.value}")
        if self.access_status is not AccessStatus.PERMITTED:
            reasons.append(f"access_status={self.access_status.value}")
        if self.execution_status not in (
            ExecutionStatus.NOT_ATTEMPTED,
            ExecutionStatus.MATCHED,
        ):
            reasons.append(f"execution_status={self.execution_status.value}")
        return reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "structural_status": self.structural_status.value,
            "quote_status": self.quote_status.value,
            "economics_status": self.economics_status.value,
            "execution_status": self.execution_status.value,
            "access_status": self.access_status.value,
            "residual_risks": list(self.residual_risks),
            "tradeable": self.tradeable,
            "blocking_reasons": self.blocking_reasons(),
        }
