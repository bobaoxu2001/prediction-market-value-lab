"""Point-in-time observations: the layer that makes leakage hard to write.

Every number a research decision is allowed to see must be an observation with a
time attached, and the time that matters is **not** the one that is usually
convenient to record.

Four distinct instants
----------------------

``event_time``
    When the thing being measured happened. A 14:00 UTC weather observation has
    ``event_time = 14:00`` no matter when anybody read it.

``observed_at``
    When the *source* published it. NWS issues a forecast; BLS releases a print.
    Still not the moment we could act on it.

``available_at``
    When **this system** could first have used it. This is the only one a
    backtest may compare against a decision time. It is later than
    ``observed_at`` whenever there is polling latency, an ingestion delay, or a
    provider that publishes on a schedule we do not sit on.

``ingested_at``
    When the row was written. Useful for debugging a pipeline; never a substitute
    for ``available_at``, because a backfill writes an old observation with a new
    ``ingested_at`` and would make history look knowable.

Collapsing any of these into "timestamp" is how look-ahead gets written by
accident, and the accident has no symptom: the backtest simply looks better.

The default that matters
------------------------
An observation with **no** ``available_at`` is not admissible. Absent provenance
is not evidence of freshness. This mirrors
:meth:`pmvl_markets.backtest.leakage.Observation.known_by`, which made the same
decision for the same reason, and this module is the generalisation of it:
``leakage.Observation`` carries one timestamp and is asserted against at the end,
whereas :class:`AsOfView` refuses to hand the value over in the first place.

Revisions
---------
Economic data is revised. A CPI print published on the 12th and revised on the
26th is *two* observations of one ``event_time``, with different
``available_at``. A decision made on the 20th may see only the first. Revision
handling therefore falls straight out of ``available_at`` filtering and needs no
special case - which is exactly why the field exists rather than a single
``timestamp`` plus a convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Generic, Iterable, Iterator, Sequence, TypeVar

from pmvl_shared.timeutil import ensure_utc

T = TypeVar("T")


class LookaheadError(AssertionError):
    """Something was read that was not available at the decision time.

    Raised rather than returned. A caller that asks an :class:`AsOfView` for
    information it may not have has a bug in its research code, and the useful
    behaviour is to stop, not to silently substitute a default that quietly
    becomes a result.
    """


@dataclass(frozen=True)
class SourceObservation(Generic[T]):
    """One externally-derived fact, with everything needed to place it in time.

    ``source_id`` is a stable identifier for *where this came from* - not a
    display name. It is what the independence machinery keys on: two forecasts
    that both restate the same NWS gridpoint are one source, and counting them as
    two would inflate every dispersion and agreement measure built on top.
    """

    source_id: str
    kind: str
    value: T
    #: When this system could first have used it. The only admissibility field.
    available_at: datetime | None
    #: When the source published it.
    observed_at: datetime | None = None
    #: When the underlying thing happened.
    event_time: datetime | None = None
    #: When the row was written locally. Never used for admissibility.
    ingested_at: datetime | None = None
    #: Free-form provenance: endpoint, parser version, payload hash.
    provenance: dict[str, Any] = field(default_factory=dict)

    def known_by(self, decision_time: datetime) -> bool:
        """Whether a decision at ``decision_time`` could have used this.

        No ``available_at`` means no. A row that cannot say when it became
        knowable is not evidence that it was knowable early.
        """
        available = ensure_utc(self.available_at)
        return available is not None and available <= ensure_utc(decision_time)

    def age_at(self, decision_time: datetime) -> timedelta | None:
        """How stale this was at the decision, measured from ``available_at``.

        ``None`` when the observation is inadmissible, so a caller cannot get a
        plausible-looking age out of something it may not read.
        """
        if not self.known_by(decision_time):
            return None
        return ensure_utc(decision_time) - ensure_utc(self.available_at)

    def source_age_at(self, decision_time: datetime) -> timedelta | None:
        """Staleness measured from ``observed_at`` instead.

        Different question, and the one a "how fresh is the underlying data"
        factor wants: a forecast issued an hour ago that we only ingested a
        minute ago is fresh to us and an hour old at the source.
        """
        if not self.known_by(decision_time):
            return None
        observed = ensure_utc(self.observed_at)
        if observed is None:
            return None
        return ensure_utc(decision_time) - observed

    def ingestion_lag(self) -> timedelta | None:
        """``available_at - observed_at``: how long the source led this system.

        The quantity a source-lag strategy is ultimately trying to trade, seen
        from our side rather than the market's.
        """
        available, observed = ensure_utc(self.available_at), ensure_utc(self.observed_at)
        if available is None or observed is None:
            return None
        return available - observed


@dataclass(frozen=True)
class AsOfView:
    """A read-only window onto observations, closed at a decision instant.

    Construct one per decision and read *everything* through it. The point is
    that the ordinary way to get a value - ``view.latest("nws_forecast")`` -
    already cannot return the future, so leakage requires deliberately reaching
    around the view rather than merely forgetting a filter.

    This is the difference between a guard that is checked and a guard that is
    structural, and it is the same choice the walk-forward backtest made when it
    decided to read only frozen snapshots.
    """

    decision_time: datetime
    observations: tuple[SourceObservation[Any], ...] = ()

    @classmethod
    def build(
        cls,
        decision_time: datetime,
        observations: Iterable[SourceObservation[Any]],
    ) -> "AsOfView":
        """Filter to what was knowable, once, at construction.

        Filtering here rather than on every read means an inadmissible
        observation is not merely ignored - it is not in the object at all, so
        no later code path can reach it by taking a different route.
        """
        cutoff = ensure_utc(decision_time)
        return cls(
            decision_time=cutoff,
            observations=tuple(o for o in observations if o.known_by(cutoff)),
        )

    def __iter__(self) -> Iterator[SourceObservation[Any]]:
        return iter(self.observations)

    def __len__(self) -> int:
        return len(self.observations)

    def of_kind(self, kind: str) -> tuple[SourceObservation[Any], ...]:
        """Every admissible observation of one kind, oldest first."""
        return tuple(
            sorted(
                (o for o in self.observations if o.kind == kind),
                key=lambda o: ensure_utc(o.available_at),
            )
        )

    def latest(self, kind: str) -> SourceObservation[Any] | None:
        """The newest admissible observation of a kind.

        Newest *admissible*, not newest. That distinction is the entire bug
        class: the tempting query returns the current row, which is exactly the
        one the decision did not have.
        """
        candidates = self.of_kind(kind)
        return candidates[-1] if candidates else None

    def latest_from(self, kind: str, source_id: str) -> SourceObservation[Any] | None:
        """The newest admissible observation of a kind from one source."""
        candidates = [o for o in self.of_kind(kind) if o.source_id == source_id]
        return candidates[-1] if candidates else None

    def previous(self, kind: str) -> SourceObservation[Any] | None:
        """The observation before the newest one.

        A source-lag factor needs the *change*, so it needs two points. Reaching
        for "the value before the update" through any other route is how a
        strategy ends up comparing today's forecast against today's forecast.
        """
        candidates = self.of_kind(kind)
        return candidates[-2] if len(candidates) >= 2 else None

    def sources(self, kind: str) -> tuple[str, ...]:
        """Distinct ``source_id`` values contributing to a kind, sorted.

        Distinct *sources*, not distinct observations. Ten pulls of one NWS
        gridpoint are one source, and a dispersion factor that counted them as
        ten independent forecasts would report agreement that does not exist.
        """
        return tuple(sorted({o.source_id for o in self.of_kind(kind)}))

    def require(self, kind: str) -> SourceObservation[Any]:
        """The newest admissible observation, or raise.

        For a caller that has already established the observation must exist and
        wants a loud failure rather than a ``None`` that turns into a default.
        """
        found = self.latest(kind)
        if found is None:
            raise LookaheadError(
                f"no admissible {kind!r} observation at "
                f"{self.decision_time.isoformat()}; a decision cannot be made "
                "from information that did not exist yet"
            )
        return found

    def assert_admissible(self, observation: SourceObservation[Any]) -> None:
        """Raise if an externally-supplied observation postdates the decision.

        Used at the boundary where a caller hands in something it obtained
        outside the view - the one route by which the future can still get in.
        """
        if not observation.known_by(self.decision_time):
            raise LookaheadError(
                f"{observation.kind!r} from {observation.source_id!r} became "
                f"available at {observation.available_at} which is after the "
                f"decision at {self.decision_time.isoformat()}"
            )


def observation(
    *,
    source_id: str,
    kind: str,
    value: Any,
    available_at: datetime | None,
    observed_at: datetime | None = None,
    event_time: datetime | None = None,
    ingested_at: datetime | None = None,
    **provenance: Any,
) -> SourceObservation[Any]:
    """Keyword-only constructor, so the four instants cannot be swapped.

    Positional construction of a four-timestamp record is a bug waiting for a
    refactor: the arguments are all the same type and a transposition type-checks
    perfectly while inverting what the backtest is allowed to see.
    """
    return SourceObservation(
        source_id=source_id,
        kind=kind,
        value=value,
        available_at=available_at,
        observed_at=observed_at if observed_at is not None else available_at,
        event_time=event_time,
        ingested_at=ingested_at,
        provenance=dict(provenance),
    )


def independent_source_count(
    observations: Sequence[SourceObservation[Any]],
    *,
    mirrors: dict[str, str] | None = None,
) -> int:
    """How many genuinely independent sources these observations represent.

    ``mirrors`` maps a ``source_id`` onto the upstream it restates. A vendor that
    republishes NWS is not a second opinion about the weather, and treating it as
    one inflates agreement exactly when agreement is being used as evidence.

    Defaults to no mirrors declared, which is the honest starting position: it
    counts distinct source IDs and leaves the judgement about what mirrors what
    to whoever can actually answer it.
    """
    mapping = mirrors or {}
    return len({mapping.get(o.source_id, o.source_id) for o in observations})


def decimal_value(observation: SourceObservation[Any]) -> Decimal | None:
    """Read an observation's value as a Decimal, or ``None`` if it is not one.

    Money and probabilities are ``Decimal`` everywhere in this repository
    because a binary contract lives on a cent lattice where float error changes
    verdicts. A float arriving from a provider is coerced through ``str`` rather
    than passed to ``Decimal`` directly, which would preserve the binary
    representation error instead of the number a human read.
    """
    value = observation.value
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ArithmeticError, ValueError):
            return None
    return None
