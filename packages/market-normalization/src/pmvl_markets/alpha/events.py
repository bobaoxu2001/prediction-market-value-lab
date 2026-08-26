"""The historical timeline, as events.

The existing walk-forward backtest reads one row per published recommendation
and holds every position to settlement. That is the right shape for grading what
was actually published, and the wrong shape for asking whether a *convergence*
strategy works, because a convergence strategy's entire claim is about what
happens between publication and settlement.

So this module models history as an ordered stream of things that happened:
quotes moved, books changed, trades printed, a source updated, a contract
settled. A strategy sees them in order, and can only see what had already
happened.

Two ordering rules, both load-bearing
-------------------------------------

**Events sort by ``available_at``, not by ``event_time``.** A weather observation
timestamped 14:00 that this system could not read until 14:40 belongs at 14:40 in
the timeline. Sorting by ``event_time`` would let a strategy act on it at 14:00,
which is the leak this whole layer exists to prevent - and it is a leak that
produces a *better* backtest, so nothing complains.

**Ties break deterministically.** Two events at the same instant are ordered by
kind then by sequence, so a replay of the same history produces the same
decisions. Without it, a run's results depend on dict ordering and "deterministic
backtest" becomes a claim nobody can check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Iterator, Sequence

from pmvl_shared.enums import Side
from pmvl_shared.schemas import OrderBook
from pmvl_shared.timeutil import ensure_utc

from .pit import SourceObservation


class EventKind(str, Enum):
    """What happened.

    The order of the members is also the tie-break order within one instant, and
    it is chosen so that *state* arrives before *decisions*: a strategy
    evaluating at time t sees the book update that happened at t, not the one it
    is about to cause. Reversing this would let a decision be made against a
    book that had not yet been published.
    """

    QUOTE = "quote"
    ORDER_BOOK = "order_book"
    TRADE = "trade"
    SOURCE_OBSERVATION = "source_observation"
    OFFICIAL_UPDATE = "official_update"
    SETTLEMENT = "settlement"
    STRATEGY_EVALUATION = "strategy_evaluation"
    ORDER_SUBMITTED = "order_submitted"
    FILL = "fill"
    PARTIAL_FILL = "partial_fill"
    CANCELLATION = "cancellation"
    EXIT = "exit"


#: Tie-break rank within one instant. Lower sorts first.
_KIND_RANK: dict[EventKind, int] = {
    kind: index for index, kind in enumerate(EventKind)
}


@dataclass(frozen=True)
class Event:
    """One thing that happened, at the instant this system could know it.

    ``available_at`` is the only time the timeline orders on. ``event_time`` is
    carried for analysis - "how long after the observation did we act?" is a real
    question - but it is never used for sequencing.
    """

    kind: EventKind
    available_at: datetime
    market_key: str
    payload: Any = None
    event_time: datetime | None = None
    #: Monotonic within a single build, for a stable tie-break.
    sequence: int = 0

    def sort_key(self) -> tuple[datetime, int, int, str]:
        return (
            ensure_utc(self.available_at),
            _KIND_RANK[self.kind],
            self.sequence,
            self.market_key,
        )


@dataclass(frozen=True)
class QuotePayload:
    """Top of book, as a strategy would have seen it."""

    best_yes_bid: Decimal | None
    best_yes_ask: Decimal | None
    best_no_bid: Decimal | None
    best_no_ask: Decimal | None

    def probability(self, side: Side) -> Decimal | None:
        return self.best_yes_ask if side is Side.YES else self.best_no_ask


@dataclass(frozen=True)
class TradePayload:
    """A public print. Volume in contracts."""

    price: Decimal
    volume: Decimal
    taker_side: str = ""


@dataclass(frozen=True)
class SettlementPayload:
    """Terminal state, from the venue's published value."""

    yes_payout: Decimal
    settled_at: datetime
    source: str = ""


