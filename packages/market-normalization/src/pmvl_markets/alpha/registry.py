"""The alpha factor registry.

A factor is a named, versioned function from *what was knowable at time t* to a
number, plus the metadata that makes a historical computation of it reproducible.

Three properties this exists to enforce:

**A factor declares what it reads.** ``requires`` names the observation kinds and
market fields it consumes. That is not documentation - it is what lets a
research run report "this factor was unavailable on 4,100 of 6,700 markets
because ``nws_forecast`` was missing" instead of quietly emitting zeros.

**A factor may refuse.** :class:`FeatureValue` is either available with a value,
or unavailable with a reason. There is no third state and no default. A factor
that returns ``0.0`` when it has no data is indistinguishable, downstream, from
one that measured zero - and a strategy trained on that mixture has learned the
shape of the missing data.

**A factor is versioned.** ``version`` changes when the computation changes.
A historical feature value carries the version that produced it, so a research
result computed under ``v1`` is never silently compared against ``v2``.

Why a registry rather than a module of functions
------------------------------------------------
Because the interesting question is almost never "what is this factor here" but
"which factors were available across this universe, in this family, at this
time, and what did each of them read". A dict of specs answers that; a set of
imports does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from pmvl_shared.schemas import NormalizedMarket, OrderBook

from .pit import AsOfView


class FactorFamily(str, Enum):
    """The families named in the research plan.

    Kept as an enum so a segmentation report can group by family without
    string-matching names, and so adding a factor to a family that does not
    exist fails at import rather than producing an orphan group.
    """

    SOURCE_DIVERGENCE = "source_divergence"
    SOURCE_LAG = "source_lag"
    THRESHOLD_DISTANCE = "threshold_distance"
    FORECAST_DISPERSION = "forecast_dispersion"
    MICROSTRUCTURE = "microstructure"
    MAKER_TAKER_ECONOMICS = "maker_taker_economics"
    TIME_TO_RESOLUTION = "time_to_resolution"
    PRICE_BUCKET = "price_bucket"
    CROSS_VENUE = "cross_venue"
    SIBLING_COHERENCE = "sibling_coherence"
    CATALYST = "catalyst"
    LIQUIDITY_CAPACITY = "liquidity_capacity"


class Unavailable(str, Enum):
    """Why a factor declined, as a closed vocabulary.

    Closed on purpose. "This factor was unavailable" is a research finding, and
    a free-text reason cannot be counted. Knowing that 80% of refusals were
    ``NO_SOURCE_OBSERVATION`` and 5% were ``STALE_SOURCE`` tells you what data to
    go and get; a bag of sentences does not.
    """

    NO_SOURCE_OBSERVATION = "no_source_observation"
    NO_ORDER_BOOK = "no_order_book"
    NO_QUOTE = "no_quote"
    NO_THRESHOLD = "no_threshold"
    NO_ERROR_SCALE = "no_error_scale"
    STALE_SOURCE = "stale_source"
    INSUFFICIENT_SOURCES = "insufficient_sources"
    NOT_APPLICABLE = "not_applicable"
    NOT_VERIFIED_EQUIVALENT = "not_verified_equivalent"
    NO_CATALYST_SCHEDULE = "no_catalyst_schedule"
    MALFORMED_INPUT = "malformed_input"


@dataclass(frozen=True)
class FeatureValue:
    """A factor's answer: a number, or a reason there is none.

    ``components`` is required by the strategy layer's rule against collapsing
    everything into one score. A factor that combines a gap, a fee and a spread
    keeps all three here, so an attribution report can ask which part of the
    signal was doing the work rather than only how big the total was.
    """

    name: str
    value: Decimal | None
    available: bool
    reason: Unavailable | None = None
    #: Named parts of the value, retained for attribution.
    components: Mapping[str, Decimal] = field(default_factory=dict)
    #: Non-numeric context: which source, which bucket, which venue.
    labels: Mapping[str, str] = field(default_factory=dict)
    #: The oldest ``available_at`` among the inputs, so a caller can tell how far
    #: back the value reaches without re-deriving it.
    inputs_available_at: datetime | None = None
    version: str = "v0"

    @classmethod
    def of(
        cls,
        name: str,
        value: Decimal,
        *,
        components: Mapping[str, Decimal] | None = None,
        labels: Mapping[str, str] | None = None,
        inputs_available_at: datetime | None = None,
        version: str = "v0",
    ) -> "FeatureValue":
        return cls(
            name=name,
            value=value,
            available=True,
            components=dict(components or {}),
            labels=dict(labels or {}),
            inputs_available_at=inputs_available_at,
            version=version,
        )

    @classmethod
    def missing(
        cls,
        name: str,
        reason: Unavailable,
        *,
        labels: Mapping[str, str] | None = None,
        version: str = "v0",
    ) -> "FeatureValue":
        return cls(
            name=name,
            value=None,
            available=False,
            reason=reason,
            labels=dict(labels or {}),
            version=version,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": None if self.value is None else str(self.value),
            "available": self.available,
            "reason": self.reason.value if self.reason else None,
            "components": {k: str(v) for k, v in self.components.items()},
            "labels": dict(self.labels),
            "inputs_available_at": (
                self.inputs_available_at.isoformat()
                if self.inputs_available_at
                else None
            ),
            "version": self.version,
        }


@dataclass(frozen=True)
class FactorContext:
    """Everything a factor is permitted to look at.

    Deliberately shaped like :class:`~pmvl_markets.probability.base.ModelContext`,
    and deliberately different in one respect: there is no unguarded "now". A
    factor computes as of ``view.decision_time`` or not at all, because a factor
    that can read the clock can read the future.

    ``model_fair_probability`` is the **outcome model's** answer, passed in
    rather than computed here. That is the Model A / Model B boundary made
    physical: a factor may use the fair probability as an input, and may not
    reach into the ensemble to produce one.
    """

    market: NormalizedMarket
    view: AsOfView
    book: OrderBook | None = None
    model_fair_probability: Decimal | None = None
    model_fair_probability_low: Decimal | None = None
    model_fair_probability_high: Decimal | None = None
    #: Executable YES/NO probabilities implied by the ask ladder, if priced.
    executable_probability: Decimal | None = None
    #: Sibling markets of the same event: (platform_market_id, probability).
    sibling_probabilities: Sequence[tuple[str, Decimal]] = ()
    #: Verified-equivalent counterparts elsewhere: (platform, probability).
    cross_venue_probabilities: Sequence[tuple[str, Decimal]] = ()
    #: Whether the cross-venue counterparts passed strict equivalence.
    cross_venue_verified: bool = False
    #: Parsed threshold terms, when the rule parser extracted them.
    threshold_value: Decimal | None = None
    threshold_comparator: str = ""
    #: Scheduled future information events: (label, when).
    catalysts: Sequence[tuple[str, datetime]] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def decision_time(self) -> datetime:
        return self.view.decision_time


#: A factor is a plain callable. No base class, because there is no state worth
#: inheriting and a function is easier to test than a subclass.
FactorFn = Callable[[FactorContext], FeatureValue]


@dataclass(frozen=True)
class FactorSpec:
    """A registered factor and everything needed to reproduce it."""

    name: str
    family: FactorFamily
    version: str
    description: str
    compute: FactorFn
    #: Observation kinds and market fields this reads. Names, not types: the
    #: point is to be able to report coverage per input.
    requires: tuple[str, ...] = ()
    #: True when the factor's value cannot be computed without an order book.
    needs_book: bool = False
    #: True when the factor's value depends on the outcome model's estimate.
    needs_outcome_model: bool = False

    def __call__(self, ctx: FactorContext) -> FeatureValue:
        """Compute, and stamp the version onto whatever comes back.

        Stamped here rather than trusted from the factor body so a factor cannot
        report a version it does not have, and so bumping a version is one edit
        in the registration rather than two that can disagree.
        """
        value = self.compute(ctx)
        if value.version == self.version and value.name == self.name:
            return value
        return FeatureValue(
            name=self.name,
            value=value.value,
            available=value.available,
            reason=value.reason,
            components=value.components,
            labels=value.labels,
            inputs_available_at=value.inputs_available_at,
            version=self.version,
        )


@dataclass(frozen=True)
class FeatureVector:
    """Every factor's answer for one market at one decision time.

    Carries the refusals as well as the values, because "we had no NWS forecast"
    is the finding that tells you what to go and collect, and a vector that
    silently omitted its gaps could not report it.
    """

    market_key: str
    decision_time: datetime
    values: Mapping[str, FeatureValue]

    def get(self, name: str) -> FeatureValue | None:
        return self.values.get(name)

    def number(self, name: str) -> Decimal | None:
        """The value if the factor was available, else ``None``.

        Never a default. A caller that wants 0 for a missing factor has to write
        that themselves, in the open, where a reviewer can see the decision.
        """
        found = self.values.get(name)
        return found.value if found and found.available else None

    @property
    def available_names(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, v in self.values.items() if v.available))

    @property
    def unavailable(self) -> Mapping[str, Unavailable]:
        return {
            name: value.reason
            for name, value in sorted(self.values.items())
            if not value.available and value.reason is not None
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_key": self.market_key,
            "decision_time": self.decision_time.isoformat(),
            "values": {n: v.as_dict() for n, v in sorted(self.values.items())},
        }


class FactorRegistry:
    """The set of registered factors, and the only way to compute a vector."""

    def __init__(self) -> None:
        self._specs: dict[str, FactorSpec] = {}

    def register(self, spec: FactorSpec) -> FactorSpec:
        if spec.name in self._specs:
            # A silent overwrite would mean two different computations shipping
            # under one name, and a stored historical value could not be
            # attributed to either.
            raise ValueError(f"factor {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        return spec

    def factor(
        self,
        name: str,
        *,
        family: FactorFamily,
        version: str,
        description: str,
        requires: Sequence[str] = (),
        needs_book: bool = False,
        needs_outcome_model: bool = False,
    ) -> Callable[[FactorFn], FactorFn]:
        """Decorator form. Returns the undecorated function for direct testing."""

        def decorate(fn: FactorFn) -> FactorFn:
            self.register(
                FactorSpec(
                    name=name,
                    family=family,
                    version=version,
                    description=description,
                    compute=fn,
                    requires=tuple(requires),
                    needs_book=needs_book,
                    needs_outcome_model=needs_outcome_model,
                )
            )
            return fn

        return decorate

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def spec(self, name: str) -> FactorSpec:
        return self._specs[name]

    def specs(
        self, *, family: FactorFamily | None = None
    ) -> tuple[FactorSpec, ...]:
        chosen = [
            spec
            for spec in self._specs.values()
            if family is None or spec.family is family
        ]
        return tuple(sorted(chosen, key=lambda s: s.name))

    def compute(
        self,
        ctx: FactorContext,
        *,
        names: Iterable[str] | None = None,
    ) -> FeatureVector:
        """Compute every requested factor, catching refusals but not bugs.

        A factor raising is a defect in the factor, not a data condition, and it
        is allowed to propagate. Swallowing exceptions here would convert every
        such defect into a silent ``unavailable``, and a research run would then
        report thin coverage where it actually had broken code.
        """
        wanted = self._specs if names is None else {n: self._specs[n] for n in names}
        values = {name: spec(ctx) for name, spec in wanted.items()}
        return FeatureVector(
            market_key=f"{ctx.market.platform.value}:{ctx.market.platform_market_id}",
            decision_time=ctx.decision_time,
            values=values,
        )

    def coverage(
        self, vectors: Sequence[FeatureVector]
    ) -> dict[str, dict[str, int]]:
        """Per-factor availability across a research universe.

        The report that answers "is this strategy narrow because the edge is
        narrow, or because the data is missing?" - which are very different
        findings and look identical in an aggregate P&L number.
        """
        report: dict[str, dict[str, int]] = {}
        for name in self.names():
            counts = {"available": 0, "unavailable": 0}
            for vector in vectors:
                value = vector.get(name)
                if value is None:
                    continue
                counts["available" if value.available else "unavailable"] += 1
                if not value.available and value.reason is not None:
                    key = f"reason:{value.reason.value}"
                    counts[key] = counts.get(key, 0) + 1
            report[name] = counts
        return report


#: The process-wide registry. Factors register into it at import time.
REGISTRY = FactorRegistry()
