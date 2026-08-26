"""What a result is allowed to claim.

Every number this system produces about a strategy carries one of these labels,
and the label is computed from what actually exists rather than asserted by
whoever wrote the report.

The distinction that matters most is between **RESEARCH_ONLY** and everything
above it. A backtest run on fixture data, or on a period whose parameters were
chosen by looking at that same period, is a check that the machinery works. It is
not evidence about money, and presenting it as though it were is the failure mode
this whole exercise exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Sequence


class EvidenceStatus(str, Enum):
    """How much weight a result can bear. Ordered weakest to strongest."""

    #: Not enough observations to say anything, including "it does not work".
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    #: Ran on synthetic or fixture data, or on a period used for development.
    #: Proves the machinery executes; proves nothing about money.
    RESEARCH_ONLY = "RESEARCH_ONLY"
    #: Decisions were frozen before the outcome was known, and outcomes were
    #: appended afterwards. Real evidence, no capital at risk.
    SHADOW_ONLY = "SHADOW_ONLY"
    #: Held-out period never used for parameter selection, with enough
    #: observations to separate the result from noise.
    VERIFIED_OOS = "VERIFIED_OOS"


#: Minimum settled, independent decisions before a result is more than noise.
#:
#: Not a statistical derivation - the observations are not independent draws from
#: a common distribution, and pretending otherwise would be its own overclaim.
#: It is the same floor `pmvl readiness` already applies to the live track
#: record, kept identical so two parts of the system cannot disagree about what
#: "enough" means.
MIN_DECISIONS_FOR_EVIDENCE = 60


@dataclass(frozen=True)
class EvidenceAssessment:
    """The label, and every reason it is not a stronger one."""

    status: EvidenceStatus
    decisions: int
    settled_decisions: int
    #: Reasons the status is not VERIFIED_OOS. Empty only when it is.
    limitations: tuple[str, ...] = ()
    dataset_period: tuple[datetime, datetime] | None = None
    markets: int = 0
    notes: tuple[str, ...] = ()

    @property
    def may_claim_profitability(self) -> bool:
        """Whether any statement about profit is defensible from this result.

        The only status that clears it. Everything else is machinery working, a
        sample too small to read, or evidence that has not yet been out of
        sample.
        """
        return self.status is EvidenceStatus.VERIFIED_OOS

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "decisions": self.decisions,
            "settled_decisions": self.settled_decisions,
            "markets": self.markets,
            "limitations": list(self.limitations),
            "dataset_period": (
                [self.dataset_period[0].isoformat(), self.dataset_period[1].isoformat()]
                if self.dataset_period
                else None
            ),
            "notes": list(self.notes),
            "may_claim_profitability": self.may_claim_profitability,
        }


def assess(
    *,
    decisions: int,
    settled_decisions: int,
    synthetic: bool,
    held_out: bool,
    parameters_fitted_on_this_period: bool,
    shadow_frozen: bool = False,
    markets: int = 0,
    dataset_period: tuple[datetime, datetime] | None = None,
    extra_limitations: Sequence[str] = (),
) -> EvidenceAssessment:
    """Compute the status from what is true, never from what is hoped.

    Deliberately ordered so that the weakest disqualifier wins. Synthetic data
    caps the result at RESEARCH_ONLY no matter how many observations it contains,
    because a million fixture trades are a million facts about the fixture.
    """
    limitations: list[str] = list(extra_limitations)

    if synthetic:
        limitations.append(
            "ran on synthetic or fixture data; this is a check that the "
            "machinery executes, not evidence about money"
        )
    if parameters_fitted_on_this_period:
        limitations.append(
            "parameters were selected against this same period; the result is "
            "in-sample and overstates what an untouched period would show"
        )
    if not held_out:
        limitations.append("no untouched out-of-sample period was reserved")
    if settled_decisions < MIN_DECISIONS_FOR_EVIDENCE:
        limitations.append(
            f"{settled_decisions} settled decisions against the "
            f"{MIN_DECISIONS_FOR_EVIDENCE} needed to separate a result from noise"
        )

    if decisions == 0 or settled_decisions == 0:
        status = EvidenceStatus.INSUFFICIENT_DATA
    elif synthetic:
        status = EvidenceStatus.RESEARCH_ONLY
    elif settled_decisions < MIN_DECISIONS_FOR_EVIDENCE:
        status = EvidenceStatus.INSUFFICIENT_DATA
    elif parameters_fitted_on_this_period or not held_out:
        status = (
            EvidenceStatus.SHADOW_ONLY if shadow_frozen else EvidenceStatus.RESEARCH_ONLY
        )
    elif shadow_frozen:
        status = EvidenceStatus.VERIFIED_OOS
    else:
        status = EvidenceStatus.VERIFIED_OOS

    return EvidenceAssessment(
        status=status,
        decisions=decisions,
        settled_decisions=settled_decisions,
        limitations=tuple(limitations),
        dataset_period=dataset_period,
        markets=markets,
    )


@dataclass(frozen=True)
class ResearchSplit:
    """Train / validation / untouched-out-of-sample boundaries.

    Three periods, not two, and the third is the point. Parameters are chosen on
    train, compared on validation, and the final period is looked at **once**.
    Selecting anything against it converts it into another validation set and
    there is no longer an out-of-sample claim to make.

    :meth:`describe` exists so a report can print the boundaries beside the
    result. A split nobody can see is a split nobody can check.
    """

    train_start: datetime
    train_end: datetime
    validation_end: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        ordered = [self.train_start, self.train_end, self.validation_end, self.test_end]
        if ordered != sorted(ordered):
            raise ValueError(
                "research split boundaries are out of order; overlapping periods "
                "make every out-of-sample claim meaningless"
            )

    def phase_of(self, when: datetime) -> str:
        if when < self.train_start:
            return "before"
        if when < self.train_end:
            return "train"
        if when < self.validation_end:
            return "validation"
        if when < self.test_end:
            return "test"
        return "after"

    def describe(self) -> dict[str, str]:
        return {
            "train": f"{self.train_start.isoformat()} .. {self.train_end.isoformat()}",
            "validation": f"{self.train_end.isoformat()} .. {self.validation_end.isoformat()}",
            "test": f"{self.validation_end.isoformat()} .. {self.test_end.isoformat()}",
        }


def sufficient_history(
    span: timedelta, decisions: int, *, minimum_span: timedelta = timedelta(days=42)
) -> tuple[bool, str]:
    """Whether a three-way split is even worth constructing.

    Returns the verdict and the sentence to print when it is negative. Cutting a
    two-week window into three periods produces three windows too short to say
    anything, and calling the last one "out of sample" makes a claim the data
    cannot support. Saying so is the correct output.
    """
    if span < minimum_span:
        return False, (
            f"available point-in-time history spans {span.days} days, below the "
            f"{minimum_span.days} needed for a train/validation/test split. "
            "Splitting it anyway would produce three periods too short to read "
            "and an out-of-sample label that means nothing."
        )
    if decisions < MIN_DECISIONS_FOR_EVIDENCE * 3:
        return False, (
            f"{decisions} decisions available; a three-way split needs at least "
            f"{MIN_DECISIONS_FOR_EVIDENCE * 3} for the final period to carry "
            f"{MIN_DECISIONS_FOR_EVIDENCE}."
        )
    return True, ""


#: The sentence to print when there is no economic evidence at all.
#:
#: A constant so that every surface prints the same words and none of them can
#: soften it into "preliminary results" during a rewrite.
NO_VERIFIED_BACKTEST = "NO VERIFIED ECONOMIC BACKTEST YET"


@dataclass
class ResearchRecord:
    """A result, its evidence status, and everything needed to reproduce it.

    ``as_dict`` is the shape a future Alpha Terminal would read. The evidence
    status travels *with* the numbers rather than beside them, so there is no
    route by which an ROI reaches a display without the label that says what it
    is worth.
    """

    strategy: str
    evidence: EvidenceAssessment
    metrics: dict[str, Any] = field(default_factory=dict)
    segments: dict[str, Any] = field(default_factory=dict)
    fill_assumptions: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "evidence": self.evidence.as_dict(),
            "metrics": self.metrics,
            "segments": self.segments,
            "fill_assumptions": self.fill_assumptions,
            "versions": self.versions,
            "headline": (
                NO_VERIFIED_BACKTEST
                if not self.evidence.may_claim_profitability
                else f"{self.strategy}: out-of-sample result"
            ),
        }
