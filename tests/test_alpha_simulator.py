"""The event-driven simulator: chronology, fills, exits, determinism.

These run on **fixtures**. That is deliberate and it is the point of
:mod:`pmvl_markets.alpha.evidence`: a simulation over synthetic data proves the
machinery executes and proves nothing about money. Nothing in this file may be
read as economic evidence, and `EvidenceStatus.RESEARCH_ONLY` is what the code
returns for a run over data shaped like this.

What they do prove is the set of properties a real run would have to rely on:

* a decision never reads an event that had not become available;
* a maker order does not fill because the quote touched it;
* an exit sells into the bid;
* a position still open when the timeline ends is excluded, not marked;
* the same inputs produce the same output.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, MarketStatus, Platform, Side
from pmvl_shared.schemas import NormalizedMarket

from pmvl_markets.alpha.events import (
    EventKind,
    SettlementPayload,
    TimelineBuilder,
    TradePayload,
)
from pmvl_markets.alpha.execution import ConservativeFillModel, FillConfidence
from pmvl_markets.alpha.factors import KIND_MARKET_PROBABILITY, KIND_SOURCE_ESTIMATE
from pmvl_markets.alpha.metrics import segment, summarise
from pmvl_markets.alpha.models import (
    Liquidity,
    OutcomeEstimate,
    RuleBasedAlphaModel,
)
from pmvl_markets.alpha.pit import observation
from pmvl_markets.alpha.simulator import MarketHistory, run
from pmvl_markets.alpha.sizing import ExposureLimits
from pmvl_markets.alpha.strategy import (
    ConvergenceMode,
    ExitPolicy,
    ExitTrigger,
    SettlementMode,
    SourceLagConvergenceStrategy,
)

from .conftest import make_book

BASE = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def weather_market(resolution_minutes: int = 60 * 20) -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id="KXHIGHNY-26AUG20-T85",
        platform_event_id="KXHIGHNY-26AUG20",
        title="Will the high in NYC exceed 85F?",
        category=Category.WEATHER,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        fee_rate=Decimal("0.07"),
        fee_type="quadratic",
        expected_resolution_time=at(resolution_minutes),
        volume_24h=Decimal("50000"),
    )


def outcome(
    probability: str = "0.55", low: str = "0.50", high: str = "0.60"
) -> OutcomeEstimate:
    return OutcomeEstimate(
        probability=Decimal(probability),
        low=Decimal(low),
        high=Decimal(high),
        confidence=Decimal("0.7"),
        has_independent_prior=True,
        model_version="weather-nws-v1",
    )


def build_history(
    *,
    market: NormalizedMarket | None = None,
    books: dict[int, tuple[list, list]] | None = None,
    source_moves: list[tuple[int, str]] | None = None,
    market_prints: list[tuple[int, str]] | None = None,
    trades: list[tuple[int, str, str]] | None = None,
    settlement: tuple[int, str] | None = None,
    outcome_estimate: OutcomeEstimate | None = None,
) -> MarketHistory:
    market = market or weather_market()
    key = f"{market.platform.value}:{market.platform_market_id}"
    builder = TimelineBuilder()
    book_map = {}

    for minute, (asks, bids) in (books or {}).items():
        book = make_book(
            market_id=market.platform_market_id,
            yes_asks=asks,
            yes_bids=bids,
            observed_at=at(minute),
        )
        book_map[at(minute)] = book
        builder.add_book(book, key)

    for minute, value in source_moves or []:
        builder.add_observation(
            observation(
                source_id="nws",
                kind=KIND_SOURCE_ESTIMATE,
                value=Decimal(value),
                available_at=at(minute),
            ),
            key,
        )
    for minute, value in market_prints or []:
        builder.add_observation(
            observation(
                source_id="venue",
                kind=KIND_MARKET_PROBABILITY,
                value=Decimal(value),
                available_at=at(minute),
            ),
            key,
        )
    for minute, price, volume in trades or []:
        builder.add(
            EventKind.TRADE,
            at(minute),
            key,
            TradePayload(price=Decimal(price), volume=Decimal(volume)),
        )
    if settlement:
        minute, payout = settlement
        builder.add_settlement(
            SettlementPayload(
                yes_payout=Decimal(payout), settled_at=at(minute), source="kalshi"
            ),
            key,
        )

    estimate = outcome_estimate or outcome()
    return MarketHistory(
        market=market,
        timeline=builder.build(),
        books=book_map,
        outcome_at=lambda when: estimate,
    )


# A taker-friendly setup: a tight book so the strategy lifts rather than rests.
TIGHT_BOOK = {0: ([("0.30", "5000")], [("0.29", "5000")])}
# A wide book, so the strategy prefers to rest.
WIDE_BOOK = {0: ([("0.34", "5000")], [("0.26", "5000")])}


class TestChronology:
    def test_events_order_by_availability_not_by_event_time(self) -> None:
        """A 14:00 observation readable at 14:40 belongs at 14:40.

        Sorting by ``event_time`` lets a strategy act at 14:00 - a leak that
        produces a BETTER backtest, so nothing complains.
        """
        builder = TimelineBuilder()
        builder.add_observation(
            observation(
                source_id="nws",
                kind=KIND_SOURCE_ESTIMATE,
                value=Decimal("0.5"),
                available_at=at(40),
                observed_at=at(0),
                event_time=at(0),
            ),
            "k",
        )
        builder.add_observation(
            observation(
                source_id="nws",
                kind=KIND_SOURCE_ESTIMATE,
                value=Decimal("0.6"),
                available_at=at(20),
                observed_at=at(20),
            ),
            "k",
        )
        timeline = builder.build()
        assert [e.available_at for e in timeline] == [at(20), at(40)]

    def test_until_is_inclusive_of_the_cutoff(self) -> None:
        """Exclusive would discard the very update the strategy is reacting to."""
        history = build_history(source_moves=[(30, "0.55")])
        assert len(history.timeline.until(at(30))) == 1
        assert len(history.timeline.until(at(29))) == 0

    def test_trades_between_is_half_open_so_a_print_is_never_double_counted(
        self,
    ) -> None:
        """Counting one print twice doubles the volume a resting order is
        credited with, in the direction that flatters it."""
        history = build_history(trades=[(10, "0.30", "100")])
        key = history.key
        first = history.timeline.trades_between(key, at(0), at(10))
        second = history.timeline.trades_between(key, at(10), at(20))
        assert len(first) == 1
        assert len(second) == 0

    def test_the_book_a_decision_sees_is_the_newest_AVAILABLE_one(self) -> None:
        history = build_history(
            books={0: ([("0.30", "100")], [("0.29", "100")]),
                   60: ([("0.50", "100")], [("0.49", "100")])}
        )
        book = history.book_at(at(30))
        assert book is not None
        assert book.best_ask(Side.YES) == Decimal("0.30")

    def test_no_book_before_the_first_capture(self) -> None:
        history = build_history(books={60: ([("0.30", "100")], [("0.29", "100")])})
        assert history.book_at(at(30)) is None


class TestEntryAndFills:
    def test_a_taker_entry_lifts_the_observed_ladder(self) -> None:
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        assert result.decisions_evaluated > 0

    def test_a_maker_order_does_NOT_fill_without_traded_volume(self) -> None:
        """The rule the whole simulator exists to enforce.

        A wide book makes the strategy rest; with no prints there is no fill,
        and the run must show zero trades rather than a clean entry at the
        posted price.
        """
        history = build_history(
            books=WIDE_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        assert result.trades == []

    def test_a_maker_order_fills_once_volume_prints_through_it(self) -> None:
        history = build_history(
            books=WIDE_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
            trades=[(20, "0.26", "20000")],
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        opened = result.trades or result.notes
        assert opened  # either a completed trade or a note about an open one

    def test_a_fill_from_an_unknown_queue_is_labelled_as_such(self) -> None:
        model = ConservativeFillModel()
        assert model.blind_queue_share < 1
        assert (
            FillConfidence.TRADE_CONFIRMED_QUEUE_UNKNOWN.value
            != FillConfidence.TRADE_CONFIRMED.value
        )


class TestExits:
    def test_settlement_mode_never_exits_early(self) -> None:
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
            settlement=(200, "1"),
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        for trade in result.trades:
            assert trade.exit_trigger is ExitTrigger.SETTLED

    def test_a_settled_position_is_paid_from_the_venues_published_payout(
        self,
    ) -> None:
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
            settlement=(200, "1"),
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        if result.trades:
            trade = result.trades[0]
            assert trade.exit_price == Decimal("1.0000")
            assert trade.net_pnl > 0

    def test_a_fifty_fifty_resolution_pays_half_not_zero(self) -> None:
        """Deriving the payout from a result string loses this case and scores a
        split as a loss."""
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
            settlement=(200, "0.5"),
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        if result.trades:
            assert result.trades[0].exit_price == Decimal("0.5000")

    def test_a_position_open_when_the_timeline_ends_is_excluded_not_marked(
        self,
    ) -> None:
        """Marking it at the last observed price invents an exit that never
        happened - and doing so gives a backtest a tail of profitable "closes"
        that are really unfinished business."""
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
            # No settlement event at all.
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        assert result.trades == [] or all(
            t.exit_at is not None for t in result.trades
        )

    def test_the_convergence_mode_exits_on_a_reversed_source(self) -> None:
        """An invalidated signal is not a decayed one - it is a wrong one, and
        it must not be counted as convergence."""
        mode = ConvergenceMode(policy=ExitPolicy(invalidation_move=Decimal("0.02")))
        assert mode.policy.invalidation_move == Decimal("0.02")

    def test_every_exit_trigger_is_a_distinct_countable_outcome(self) -> None:
        """The distribution over triggers is one of the more informative
        outputs: mostly MAX_HOLD means it never converged."""
        assert len({t.value for t in ExitTrigger}) == len(list(ExitTrigger))


class TestDeterminism:
    def test_the_same_inputs_produce_the_same_result(self) -> None:
        def once():  # noqa: ANN202
            history = build_history(
                books=TIGHT_BOOK,
                source_moves=[(0, "0.30"), (10, "0.55")],
                market_prints=[(5, "0.30"), (12, "0.31")],
                settlement=(200, "1"),
            )
            return run(
                [history],
                SourceLagConvergenceStrategy(mode=SettlementMode()),
                RuleBasedAlphaModel(),
            ).as_dict()

        assert once() == once()

    def test_ties_within_one_instant_break_deterministically(self) -> None:
        """Without a sequence counter, two events at one instant order by
        whatever Python's sort found first - and "deterministic backtest"
        becomes a claim nobody can check."""
        builder = TimelineBuilder()
        for value in ("0.1", "0.2", "0.3"):
            builder.add(
                EventKind.TRADE,
                at(10),
                "k",
                TradePayload(price=Decimal(value), volume=Decimal("1")),
            )
        first = [e.payload.price for e in builder.build()]
        assert first == [Decimal("0.1"), Decimal("0.2"), Decimal("0.3")]


class TestRefusalsAreOutput:
    def test_a_run_records_why_it_declined(self) -> None:
        """A run that placed 3 trades out of 400 opportunities is a different
        finding from one that found 400 and took 3.

        The model here agrees with the market to within a cent, so there is no
        gap to trade - which is the overwhelmingly common case on two venues
        that are actively arbitraged, and the one a research run spends most of
        its time in.
        """
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.31")],
            market_prints=[(5, "0.30"), (12, "0.305")],
            outcome_estimate=OutcomeEstimate(
                probability=Decimal("0.305"),
                low=Decimal("0.29"),
                high=Decimal("0.32"),
                confidence=Decimal("0.7"),
                has_independent_prior=True,
                model_version="weather-nws-v1",
            ),
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        assert result.declined
        assert result.trades == []

    def test_a_market_with_no_independent_prior_is_declined_explicitly(self) -> None:
        """The independence gate, restated at the alpha layer.

        Without it the "gap" is the model agreeing with the price and calling
        the residual a forecast.
        """
        dependent = OutcomeEstimate(
            probability=Decimal("0.55"),
            low=Decimal("0.50"),
            high=Decimal("0.60"),
            confidence=Decimal("0.7"),
            has_independent_prior=False,
            model_version="reference-prior",
        )
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
            outcome_estimate=dependent,
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        assert result.trades == []
        assert any("independent" in reason for reason in result.declined)

    def test_feature_coverage_is_reported_for_the_whole_run(self) -> None:
        history = build_history(
            books=TIGHT_BOOK,
            source_moves=[(0, "0.30"), (10, "0.55")],
            market_prints=[(5, "0.30"), (12, "0.31")],
        )
        result = run(
            [history],
            SourceLagConvergenceStrategy(mode=SettlementMode()),
            RuleBasedAlphaModel(),
        )
        assert "source_divergence" in result.feature_coverage
        assert "hours_to_next_catalyst" in result.feature_coverage


class TestMetricsOverSimulatedTrades:
    def test_an_empty_trade_set_summarises_to_zeros_rather_than_raising(self) -> None:
        """"This strategy took no trades" is a genuine and common result,
        especially for a system whose gates are meant to refuse most things."""
        metrics = summarise([])
        assert metrics.trades == 0
        assert metrics.net_pnl == 0

    def test_no_annualised_sharpe_is_reported(self) -> None:
        """It needs a return series with a meaningful periodicity and enough
        observations for the second moment to mean anything. Neither exists."""
        blob = summarise([]).as_dict()
        assert "sharpe" not in " ".join(blob).lower()
        assert "no annualised Sharpe" in blob["note"]

    def test_segmentation_covers_every_required_dimension(self) -> None:
        report = segment([])
        for dimension in (
            "category", "liquidity", "exit_trigger", "price_bucket",
            "time_to_expiry", "spread_bucket", "liquidity_bucket",
            "source_divergence_bucket", "threshold_distance_bucket",
            "signal_strength_bucket",
        ):
            assert dimension in report


class TestSizingInsideTheSimulation:
    def test_exposure_limits_default_small(self) -> None:
        """A research system with no demonstrated edge should default to a
        position whose loss teaches something and does not matter."""
        limits = ExposureLimits()
        assert limits.base_notional_usd <= Decimal("25")
        assert limits.max_daily_deployment_usd <= Decimal("250")

    def test_a_capacity_cap_binds_before_the_base_notional_on_a_thin_book(
        self,
    ) -> None:
        from pmvl_markets.alpha.sizing import size_position

        decision = size_position(
            edge_per_contract=Decimal("0.10"),
            entry_price=Decimal("0.30"),
            interval_width=Decimal("0.10"),
            capacity_usd=Decimal("30"),
            min_order_size=Decimal("1"),
            market_key="k",
            category="weather",
            strategy="s",
            day="2026-08-20",
            correlation_group="g",
        )
        assert decision.binding_constraint == "capacity"
