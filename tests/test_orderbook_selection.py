"""Which markets get an order book fetched.

This function decides what the whole platform can see. A market that never gets a
book cannot be scored, cannot be arbitraged, and cannot have its execution cost
computed — it is ingested and then invisible. It had no tests.

The clock is pinned in every case. Eligibility depends on time to resolution, so a
test reading the wall clock would assert on a different branch depending on the day
it ran, which this suite already treats as a defect elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category, MarketStatus, Platform
from pmvl_shared.schemas import NormalizedMarket

from pmvl_markets.ingest.runner import (
    MAX_SERIES_BUDGET_SHARE,
    MIN_PER_SERIES_BOOKS,
    select_for_orderbooks,
)

#: A fixed "now" every case is evaluated against.
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _restore_share():
    """Each case sets the reserve share; none may leak into the next."""
    settings = get_settings()
    original = settings.orderbook_coverage_share
    yield
    settings.orderbook_coverage_share = original


def share(value: float) -> None:
    get_settings().orderbook_coverage_share = value


def market(
    ticker: str,
    *,
    category: Category = Category.OTHER,
    days_out: float = 1,
    volume: str = "50000",
    series: str | None = None,
    event: str | None = None,
    status: MarketStatus = MarketStatus.OPEN,
    accepting: bool = True,
) -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id=ticker,
        series_ticker=series,
        platform_event_id=event,
        title=ticker,
        category=category,
        status=status,
        accepting_orders=accepting,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("0.01"),
        fee_rate=Decimal("0.07"),
        fee_type="quadratic",
        expected_resolution_time=NOW + timedelta(days=days_out),
        volume_24h=Decimal(volume),
    )


def ids(markets) -> set[str]:  # noqa: ANN001
    return {m.platform_market_id for m in markets}


class TestTheOffSwitch:
    """`orderbook_coverage_share = 0` must mean what the setting says it means.

    An operator reaching for it is doing so because something has gone wrong and
    they want the previous allocation back. A switch that silently changes
    behaviour instead of restoring it is worse than no switch.
    """

    def test_zero_share_selects_nothing_outside_the_ranking_horizon(self) -> None:
        share(0.0)
        universe = [
            market("NEAR", days_out=2, volume="1000"),
            market("FAR", days_out=400, volume="9999999"),  # vastly more liquid
        ]
        assert ids(select_for_orderbooks(universe, 10, now=NOW)) == {"NEAR"}

    def test_zero_share_leaves_budget_unspent_rather_than_widening_scope(self) -> None:
        share(0.0)
        universe = [market("NEAR", days_out=2)] + [
            market(f"FAR{i}", days_out=400) for i in range(50)
        ]
        # One eligible market, a budget of 20: the answer is one book, not twenty.
        assert len(select_for_orderbooks(universe, 20, now=NOW)) == 1


class TestCoverageReserve:
    def test_reaches_liquid_markets_beyond_the_ranking_horizon(self) -> None:
        """The change this reserve exists for.

        On the live universe the 30-day window is the tightest filter by far, and
        the contracts it excludes include the most heavily traded on either venue.
        Execution cost is computable for all of them.
        """
        share(0.5)
        universe = [
            market("NEAR", days_out=2, volume="1000"),
            market("FED", days_out=400, volume="3500000"),
        ]
        selected = ids(select_for_orderbooks(universe, 10, now=NOW))
        assert "FED" in selected
        assert "NEAR" in selected

    def test_ranks_the_reserve_by_volume_not_modellability(self) -> None:
        share(1.0)
        universe = [
            market("QUIET_CRYPTO", category=Category.CRYPTO, days_out=1, volume="600"),
            market("BUSY_POLITICS", category=Category.POLITICS, days_out=1, volume="900000"),
        ]
        # Budget of exactly one: the liquid one wins despite not being modellable.
        assert ids(select_for_orderbooks(universe, 1, now=NOW)) == {"BUSY_POLITICS"}

    def test_a_long_dated_market_is_not_ranked_below_a_near_one(self) -> None:
        # Time to resolution is already priced as capital cost; it must not also
        # push a contract down the coverage queue.
        share(1.0)
        universe = [
            market("NEAR_QUIET", days_out=1, volume="600"),
            market("FAR_BUSY", days_out=900, volume="500000"),
        ]
        assert ids(select_for_orderbooks(universe, 1, now=NOW)) == {"FAR_BUSY"}


class TestScoringReserveIsProtected:
    def test_liquid_unmodellable_markets_cannot_take_the_whole_budget(self) -> None:
        """The property that makes this a reserve split rather than a reweighting.

        Under one blended key the scanner would lose its input entirely on any day
        when sports happened to be busier than crypto.
        """
        share(0.4)
        scoreable = [
            market(f"CRYPTO{i}", category=Category.CRYPTO, days_out=1, volume="600")
            for i in range(20)
        ]
        liquid = [
            market(f"SPORT{i}", category=Category.SPORTS, days_out=400, volume="900000")
            for i in range(200)
        ]
        selected = select_for_orderbooks(scoreable + liquid, 10, now=NOW)
        scoring_slots = sum(1 for m in selected if m.category is Category.CRYPTO)
        # 40% reserved for coverage leaves 60% as a floor for scoring.
        assert scoring_slots >= 6

    def test_scoring_keeps_its_own_priority_order(self) -> None:
        share(0.4)
        universe = [
            market("MODELLABLE", category=Category.CRYPTO, days_out=1, volume="600"),
            market("PLAIN", category=Category.OTHER, days_out=1, volume="700"),
        ]
        # One scoring slot: modellability still decides it, as it always did.
        selected = select_for_orderbooks(universe, 2, now=NOW)
        assert "MODELLABLE" in ids(selected)


class TestPreservedInvariants:
    """Properties that predate this change and must survive it."""

    def test_the_volume_floor_applies_to_both_reserves(self) -> None:
        share(0.5)
        floor = get_settings().min_volume_24h_usd
        universe = [
            market("DUST_NEAR", days_out=1, volume=str(floor - 1)),
            market("DUST_FAR", days_out=400, volume=str(floor - 1)),
        ]
        assert select_for_orderbooks(universe, 10, now=NOW) == []

    def test_closed_and_non_accepting_markets_are_never_selected(self) -> None:
        share(0.5)
        universe = [
            market("CLOSED", status=MarketStatus.CLOSED),
            market("HALTED", accepting=False),
        ]
        assert select_for_orderbooks(universe, 10, now=NOW) == []

    def test_no_single_series_monopolises_the_budget(self) -> None:
        # Kalshi's daily Bitcoin board publishes hundreds of liquid strikes and
        # used to consume the entire allocation.
        share(0.4)
        limit = 100
        board = [
            market(f"BTC{i}", series="KXBTC", days_out=1, volume="900000")
            for i in range(300)
        ]
        others = [
            market(f"OTHER{i}", series=f"S{i}", days_out=1, volume="600")
            for i in range(300)
        ]
        selected = select_for_orderbooks(board + others, limit, now=NOW)
        from_board = sum(1 for m in selected if m.series_ticker == "KXBTC")
        cap = max(MIN_PER_SERIES_BOOKS, int(limit * MAX_SERIES_BUDGET_SHARE))
        assert from_board <= cap

    def test_an_event_is_taken_whole_when_the_budget_allows(self) -> None:
        # Multi-outcome arbitrage cannot be assessed on a partial basket.
        share(0.4)
        siblings = [
            market(f"TEMP{i}", series="KXTEMP", event="EV1", days_out=1, volume="600")
            for i in range(5)
        ]
        selected = ids(select_for_orderbooks(siblings, 50, now=NOW))
        assert selected == {f"TEMP{i}" for i in range(5)}

    def test_never_exceeds_the_limit_and_never_duplicates(self) -> None:
        share(0.4)
        universe = [
            market(f"M{i}", days_out=1 if i % 2 else 400, volume=str(1000 + i))
            for i in range(400)
        ]
        selected = select_for_orderbooks(universe, 37, now=NOW)
        assert len(selected) <= 37
        assert len(ids(selected)) == len(selected)

    def test_an_empty_universe_is_not_an_error(self) -> None:
        share(0.4)
        assert select_for_orderbooks([], 10, now=NOW) == []


class TestClockInjection:
    def test_the_horizon_is_measured_from_the_supplied_instant(self) -> None:
        """Without this the scoring pool depends on the day the caller runs."""
        share(0.0)
        universe = [market("M", days_out=10, volume="1000")]

        assert ids(select_for_orderbooks(universe, 5, now=NOW)) == {"M"}
        # Twenty days later the same contract is in the past, so it drops out of
        # the scoring pool entirely.
        later = NOW + timedelta(days=20)
        assert select_for_orderbooks(universe, 5, now=later) == []
