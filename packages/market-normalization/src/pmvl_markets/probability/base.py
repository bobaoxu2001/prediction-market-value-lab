"""Probability model interface and the independence rule.

The single most important invariant in this package:

    **A model may not use the target market's own price to justify trading against
    that same price.**

Doing so produces circular "edge": the estimate follows the quote, the difference is
noise plus a fee-shaped bias, and the resulting recommendation is indistinguishable
from randomness. Every component therefore declares whether it is *independent* of
the target market, and :class:`ProbabilityModel.estimate` is handed only the
information it is allowed to see.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from pmvl_shared.enums import Category
from pmvl_shared.money import ZERO
from pmvl_shared.schemas import EvidenceRecord, NormalizedMarket, OrderBook, ProbabilityComponent


@dataclass
class ModelContext:
    """Everything a probability model is permitted to look at.

    ``target_book`` is present because models legitimately need the *spread and
    depth* of the target market to judge liquidity, and because the ranking layer
    needs the executable price. Models must not use ``target_book``'s price level as
    evidence for their own estimate; components that do (the reference prior) set
    ``independent=False`` and are excluded from edge-bearing estimates.
    """

    market: NormalizedMarket
    target_book: OrderBook | None = None
    #: Quotes for the *same question* on other venues, keyed by platform value.
    cross_platform_quotes: dict[str, Decimal] = field(default_factory=dict)
    #: Prices of related-but-not-identical markets, used for coherence priors.
    related_market_prices: list[tuple[str, Decimal]] = field(default_factory=list)
    #: Other outcomes of the same event (multi-outcome coherence).
    sibling_outcome_prices: list[tuple[str, Decimal]] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    now: datetime | None = None
    #: Set only by the retrodiction harness. It means: *pretend this is the current
    #: instant, and do not look at anything published after it.*
    #:
    #: This is not the same field as ``now``. ``now`` is what the model uses to
    #: compute time to resolution, and a caller may set it for perfectly ordinary
    #: reasons (a deterministic test, a snapshot replayed a few minutes late).
    #: ``as_of`` is a much stronger claim, and it is the only thing standing between
    #: a historical evaluation and a look-ahead result: with it set, "spot price"
    #: must mean the spot price *then*, not the spot price now.
    #:
    #: A model that cannot honour it must decline - see :func:`lookahead_guard`.
    #: Fetching current data under an ``as_of`` would not fail loudly; it would
    #: quietly produce a spectacular backtest, which is the single most expensive
    #: mistake this repository could make.
    as_of: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_retrodiction(self) -> bool:
        return self.as_of is not None

    @property
    def evaluation_time(self) -> datetime | None:
        """The instant the model should treat as the present."""
        return self.as_of or self.now


@dataclass
class ModelEstimate:
    """One component's output.

    ``probability`` of ``None`` means "no opinion" - the model had insufficient data.
    That is a first-class, honest result and is strictly better than emitting 0.5
    with low confidence, which would drag the ensemble toward a coin flip.
    """

    probability: Decimal | None
    confidence: Decimal = ZERO
    #: One standard deviation of the estimate, used to build the interval.
    stdev: Decimal | None = None
    independent: bool = True
    detail: str = ""
    data_freshness_seconds: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def has_opinion(self) -> bool:
        return self.probability is not None and self.confidence > 0

    def to_component(self, name: str, weight: Decimal) -> ProbabilityComponent:
        return ProbabilityComponent(
            name=name,
            probability=self.probability,
            weight=weight,
            confidence=self.confidence,
            detail=self.detail,
            data={
                **self.data,
                "independent": self.independent,
                "stdev": str(self.stdev) if self.stdev is not None else None,
                "data_freshness_seconds": self.data_freshness_seconds,
            },
        )


class ProbabilityModel(ABC):
    """Base class for every ensemble member."""

    #: Stable identifier recorded on every prediction for reproducibility.
    name: str = "base"
    #: Categories this model claims competence in. Empty means "any".
    categories: tuple[Category, ...] = ()
    #: Whether this model's output may be used to demonstrate an edge against the
    #: target market's own quote.
    independent: bool = True
    #: Ceiling on this model's confidence, expressing structural trust in the method.
    max_confidence: Decimal = Decimal("1")
    #: Whether this model can reconstruct its inputs as they stood at a past instant.
    #:
    #: Defaults to False, and the default is the point. A model that has not been
    #: audited for look-ahead is not presumed safe to replay; it is excluded from
    #: historical evaluation until someone reads its data path and says otherwise.
    supports_as_of: bool = False

    def handles(self, market: NormalizedMarket) -> bool:
        return not self.categories or market.category in self.categories

    @abstractmethod
    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        """Return this model's opinion, or ``ModelEstimate(None)`` if it has none."""

    async def aclose(self) -> None:
        """Release any provider clients. Default: nothing to do."""
        return None


def no_opinion(detail: str) -> ModelEstimate:
    """Helper for the common 'not enough data' path."""
    return ModelEstimate(probability=None, confidence=ZERO, detail=detail)


def lookahead_guard(model: ProbabilityModel, ctx: ModelContext) -> ModelEstimate | None:
    """Refuse to answer a retrodiction the model cannot honestly answer.

    Returns a ``no_opinion`` estimate when ``ctx.as_of`` is set and the model has not
    declared ``supports_as_of``; otherwise ``None``, meaning carry on.

    Every model consulted by the historical harness calls this first. Declining is
    the safe direction: a category missing from the historical evaluation understates
    what the system can do, whereas a category that silently answered with today's
    data overstates it, and only one of those two errors gets caught by a reader
    looking at a Brier score.
    """
    if ctx.as_of is None or model.supports_as_of:
        return None
    return no_opinion(
        f"{model.name} cannot reconstruct its inputs as of "
        f"{ctx.as_of.isoformat()}; declining rather than using present-day data"
    )


def clamp_confidence(value: Decimal, ceiling: Decimal) -> Decimal:
    return max(ZERO, min(value, ceiling))


def freshest(seconds: Sequence[int | None]) -> int | None:
    present = [s for s in seconds if s is not None]
    return min(present) if present else None
