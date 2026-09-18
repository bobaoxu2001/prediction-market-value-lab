"""How often is the book wrong by three cents, for how long, and for how much?

The audit that closed the previous phase located the barrier precisely: the median
gross dislocation was *negative* thirty-four cents, the 99th percentile was plus one,
and an all-in taker fee at executable size is between 0.63c and 1.31c. Beating the fee
needs roughly 1.3c; surviving one adverse fill needs three to four. So the question
that decides whether any taker route exists is not "is there an edge" but "does a
three-cent edge ever appear, and if it does, does it stay long enough and go deep
enough to be worth an attempt".

This module answers exactly that, on one structure and no more: the **complete set
inside a single market**. Buying YES and NO in the same market pays exactly $1 at
settlement whatever happens, including if the market voids - there is no theorem to
get wrong, no strike ordering to infer, and, crucially, no second market, so the two
legs are from the same book in the same response and leg skew is not merely small but
structurally absent. If a synchronized three-cent route exists anywhere, it exists
here first; if it does not exist here, a wider structure with genuine skew risk is not
where it will be found.

On Kalshi this is visible as crossed bids. The venue publishes bids only and the asks
are derived, so ``yes_ask + no_ask < $1`` is the same statement as
``yes_bid + no_bid > $1``. Both legs still have to be sent as two separate orders, so
the sequential model applies here as it does everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import ONE, ZERO, quantize_usd
from pmvl_shared.schemas import BookLevel, OrderBook
from pmvl_shared.timeutil import iso

from ..pricing.fees import KALSHI_TAKER_RATE, taker_fee
from ..recording.schema import ANALYSIS_VERSION, EventSnapshot
from .execution_model import LegPlan, SequentialOutcome, evaluate_sequential

__all__ = [
    "THRESHOLDS_CENTS",
    "DislocationObservation",
    "DislocationEpisode",
    "HarvestReport",
    "observe_complete_set",
    "observe_pair",
    "harvest",
    "harvest_implication_pairs",
]

#: Declared before the forward run started. 1c and 2c are below the fee barrier and
#: are measured anyway - a threshold set that begins at the level the strategy needs
#: cannot show that the level is never reached.
THRESHOLDS_CENTS = (1, 2, 3, 4, 5)

CENT = Decimal("0.01")


@dataclass(frozen=True)
class DislocationObservation:
    """One market, one snapshot: what the complete set cost and how much of it there was."""

    market: str
    event_ticker: str
    observed_at: datetime
    snapshot_sequence: int
    skew_bound_ms: Decimal
    yes_ask: Decimal
    no_ask: Decimal
    #: Positive when the pair costs less than the dollar it settles for.
    gross_edge_per_set: Decimal
    top_of_book_sets: Decimal
    #: threshold in cents -> sets whose *marginal* pair cost keeps that much edge
    capacity_sets: dict[int, Decimal]
    #: threshold in cents -> net dollars after both legs' taker fees at that size
    net_after_fees: dict[int, Decimal]

    def meets(self, threshold_cents: int) -> bool:
        return self.capacity_sets.get(threshold_cents, ZERO) > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "event_ticker": self.event_ticker,
            "observed_at": iso(self.observed_at),
            "snapshot_sequence": self.snapshot_sequence,
            "skew_bound_ms": str(self.skew_bound_ms),
            "yes_ask": str(self.yes_ask),
            "no_ask": str(self.no_ask),
            "gross_edge_per_set": str(self.gross_edge_per_set),
            "top_of_book_sets": str(self.top_of_book_sets),
            "capacity_sets": {str(k): str(v) for k, v in sorted(self.capacity_sets.items())},
            "net_after_fees_usd": {
                str(k): str(v) for k, v in sorted(self.net_after_fees.items())
            },
        }


def _lockstep_capacity(
    yes_levels: Sequence[BookLevel],
    no_levels: Sequence[BookLevel],
    *,
    max_pair_cost: Decimal,
    ceiling: Decimal,
) -> tuple[Decimal, Decimal]:
    """(sets, gross notional) taking only pairs whose marginal cost clears the bar.

    Marginal, not average. Taking every pair whose *running average* still clears the
    bar keeps buying after the next pair has stopped paying, which is how a structure
    with a real six-cent top of book turns into a blended loss.
    """
    if not yes_levels or not no_levels:
        return ZERO, ZERO

    i = j = 0
    left_yes = yes_levels[0].size
    left_no = no_levels[0].size
    sets = ZERO
    notional = ZERO

    while i < len(yes_levels) and j < len(no_levels) and sets < ceiling:
        marginal = yes_levels[i].price + no_levels[j].price
        if marginal > max_pair_cost:
            break
        take = min(left_yes, left_no, ceiling - sets)
        if take <= ZERO:
            break
        sets += take
        notional += take * marginal
        left_yes -= take
        left_no -= take
        if left_yes <= ZERO:
            i += 1
            if i < len(yes_levels):
                left_yes = yes_levels[i].size
        if left_no <= ZERO:
            j += 1
            if j < len(no_levels):
                left_no = no_levels[j].size
    return sets, notional


def observe_pair(
    market: str,
    event_ticker: str,
    first: Sequence[BookLevel],
    second: Sequence[BookLevel],
    *,
    observed_at: datetime,
    snapshot_sequence: int,
    skew_bound_ms: Decimal,
    ceiling: Decimal = Decimal("10000"),
    fee_rate: Decimal = KALSHI_TAKER_RATE,
) -> DislocationObservation | None:
    """Measure any two ask ladders whose pair settles for exactly $1 per set.

    Both the complete set inside one market and the implication pair across two rungs
    reduce to this: two things to buy, one dollar per set at settlement whatever
    happens. What differs is whether a dislocation is even *possible*, and that is a
    property of the venue rather than of the arithmetic.
    """
    if not first or not second:
        return None
    first_ask = first[0].price
    second_ask = second[0].price

    gross = ONE - (first_ask + second_ask)
    top_sets = min(first[0].size, second[0].size)

    capacity: dict[int, Decimal] = {}
    net: dict[int, Decimal] = {}
    for cents in THRESHOLDS_CENTS:
        bar = ONE - CENT * cents
        sets, _ = _lockstep_capacity(
            first, second, max_pair_cost=bar, ceiling=ceiling
        )
        capacity[cents] = sets
        if sets <= 0:
            net[cents] = ZERO
            continue
        first_cost = _walk_cost(first, sets)
        second_cost = _walk_cost(second, sets)
        fees = taker_fee(
            Platform.KALSHI, sets, _vwap(first_cost, sets), rate=fee_rate
        ) + taker_fee(Platform.KALSHI, sets, _vwap(second_cost, sets), rate=fee_rate)
        net[cents] = quantize_usd(sets * ONE - first_cost - second_cost - fees)

    return DislocationObservation(
        market=market,
        event_ticker=event_ticker,
        observed_at=observed_at,
        snapshot_sequence=snapshot_sequence,
        skew_bound_ms=skew_bound_ms,
        yes_ask=first_ask,
        no_ask=second_ask,
        gross_edge_per_set=gross,
        top_of_book_sets=top_sets,
        capacity_sets=capacity,
        net_after_fees=net,
    )


def observe_complete_set(
    market: str,
    event_ticker: str,
    book: OrderBook,
    *,
    observed_at: datetime,
    snapshot_sequence: int,
    skew_bound_ms: Decimal,
    ceiling: Decimal = Decimal("10000"),
    fee_rate: Decimal = KALSHI_TAKER_RATE,
) -> DislocationObservation | None:
    """Measure one market's complete set. ``None`` only when a side is unquoted.

    A negative edge is returned, not discarded. The distribution of the *losers* is
    what tells you how far the market normally sits from the arbitrage line, and a
    record of only the positive observations cannot answer how unusual a positive one
    is.

    Note what this structure can and cannot show. On Kalshi both legs rest in the same
    matching engine, ``yes_ask = $1 - best NO bid`` and ``no_ask = $1 - best YES bid``,
    so a pair costing under a dollar is the same statement as ``yes_bid + no_bid > $1``
    - a crossed book, which the engine matches away the instant it appears. A positive
    reading here would therefore be surprising, and its absence is close to a
    tautology. It is measured anyway because it is the only structure with *no* leg
    skew at all, which makes it the control: if a dislocation cannot be found where
    simultaneity is free, a wider structure is not where the money is.
    """
    return observe_pair(
        market,
        event_ticker,
        book.asks(Side.YES),
        book.asks(Side.NO),
        observed_at=observed_at,
        snapshot_sequence=snapshot_sequence,
        skew_bound_ms=skew_bound_ms,
        ceiling=ceiling,
        fee_rate=fee_rate,
    )


def _walk_cost(levels: Sequence[BookLevel], size: Decimal) -> Decimal:
    remaining = size
    cost = ZERO
    for level in levels:
        if remaining <= 0:
            break
        take = min(level.size, remaining)
        cost += take * level.price
        remaining -= take
    return cost


def _vwap(cost: Decimal, size: Decimal) -> Decimal:
    return cost / size if size else ZERO


# ---------------------------------------------------------------- episodes
@dataclass
class DislocationEpisode:
    """A stretch of consecutive snapshots where one market cleared a threshold.

    Duration is measured between the *first and last snapshot that cleared it*, so a
    dislocation seen in exactly one snapshot has a duration of zero seconds and says
    so. Padding it out to the polling interval would invent persistence that was
    never observed.
    """

    market: str
    event_ticker: str
    threshold_cents: int
    started_at: datetime
    ended_at: datetime
    snapshots: int = 0
    peak_gross_edge: Decimal = ZERO
    peak_capacity_sets: Decimal = ZERO
    peak_net_after_fees: Decimal = ZERO
    max_skew_bound_ms: Decimal = ZERO
    sequential_net_floor: Decimal | None = None
    failure_rescue_floor: Decimal | None = None

    @property
    def duration_seconds(self) -> Decimal:
        return Decimal(str((self.ended_at - self.started_at).total_seconds()))

    @property
    def survives_sequencing(self) -> bool:
        return (
            self.sequential_net_floor is not None and self.sequential_net_floor > ZERO
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "event_ticker": self.event_ticker,
            "threshold_cents": self.threshold_cents,
            "started_at": iso(self.started_at),
            "ended_at": iso(self.ended_at),
            "duration_seconds": str(self.duration_seconds),
            "snapshots": self.snapshots,
            "peak_gross_edge_per_set": str(self.peak_gross_edge),
            "peak_capacity_sets": str(self.peak_capacity_sets),
            "peak_net_after_fees_usd": str(self.peak_net_after_fees),
            "max_skew_bound_ms": str(self.max_skew_bound_ms),
            "sequential_net_floor_usd": (
                None if self.sequential_net_floor is None else str(self.sequential_net_floor)
            ),
            "failure_rescue_floor_usd": (
                None if self.failure_rescue_floor is None else str(self.failure_rescue_floor)
            ),
            "survives_sequencing": self.survives_sequencing,
        }


@dataclass
class HarvestReport:
    strategy_version: str
    snapshots_read: int = 0
    degraded_snapshots_skipped: int = 0
    market_observations: int = 0
    markets: int = 0
    event_days: int = 0
    observations: list[DislocationObservation] = field(default_factory=list)
    episodes: list[DislocationEpisode] = field(default_factory=list)
    #: proof status -> how many candidate pairs it turned away. Kept because "we
    #: looked and the pair was not provable" and "we never looked" are different
    #: findings and only one of them is about the market.
    pairs_refused: dict[str, int] = field(default_factory=dict)
    #: Provable structures that could not be priced because a leg had no quote on the
    #: side it would have to be bought. Excluded from the denominator of "how often
    #: does a dislocation clear", because the question was never asked of them - but
    #: counted, because a structure that is usually unquoted is usually untradeable
    #: and that is a finding about the market, not a gap in the data.
    unpriceable_observations: int = 0

    def episodes_at(self, cents: int) -> list[DislocationEpisode]:
        return [e for e in self.episodes if e.threshold_cents == cents]

    def edge_percentiles(self) -> dict[str, str]:
        values = sorted(o.gross_edge_per_set for o in self.observations)
        if not values:
            return {}
        def at(q: float) -> str:
            idx = min(len(values) - 1, int(q * (len(values) - 1) + 0.5))
            return str(values[idx])
        return {
            "p01": at(0.01), "p50": at(0.50), "p95": at(0.95),
            "p99": at(0.99), "max": str(values[-1]),
        }

    def as_dict(self) -> dict[str, Any]:
        per_threshold: dict[str, Any] = {}
        for cents in THRESHOLDS_CENTS:
            eps = self.episodes_at(cents)
            hits = sum(1 for o in self.observations if o.meets(cents))
            survivors = [e for e in eps if e.survives_sequencing]
            durations = sorted(e.duration_seconds for e in eps)
            #: Cleared the bar one cent lower but not this one. Counted because "it
            #: never came close" and "it was one tick away, repeatedly" are different
            #: findings about the same zero, and only the second one says the barrier
            #: is where the fees are rather than somewhere far above them.
            near = sum(
                1
                for o in self.observations
                if not o.meets(cents) and o.gross_edge_per_set >= CENT * (cents - 1)
            )
            per_threshold[str(cents)] = {
                "observations_clearing": hits,
                "near_misses_within_one_cent": near,
                "observation_rate": (
                    str(round(Decimal(hits) / Decimal(self.market_observations), 6))
                    if self.market_observations
                    else "0"
                ),
                "episodes": len(eps),
                "episodes_surviving_sequential_execution": len(survivors),
                "median_duration_seconds": (
                    str(durations[len(durations) // 2]) if durations else None
                ),
                "max_duration_seconds": str(durations[-1]) if durations else None,
                "max_capacity_sets": str(
                    max((e.peak_capacity_sets for e in eps), default=ZERO)
                ),
                "max_net_after_fees_usd": str(
                    max((e.peak_net_after_fees for e in eps), default=ZERO)
                ),
            }
        return {
            "recording_version": self.strategy_version,
            "analysis_version": ANALYSIS_VERSION,
            "structure": "complete set within a single market (YES ask + NO ask vs $1)",
            "snapshots_read": self.snapshots_read,
            "degraded_snapshots_skipped": self.degraded_snapshots_skipped,
            "market_observations": self.market_observations,
            "unpriceable_observations": self.unpriceable_observations,
            "markets": self.markets,
            "independent_event_days": self.event_days,
            "gross_edge_per_set_percentiles": self.edge_percentiles(),
            "candidate_pairs_refused": dict(sorted(self.pairs_refused.items())),
            "thresholds": per_threshold,
        }


def harvest(
    snapshots: Iterable[EventSnapshot],
    *,
    strategy_version: str,
    ceiling: Decimal = Decimal("10000"),
    fee_rate: Decimal = KALSHI_TAKER_RATE,
) -> HarvestReport:
    """Measure every market in every synchronized snapshot, and stitch the episodes."""
    report = HarvestReport(strategy_version=strategy_version)
    ordered = sorted(snapshots, key=lambda s: s.response_received_at)

    open_episodes: dict[tuple[str, int], DislocationEpisode] = {}
    event_days: set[tuple[str, str]] = set()
    markets: set[str] = set()

    for snapshot in ordered:
        report.snapshots_read += 1
        if not snapshot.is_synchronized:
            report.degraded_snapshots_skipped += 1
            continue
        observed_at = snapshot.response_received_at
        event_days.add((snapshot.event_ticker, observed_at.date().isoformat()))

        seen_this_snapshot: set[tuple[str, int]] = set()
        for market, book in sorted(snapshot.books.items()):
            observation = observe_complete_set(
                market,
                snapshot.event_ticker,
                book,
                observed_at=observed_at,
                snapshot_sequence=snapshot.sequence,
                skew_bound_ms=snapshot.skew_bound_ms,
                ceiling=ceiling,
                fee_rate=fee_rate,
            )
            if observation is None:
                report.unpriceable_observations += 1
                continue
            report.observations.append(observation)
            report.market_observations += 1
            markets.add(market)

            for cents in THRESHOLDS_CENTS:
                if not observation.meets(cents):
                    continue
                key = (market, cents)
                seen_this_snapshot.add(key)
                episode = open_episodes.get(key)
                if episode is None:
                    episode = DislocationEpisode(
                        market=market,
                        event_ticker=snapshot.event_ticker,
                        threshold_cents=cents,
                        started_at=observed_at,
                        ended_at=observed_at,
                    )
                    open_episodes[key] = episode
                episode.ended_at = observed_at
                episode.snapshots += 1
                episode.peak_gross_edge = max(
                    episode.peak_gross_edge, observation.gross_edge_per_set
                )
                if observation.capacity_sets[cents] > episode.peak_capacity_sets:
                    episode.peak_capacity_sets = observation.capacity_sets[cents]
                    episode.peak_net_after_fees = observation.net_after_fees[cents]
                    outcome = _sequential_for(book, observation.capacity_sets[cents], fee_rate)
                    episode.sequential_net_floor = outcome.sequential_net_floor
                    episode.failure_rescue_floor = outcome.failure_rescue_floor
                episode.max_skew_bound_ms = max(
                    episode.max_skew_bound_ms, snapshot.skew_bound_ms
                )

        #: An episode ends the moment a snapshot of that market fails to clear.
        for key in list(open_episodes):
            market, _ = key
            if key in seen_this_snapshot:
                continue
            if market in snapshot.books:
                report.episodes.append(open_episodes.pop(key))

    report.episodes.extend(open_episodes.values())
    report.markets = len(markets)
    report.event_days = len(event_days)
    return report


def harvest_implication_pairs(
    snapshots: Iterable[EventSnapshot],
    markets: Mapping[str, Any],
    *,
    strategy_version: str,
    ceiling: Decimal = Decimal("10000"),
    fee_rate: Decimal = KALSHI_TAKER_RATE,
) -> HarvestReport:
    """The same measurement across two rungs of one ladder, where a gap is possible.

    Two rungs are two markets and therefore two independent order books. Nothing in
    the exchange enforces that a "above $4,200" contract stays cheaper than an "above
    $4,100" one, so unlike the intra-market control this structure *can* dislocate,
    and a reading of zero here is a finding about the market rather than about the
    matching engine.

    Only pairs whose implication is proven from the recorded rules are measured. The
    proof uses the market records captured when the recording started, never a fresh
    fetch, so a rung that has since been relisted cannot change what a past
    observation is taken to have meant.
    """
    from .implication import ProofStatus, StrikeContract, prove_implication

    report = HarvestReport(strategy_version=strategy_version)
    ordered = sorted(snapshots, key=lambda s: s.response_received_at)

    open_episodes: dict[tuple[str, int], DislocationEpisode] = {}
    event_days: set[tuple[str, str]] = set()
    seen_pairs: set[str] = set()

    for snapshot in ordered:
        report.snapshots_read += 1
        if not snapshot.is_synchronized:
            report.degraded_snapshots_skipped += 1
            continue
        observed_at = snapshot.response_received_at
        event_days.add((snapshot.event_ticker, observed_at.date().isoformat()))

        rungs: list[StrikeContract] = []
        for ticker, book in sorted(snapshot.books.items()):
            market = markets.get(ticker)
            if market is None:
                continue
            rung = _strike_of(market)
            if rung is None:
                continue
            comparator, strike = rung
            rungs.append(
                StrikeContract(
                    market=market, book=book, comparator=comparator, threshold=strike
                )
            )
        rungs.sort(key=lambda c: c.threshold)

        seen_this_snapshot: set[tuple[str, int]] = set()
        for i, weaker in enumerate(rungs):
            for stronger in rungs[i + 1 :]:
                #: The full proof, not the threshold predicate. The predicate answers
                #: "does [X > a] imply [X > b]", which is true of any two half-lines
                #: in the right order - including two that measure different things.
                #: Only the proof checks that they measure the same thing.
                antecedent, consequent = stronger, weaker
                proof = prove_implication(antecedent, consequent)
                if proof.status is not ProofStatus.PROVEN:
                    report.pairs_refused[proof.status.value] = (
                        report.pairs_refused.get(proof.status.value, 0) + 1
                    )
                    flipped = prove_implication(weaker, stronger)
                    if flipped.status is not ProofStatus.PROVEN:
                        report.pairs_refused[flipped.status.value] = (
                            report.pairs_refused.get(flipped.status.value, 0) + 1
                        )
                        continue
                    antecedent, consequent = weaker, stronger

                #: A dollar per set: YES on the weaker claim plus NO on the stronger
                #: one covers every state, because the stronger claim being true
                #: forces the weaker one to be true as well.
                pair_key = f"{consequent.key}|{antecedent.key}"
                observation = observe_pair(
                    pair_key,
                    snapshot.event_ticker,
                    consequent.book.asks(Side.YES),
                    antecedent.book.asks(Side.NO),
                    observed_at=observed_at,
                    snapshot_sequence=snapshot.sequence,
                    skew_bound_ms=snapshot.skew_bound_ms,
                    ceiling=ceiling,
                    fee_rate=fee_rate,
                )
                if observation is None:
                    report.unpriceable_observations += 1
                    continue
                report.observations.append(observation)
                report.market_observations += 1
                seen_pairs.add(pair_key)

                for cents in THRESHOLDS_CENTS:
                    if not observation.meets(cents):
                        continue
                    key = (pair_key, cents)
                    seen_this_snapshot.add(key)
                    episode = open_episodes.get(key)
                    if episode is None:
                        episode = DislocationEpisode(
                            market=pair_key,
                            event_ticker=snapshot.event_ticker,
                            threshold_cents=cents,
                            started_at=observed_at,
                            ended_at=observed_at,
                        )
                        open_episodes[key] = episode
                    episode.ended_at = observed_at
                    episode.snapshots += 1
                    episode.peak_gross_edge = max(
                        episode.peak_gross_edge, observation.gross_edge_per_set
                    )
                    if observation.capacity_sets[cents] > episode.peak_capacity_sets:
                        episode.peak_capacity_sets = observation.capacity_sets[cents]
                        episode.peak_net_after_fees = observation.net_after_fees[cents]
                        outcome = _sequential_for_pair(
                            consequent.book, antecedent.book,
                            observation.capacity_sets[cents], fee_rate,
                        )
                        episode.sequential_net_floor = outcome.sequential_net_floor
                        episode.failure_rescue_floor = outcome.failure_rescue_floor
                    episode.max_skew_bound_ms = max(
                        episode.max_skew_bound_ms, snapshot.skew_bound_ms
                    )

        #: Close only the episodes belonging to *this* event. Snapshots arrive one
        #: event at a time, so closing every open episode on every snapshot would end
        #: an episode in one event because a different event was captured next, and
        #: a two-minute dislocation would be filed as a dozen instantaneous ones.
        for key in list(open_episodes):
            if key in seen_this_snapshot:
                continue
            if open_episodes[key].event_ticker == snapshot.event_ticker:
                report.episodes.append(open_episodes.pop(key))

    report.episodes.extend(open_episodes.values())
    report.markets = len(seen_pairs)
    report.event_days = len(event_days)
    return report


def _strike_of(market: Any) -> tuple[str, Decimal] | None:
    """Comparator and strike, or ``None`` when the market is not a threshold rung.

    Kalshi puts an upward comparator's strike in ``floor_strike`` and a downward
    one's in ``cap_strike``. Reading the wrong field yields ``None``, which would
    then compare equal right across a ladder and assert implications between rungs
    that have no relation at all.
    """
    comparator = (getattr(market, "strike_type", None) or "").lower()
    if comparator in {"greater", "greater_or_equal"}:
        strike = getattr(market, "floor_strike", None)
    elif comparator in {"less", "less_or_equal"}:
        strike = getattr(market, "cap_strike", None)
    else:
        return None
    if strike is None:
        return None
    return comparator, strike


def _sequential_for_pair(
    consequent: OrderBook, antecedent: OrderBook, sets: Decimal, fee_rate: Decimal
) -> SequentialOutcome:
    legs = [
        LegPlan(
            contract_key=f"{consequent.platform_market_id}:yes",
            venue=Platform.KALSHI,
            book=consequent,
            side=Side.YES,
            quantity=sets,
            fee_rate=fee_rate,
        ),
        LegPlan(
            contract_key=f"{antecedent.platform_market_id}:no",
            venue=Platform.KALSHI,
            book=antecedent,
            side=Side.NO,
            quantity=sets,
            fee_rate=fee_rate,
        ),
    ]
    return evaluate_sequential(legs, sets * ONE)


def _sequential_for(
    book: OrderBook, sets: Decimal, fee_rate: Decimal
) -> SequentialOutcome:
    """Price the same pair again with one leg filling before the other."""
    legs = [
        LegPlan(
            contract_key=f"{book.platform_market_id}:{side.value}",
            venue=Platform.KALSHI,
            book=book,
            side=side,
            quantity=sets,
            fee_rate=fee_rate,
        )
        for side in (Side.YES, Side.NO)
    ]
    return evaluate_sequential(legs, sets * ONE)
