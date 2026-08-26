"""The event-driven strategy simulator.

What this is, and what it is not
--------------------------------
It is **not** a replacement for the walk-forward backtest. That engine grades
what was actually published, reading only frozen recommendation snapshots, and
its look-ahead immunity comes from the fact that there is no code path to a live
quote. That property is worth keeping and this module does not touch it.

This is the thing that engine cannot be: a simulator that replays a timeline,
lets a strategy decide at arbitrary instants, models resting orders that may not
fill, and lets a position be *closed before the event resolves*. Those are
requirements of a convergence strategy and impossible to express as
"one snapshot in, one settlement out".

The four rules it enforces
--------------------------

1. **A decision may only read ``available_at <= decision_time``.** The strategy
   is handed an :class:`~.pit.AsOfView` built at that instant; there is no other
   accessor.
2. **No maker fill from a quote touch.** Fills come from
   :class:`~.execution.ConservativeFillModel`, which requires traded volume
   through the level and takes a queue share of it.
3. **Exits sell into the bid.** A convergence exit marked at the ask has not
   modelled an exit; it has recorded a wish.
4. **Every trade carries its fill confidence**, and a run's confidence is the
   *worst* of its trades - never an average, which would let one
   queue-unmodelled fill disappear into an aggregate that reads as executable.

Determinism
-----------
Same timeline, same strategy, same parameters, same result - byte for byte in
the returned records. Events are totally ordered, nothing reads the clock, and
no set or dict iteration decides anything. :func:`run` is safe to call twice and
compare.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from pmvl_shared.enums import Side
from pmvl_shared.money import ONE, ZERO, quantize_prob, quantize_usd, safe_div
from pmvl_shared.schemas import NormalizedMarket, OrderBook
from pmvl_shared.timeutil import ensure_utc

from .events import EventKind, SettlementPayload, Timeline, TradePayload
from .execution import (
    ConservativeFillModel,
    FillConfidence,
    LevelTrade,
    MakerQuote,
    exit_proceeds_per_contract,
    worst_fill_confidence,
)
from .models import AlphaModel, Liquidity, OutcomeEstimate
from .pit import AsOfView
from .registry import REGISTRY, FactorContext, FactorRegistry, FeatureVector
from .sizing import ExposureLedger
from .strategy import ExitTrigger, OpenPosition, Signal, Strategy


@dataclass
class SimulatedTrade:
    """One completed round trip, with everything needed to attribute it.

    ``entry_signal`` is retained whole. An attribution report that only had the
    P&L could tell you a strategy lost money; with the signal it can tell you
    which bucket of which factor lost it, which is the only version of that
    finding anybody can act on.
    """

    market_key: str
    side: Side
    liquidity: Liquidity
    contracts: Decimal
    entry_price: Decimal
    entry_at: datetime
    exit_price: Decimal | None
    exit_at: datetime | None
    exit_trigger: ExitTrigger | None
    fill_confidence: FillConfidence
    #: Fees actually charged, entry and exit, per contract.
    entry_fee: Decimal
    exit_fee: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    entry_signal: Signal
    partial: bool = False
    requested_contracts: Decimal = ZERO
    category: str = ""
    notes: tuple[str, ...] = ()

    @property
    def holding_time(self) -> timedelta | None:
        return None if self.exit_at is None else self.exit_at - self.entry_at

    @property
    def deployed(self) -> Decimal:
        return quantize_usd(self.entry_price * self.contracts)

    @property
    def realised_edge_per_contract(self) -> Decimal:
        return safe_div(self.net_pnl, self.contracts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_key": self.market_key,
            "side": self.side.value,
            "liquidity": self.liquidity.value,
            "contracts": str(self.contracts),
            "requested_contracts": str(self.requested_contracts),
            "partial": self.partial,
            "entry_price": str(self.entry_price),
            "entry_at": self.entry_at.isoformat(),
            "exit_price": None if self.exit_price is None else str(self.exit_price),
            "exit_at": None if self.exit_at is None else self.exit_at.isoformat(),
            "exit_trigger": self.exit_trigger.value if self.exit_trigger else None,
            "fill_confidence": self.fill_confidence.value,
            "entry_fee": str(self.entry_fee),
            "exit_fee": str(self.exit_fee),
            "gross_pnl": str(self.gross_pnl),
            "net_pnl": str(self.net_pnl),
            "category": self.category,
            "holding_seconds": (
                None if self.holding_time is None else int(self.holding_time.total_seconds())
            ),
            "entry_signal": self.entry_signal.as_dict(),
            "notes": list(self.notes),
        }


@dataclass
class SimulationResult:
    """Everything a run produced, including what it refused to do.

    ``declined`` and ``unfilled`` are first-class output. A run that placed three
    trades out of four hundred opportunities is a very different finding from one
    that found four hundred and took three, and a result object that only carried
    the trades could not tell them apart.
    """

    strategy: str
    strategy_version: str
    trades: list[SimulatedTrade] = field(default_factory=list)
    #: Signals the strategy declined, by reason.
    declined: dict[str, int] = field(default_factory=dict)
    #: Orders that were placed and never filled, by fill-model reason.
    unfilled: dict[str, int] = field(default_factory=dict)
    #: Sizing refusals, by reason.
    size_declined: dict[str, int] = field(default_factory=dict)
    decisions_evaluated: int = 0
    markets_seen: int = 0
    period: tuple[datetime, datetime] | None = None
    feature_coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def worst_fill_confidence(self) -> FillConfidence:
        return worst_fill_confidence([t.fill_confidence for t in self.trades])

    @property
    def fill_rate(self) -> Decimal:
        placed = len(self.trades) + sum(self.unfilled.values())
        if placed == 0:
            return ZERO
        return quantize_prob(safe_div(Decimal(len(self.trades)), Decimal(placed)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "trades": [t.as_dict() for t in self.trades],
            "declined": dict(sorted(self.declined.items())),
            "unfilled": dict(sorted(self.unfilled.items())),
            "size_declined": dict(sorted(self.size_declined.items())),
            "decisions_evaluated": self.decisions_evaluated,
            "markets_seen": self.markets_seen,
            "fill_rate": str(self.fill_rate),
            "worst_fill_confidence": self.worst_fill_confidence.value,
            "period": (
                [self.period[0].isoformat(), self.period[1].isoformat()]
                if self.period
                else None
            ),
            "feature_coverage": self.feature_coverage,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class MarketHistory:
    """One market's replayable history.

    ``outcome_at`` is a callable rather than a dict so a caller can plug in
    whatever produced the estimate - a frozen table for a replay, a recomputation
    under ``as_of`` for a research run - without this module knowing which. What
    it must never be is a function that reaches a live provider; the docstring
    says so and :func:`run` cannot check it, which is exactly why the frozen
    table is the recommended shape.
    """

    market: NormalizedMarket
    timeline: Timeline
    books: Mapping[datetime, OrderBook]
    outcome_at: Callable[[datetime], OutcomeEstimate | None]
    #: Optional extras handed to every FactorContext for this market.
    factor_extra: Mapping[str, Any] = field(default_factory=dict)
    threshold_value: Decimal | None = None
    threshold_comparator: str = ""

    @property
    def key(self) -> str:
        return f"{self.market.platform.value}:{self.market.platform_market_id}"

    def book_at(self, when: datetime) -> OrderBook | None:
        """The newest book available at ``when``.

        Newest *available*, which is the whole point. The tempting query returns
        the current book, which is precisely the one the decision did not have.
        """
        cutoff = ensure_utc(when)
        candidates = [
            book
            for observed, book in self.books.items()
            if ensure_utc(observed) <= cutoff
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda b: ensure_utc(b.observed_at))

    def settlement(self) -> SettlementPayload | None:
        events = self.timeline.of_kind(EventKind.SETTLEMENT)
        if not events:
            return None
        payload = events[-1].payload
        return payload if isinstance(payload, SettlementPayload) else None


def run(
    histories: Sequence[MarketHistory],
    strategy: Strategy,
    alpha_model: AlphaModel,
    *,
    registry: FactorRegistry = REGISTRY,
    fill_model: ConservativeFillModel | None = None,
    ledger: ExposureLedger | None = None,
    decision_kinds: Sequence[EventKind] = (EventKind.SOURCE_OBSERVATION,),
    review_interval: timedelta = timedelta(minutes=30),
) -> SimulationResult:
    """Replay every market's history and record what the strategy did.

    The loop is per market rather than globally interleaved. That is a real
    simplification and it is stated rather than hidden: it means the exposure
    ledger sees markets in a fixed order rather than in true chronological order
    across the universe, so a daily-deployment cap binds slightly differently
    than it would live. It does **not** affect any per-trade number, because a
    market's own events are still strictly ordered and no market can see
    another's future. Interleaving globally is the natural next step and is noted
    in the result.
    """
    fill_model = fill_model or ConservativeFillModel()
    ledger = ledger if ledger is not None else ExposureLedger()
    result = SimulationResult(
        strategy=strategy.name, strategy_version=strategy.version
    )
    result.notes.append(
        "markets are replayed independently; the exposure ledger therefore sees "
        "them in list order rather than global chronological order"
    )
    vectors: list[FeatureVector] = []
    spans: list[tuple[datetime, datetime]] = []

    for history in histories:
        result.markets_seen += 1
        span = history.timeline.span
        if span is not None:
            spans.append(span)
        _replay_market(
            history,
            strategy,
            alpha_model,
            registry,
            fill_model,
            ledger,
            decision_kinds,
            review_interval,
            result,
            vectors,
        )

    if spans:
        result.period = (min(s[0] for s in spans), max(s[1] for s in spans))
    result.feature_coverage = registry.coverage(vectors)
    return result


def _replay_market(
    history: MarketHistory,
    strategy: Strategy,
    alpha_model: AlphaModel,
    registry: FactorRegistry,
    fill_model: ConservativeFillModel,
    ledger: ExposureLedger,
    decision_kinds: Sequence[EventKind],
    review_interval: timedelta,
    result: SimulationResult,
    vectors: list[FeatureVector],
) -> None:
    position: OpenPosition | None = None
    pending: tuple[MakerQuote, datetime, Signal] | None = None
    last_review: datetime | None = None

    for event in history.timeline:
        when = ensure_utc(event.available_at)

        # A resting order is checked against the flow that printed since it was
        # posted, on every subsequent event. Checking only at the next decision
        # would let an order sit through a burst of trades and then be judged
        # against a quiet interval.
        if pending is not None and event.kind in (EventKind.TRADE, EventKind.ORDER_BOOK):
            quote, posted_at, signal = pending
            prints = history.timeline.trades_between(history.key, posted_at, when)
            fill = fill_model.simulate(
                quote,
                [LevelTrade(price=p.price, volume=p.volume) for p in prints],
            )
            if fill.filled and fill.fill_price is not None:
                position = _open_position(
                    history, signal, quote, fill.filled_size, fill.fill_price,
                    when, fill.confidence, ledger, strategy.name,
                )
                pending = None
            elif when - posted_at >= review_interval:
                result.unfilled[fill.reason or "no fill"] = (
                    result.unfilled.get(fill.reason or "no fill", 0) + 1
                )
                pending = None

        if event.kind is EventKind.SETTLEMENT and position is not None:
            payload = event.payload
            if isinstance(payload, SettlementPayload):
                result.trades.append(
                    _settle(history, position, payload, when)
                )
                _release(ledger, history, position, strategy.name)
                position = None
            continue

        should_decide = event.kind in decision_kinds
        due_for_review = (
            position is not None
            and (last_review is None or when - last_review >= review_interval)
        )
        if not (should_decide or due_for_review):
            continue

        book = history.book_at(when)
        if book is None:
            continue
        outcome = history.outcome_at(when)
        if outcome is None:
            continue

        ctx = _context(history, when, book, outcome)
        features = registry.compute(ctx)
        vectors.append(features)
        alpha = alpha_model.score(ctx, features, outcome)

        if position is not None:
            last_review = when
            decision = strategy.should_exit(position, ctx, features, outcome, alpha)
            if decision.should_exit:
                trade = _close(history, position, book, when, decision.trigger)
                if trade is not None:
                    result.trades.append(trade)
                _release(ledger, history, position, strategy.name)
                position = None
            continue

        if pending is not None:
            continue

        result.decisions_evaluated += 1
        signal = strategy.evaluate(ctx, features, outcome, alpha)
        if not signal.actionable:
            reason = signal.declined_reason or "not actionable"
            result.declined[reason] = result.declined.get(reason, 0) + 1
            continue

        size = strategy.size(signal, history.market, features, outcome, ledger)
        if not size.is_trade:
            key = size.declined.value if size.declined else "unknown"
            result.size_declined[key] = result.size_declined.get(key, 0) + 1
            continue

        order = strategy.propose(signal, history.market, book, size.contracts)
        if order is None:
            result.unfilled["no room to rest inside the touch"] = (
                result.unfilled.get("no room to rest inside the touch", 0) + 1
            )
            continue

        if order.liquidity is Liquidity.TAKER:
            position = _open_taker(
                history, signal, order, book, when, ledger, strategy.name
            )
            if position is None:
                result.unfilled["ask ladder could not fill"] = (
                    result.unfilled.get("ask ladder could not fill", 0) + 1
                )
        else:
            pending = (
                MakerQuote(
                    market_key=history.key,
                    side=order.side,
                    limit_price=order.limit_price,
                    size=order.size,
                    posted_at_best_bid=book.best_bid(order.side),
                    posted_at_best_ask=book.best_ask(order.side),
                ),
                when,
                signal,
            )

    # A position still open at the end of the timeline is NOT counted as a
    # trade. Marking it at the last observed price would invent an exit that
    # never happened, and doing so on the winners is how a backtest acquires a
    # tail of profitable "closes" that are really just unfinished business.
    if position is not None:
        result.notes.append(
            f"{history.key}: position still open when the timeline ended; "
            "excluded rather than marked to the last observed price"
        )
    if pending is not None:
        result.unfilled["still resting when the timeline ended"] = (
            result.unfilled.get("still resting when the timeline ended", 0) + 1
        )


def _context(
    history: MarketHistory,
    when: datetime,
    book: OrderBook,
    outcome: OutcomeEstimate,
) -> FactorContext:
    from .models import implied_probability_from_book  # noqa: PLC0415

    view = AsOfView.build(when, history.timeline.observations_until(when))
    side = Side.YES if outcome.probability >= Decimal("0.5") else Side.NO
    executable = implied_probability_from_book(book, side, Decimal("100"))
    if executable is None:
        executable = implied_probability_from_book(
            book, side, history.market.min_order_size or ONE
        )
    return FactorContext(
        market=history.market,
        view=view,
        book=book,
        model_fair_probability=outcome.probability,
        model_fair_probability_low=outcome.low,
        model_fair_probability_high=outcome.high,
        executable_probability=executable,
        threshold_value=history.threshold_value,
        threshold_comparator=history.threshold_comparator,
        extra=dict(history.factor_extra),
    )


def _open_taker(
    history: MarketHistory,
    signal: Signal,
    order: Any,
    book: OrderBook,
    when: datetime,
    ledger: ExposureLedger,
    strategy_name: str,
) -> OpenPosition | None:
    from ..pricing.orderbook import executable_quote  # noqa: PLC0415

    quote = executable_quote(book, order.side, order.size)
    if quote is None or quote.filled_size <= ZERO:
        return None
    position = OpenPosition(
        market_key=history.key,
        side=order.side,
        liquidity=Liquidity.TAKER,
        entry_price=quote.average_price,
        contracts=quote.filled_size,
        opened_at=when,
        entry_signal=signal,
        fill_confidence=FillConfidence.TAKER_FROM_LADDER.value,
        fills=[(when, quote.filled_size, quote.average_price)],
    )
    _commit(ledger, history, position, strategy_name)
    return position


def _open_position(
    history: MarketHistory,
    signal: Signal,
    quote: MakerQuote,
    filled: Decimal,
    price: Decimal,
    when: datetime,
    confidence: FillConfidence,
    ledger: ExposureLedger,
    strategy_name: str,
) -> OpenPosition:
    position = OpenPosition(
        market_key=history.key,
        side=quote.side,
        liquidity=Liquidity.MAKER,
        entry_price=price,
        contracts=filled,
        opened_at=when,
        entry_signal=signal,
        fill_confidence=confidence.value,
        fills=[(when, filled, price)],
    )
    _commit(ledger, history, position, strategy_name)
    return position


def _commit(
    ledger: ExposureLedger,
    history: MarketHistory,
    position: OpenPosition,
    strategy_name: str,
) -> None:
    from .sizing import day_key  # noqa: PLC0415

    ledger.commit(
        notional=position.notional,
        market_key=history.key,
        category=history.market.category.value,
        strategy=strategy_name,
        day=day_key(position.opened_at),
        correlation_group=history.market.platform_event_id or history.key,
    )


def _release(
    ledger: ExposureLedger,
    history: MarketHistory,
    position: OpenPosition,
    strategy_name: str,
) -> None:
    ledger.release(
        notional=position.notional,
        market_key=history.key,
        category=history.market.category.value,
        strategy=strategy_name,
        correlation_group=history.market.platform_event_id or history.key,
    )


def _entry_fee(history: MarketHistory, position: OpenPosition) -> Decimal:
    from ..pricing.fees import fee_per_contract  # noqa: PLC0415

    from .execution import _maker_fee_per_contract  # noqa: PLC0415

    if position.liquidity is Liquidity.MAKER:
        return _maker_fee_per_contract(
            history.market, position.contracts, position.entry_price
        )
    return fee_per_contract(
        history.market.platform,
        position.contracts,
        position.entry_price,
        rate=history.market.fee_rate,
        fee_type=history.market.fee_type,
    )


def _close(
    history: MarketHistory,
    position: OpenPosition,
    book: OrderBook,
    when: datetime,
    trigger: ExitTrigger | None,
) -> SimulatedTrade | None:
    """Close into the bid ladder, net of the exit fee.

    Returns ``None`` when there is no bid. A position that cannot be sold has not
    been exited, and recording one at the last ask would be the single most
    flattering possible lie: it converts every illiquid loser into a clean close.
    """
    proceeds = exit_proceeds_per_contract(
        history.market, book, position.side, position.contracts
    )
    if proceeds is None:
        return None
    entry_fee = _entry_fee(history, position)
    gross = quantize_usd((proceeds - position.entry_price) * position.contracts)
    net = quantize_usd(gross - entry_fee * position.contracts)
    return SimulatedTrade(
        market_key=position.market_key,
        side=position.side,
        liquidity=position.liquidity,
        contracts=position.contracts,
        requested_contracts=position.entry_signal.components.get(
            "requested_contracts", position.contracts
        ),
        entry_price=position.entry_price,
        entry_at=position.opened_at,
        exit_price=proceeds,
        exit_at=when,
        exit_trigger=trigger,
        fill_confidence=FillConfidence(position.fill_confidence),
        entry_fee=quantize_usd(entry_fee),
        # The exit fee is already inside `proceeds`; reported as zero here so
        # the two are never added twice.
        exit_fee=ZERO,
        gross_pnl=gross,
        net_pnl=net,
        entry_signal=position.entry_signal,
        category=history.market.category.value,
        notes=("exit priced into the bid ladder, net of the closing fee",),
    )


def _settle(
    history: MarketHistory,
    position: OpenPosition,
    payload: SettlementPayload,
    when: datetime,
) -> SimulatedTrade:
    """Terminal payout from the venue's published value.

    ``yes_payout`` is what the venue paid, including a 0.5 for a 50-50
    resolution. Deriving it from a result string instead would lose that case and
    silently score a split as a loss.
    """
    payout = (
        payload.yes_payout
        if position.side is Side.YES
        else ONE - payload.yes_payout
    )
    entry_fee = _entry_fee(history, position)
    gross = quantize_usd((payout - position.entry_price) * position.contracts)
    net = quantize_usd(gross - entry_fee * position.contracts)
    return SimulatedTrade(
        market_key=position.market_key,
        side=position.side,
        liquidity=position.liquidity,
        contracts=position.contracts,
        requested_contracts=position.contracts,
        entry_price=position.entry_price,
        entry_at=position.opened_at,
        exit_price=quantize_prob(payout),
        exit_at=when,
        exit_trigger=ExitTrigger.SETTLED,
        fill_confidence=FillConfidence(position.fill_confidence),
        entry_fee=quantize_usd(entry_fee),
        exit_fee=ZERO,
        gross_pnl=gross,
        net_pnl=net,
        entry_signal=position.entry_signal,
        category=history.market.category.value,
        notes=(f"settled from the venue's published payout via {payload.source}",),
    )