@dataclass
class Timeline:
    """An ordered, immutable-once-built stream of events.

    Built once and iterated. Appending after iteration has started would make the
    replay depend on when a caller happened to add something, so :meth:`build`
    sorts and freezes, and :meth:`until` returns a slice rather than mutating.
    """

    events: tuple[Event, ...] = ()

    @classmethod
    def build(cls, events: Iterable[Event]) -> "Timeline":
        return cls(events=tuple(sorted(events, key=lambda e: e.sort_key())))

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        if not self.events:
            return None
        return (
            ensure_utc(self.events[0].available_at),
            ensure_utc(self.events[-1].available_at),
        )

    def until(self, cutoff: datetime) -> tuple[Event, ...]:
        """Every event knowable at ``cutoff``, inclusive.

        Inclusive of the cutoff itself: an event that became available at exactly
        the decision instant was available. Exclusive would silently discard the
        very update a source-lag strategy is reacting to.
        """
        limit = ensure_utc(cutoff)
        return tuple(e for e in self.events if ensure_utc(e.available_at) <= limit)

    def of_kind(self, kind: EventKind) -> tuple[Event, ...]:
        return tuple(e for e in self.events if e.kind is kind)

    def for_market(self, market_key: str) -> "Timeline":
        return Timeline(
            events=tuple(e for e in self.events if e.market_key == market_key)
        )

    def observations_until(self, cutoff: datetime) -> tuple[SourceObservation[Any], ...]:
        """Source observations as :mod:`.pit` records, for an ``AsOfView``.

        The bridge between the timeline and the point-in-time layer. Both know
        about ``available_at``; this is where a replay hands one to the other
        without either having to reach into the raw rows.
        """
        out: list[SourceObservation[Any]] = []
        for event in self.until(cutoff):
            if event.kind in (EventKind.SOURCE_OBSERVATION, EventKind.OFFICIAL_UPDATE):
                if isinstance(event.payload, SourceObservation):
                    out.append(event.payload)
        return tuple(out)

    def trades_between(
        self, market_key: str, start: datetime, end: datetime
    ) -> tuple[TradePayload, ...]:
        """Prints for one market inside a half-open interval.

        Half-open ``(start, end]`` so a fill simulation cannot count the same
        print for two consecutive intervals - which would double the volume a
        resting order is credited with, in the direction that flatters it.
        """
        lo, hi = ensure_utc(start), ensure_utc(end)
        out = []
        for event in self.events:
            if event.kind is not EventKind.TRADE or event.market_key != market_key:
                continue
            when = ensure_utc(event.available_at)
            if lo < when <= hi and isinstance(event.payload, TradePayload):
                out.append(event.payload)
        return tuple(out)


@dataclass
class TimelineBuilder:
    """Accumulates events with a monotonic sequence, then builds.

    The sequence counter is what makes ties deterministic. Handing callers a
    plain list and sorting it would make two events at the same instant order by
    whatever Python's sort found first, and a "deterministic backtest" that
    depends on insertion order is not one.
    """

    _events: list[Event] = field(default_factory=list)
    _sequence: int = 0

    def add(
        self,
        kind: EventKind,
        available_at: datetime,
        market_key: str,
        payload: Any = None,
        *,
        event_time: datetime | None = None,
    ) -> "TimelineBuilder":
        self._events.append(
            Event(
                kind=kind,
                available_at=ensure_utc(available_at),
                market_key=market_key,
                payload=payload,
                event_time=ensure_utc(event_time) if event_time else None,
                sequence=self._sequence,
            )
        )
        self._sequence += 1
        return self

    def add_book(
        self, book: OrderBook, market_key: str, *, available_at: datetime | None = None
    ) -> "TimelineBuilder":
        """Add an order book, defaulting availability to when it was observed.

        ``available_at`` is overridable because a captured book is not always
        knowable at capture time - a batch import makes a day of books available
        at once, and pretending otherwise would let a replay trade on books it
        could not have had.
        """
        return self.add(
            EventKind.ORDER_BOOK,
            available_at or book.observed_at,
            market_key,
            book,
            event_time=book.source_timestamp or book.observed_at,
        )

    def add_observation(
        self, obs: SourceObservation[Any], market_key: str
    ) -> "TimelineBuilder":
        if obs.available_at is None:
            # An observation with no availability cannot be placed in time, and
            # placing it anywhere would be a guess that decides whether a
            # strategy saw it.
            raise ValueError(
                f"{obs.kind!r} from {obs.source_id!r} has no available_at; it "
                "cannot be placed on a timeline"
            )
        return self.add(
            EventKind.SOURCE_OBSERVATION,
            obs.available_at,
            market_key,
            obs,
            event_time=obs.event_time,
        )

    def add_settlement(
        self, payload: SettlementPayload, market_key: str
    ) -> "TimelineBuilder":
        return self.add(
            EventKind.SETTLEMENT,
            payload.settled_at,
            market_key,
            payload,
            event_time=payload.settled_at,
        )

    def build(self) -> Timeline:
        return Timeline.build(self._events)


def decision_times(
    timeline: Timeline, *, kinds: Sequence[EventKind] = (EventKind.SOURCE_OBSERVATION,)
) -> tuple[datetime, ...]:
    """When a strategy should wake up.

    Event-driven rather than on a fixed clock. A source-lag strategy has nothing
    to say between source updates, and evaluating it every minute would produce
    thousands of identical decisions whose only effect is to make a sample look
    larger than it is.
    """
    wanted = set(kinds)
    return tuple(
        ensure_utc(e.available_at) for e in timeline.events if e.kind in wanted
    )
