"""Equity index model and the NYSE trading calendar it depends on."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from pmvl_shared.enums import Category, Platform
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from pmvl_markets.probability.base import ModelContext
from pmvl_markets.probability.categories.equity import (
    EquityIndexThresholdModel,
    detect_index,
    gbm_probability_above,
)
from pmvl_markets.probability.trading_calendar import (
    EARLY_CLOSE,
    REGULAR_CLOSE,
    TRADING_HOURS_PER_YEAR,
    is_market_open,
    is_trading_day,
    next_session_open,
    session_close,
    trading_hours_between,
    trading_years_between,
)

ET = ZoneInfo("America/New_York")


def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestTradingCalendar:
    def test_weekends_are_closed(self) -> None:
        assert not is_trading_day(date(2026, 7, 25))  # Saturday
        assert not is_trading_day(date(2026, 7, 26))  # Sunday
        assert is_trading_day(date(2026, 7, 27))      # Monday

    def test_holidays_are_closed(self) -> None:
        assert not is_trading_day(date(2026, 7, 3))    # Independence Day observed
        assert not is_trading_day(date(2026, 11, 26))  # Thanksgiving
        assert not is_trading_day(date(2026, 12, 25))  # Christmas

    def test_early_close_days(self) -> None:
        assert session_close(date(2026, 12, 24)) == EARLY_CLOSE
        assert session_close(date(2026, 7, 27)) == REGULAR_CLOSE

    def test_full_session_is_six_and_a_half_hours(self) -> None:
        assert trading_hours_between(et(2026, 7, 27, 9, 30), et(2026, 7, 27, 16, 0)) == 6.5

    def test_two_sessions(self) -> None:
        assert trading_hours_between(et(2026, 7, 27, 9, 30), et(2026, 7, 28, 16, 0)) == 13.0

    def test_weekend_contributes_nothing(self) -> None:
        """Friday close to Monday open is three calendar days and zero trading time."""
        assert trading_hours_between(et(2026, 7, 24, 16, 0), et(2026, 7, 27, 9, 30)) == 0.0

    def test_the_overnight_case_that_motivates_this_module(self) -> None:
        """Sunday evening to Monday 10:00 ET is 13 calendar hours, 0.5 trading hours."""
        start, end = et(2026, 7, 26, 21, 0), et(2026, 7, 27, 10, 0)
        calendar = (end - start).total_seconds() / 3600.0
        trading = trading_hours_between(start, end)
        assert calendar == pytest.approx(13.0)
        assert trading == pytest.approx(0.5)
        # Variance scales with time, so the width error is the square root.
        assert math.sqrt(calendar / trading) > 5.0

    def test_holiday_is_skipped_inside_a_range(self) -> None:
        # Jul 2 (Thu) close -> Jul 6 (Mon) close, with Jul 3 a holiday and a weekend.
        assert trading_hours_between(et(2026, 7, 2, 16, 0), et(2026, 7, 6, 16, 0)) == 6.5

    def test_partial_session_at_both_ends(self) -> None:
        assert trading_hours_between(et(2026, 7, 27, 12, 0), et(2026, 7, 27, 13, 0)) == 1.0

    def test_before_open_clamps_to_open(self) -> None:
        assert trading_hours_between(et(2026, 7, 27, 6, 0), et(2026, 7, 27, 10, 30)) == 1.0

    def test_after_close_clamps_to_close(self) -> None:
        assert trading_hours_between(et(2026, 7, 27, 15, 0), et(2026, 7, 27, 23, 0)) == 1.0

    def test_backwards_range_is_zero(self) -> None:
        assert trading_hours_between(et(2026, 7, 27, 16, 0), et(2026, 7, 27, 9, 30)) == 0.0

    def test_trading_years_conversion(self) -> None:
        one_session = trading_years_between(et(2026, 7, 27, 9, 30), et(2026, 7, 27, 16, 0))
        assert one_session == pytest.approx(6.5 / TRADING_HOURS_PER_YEAR)
        # A trading year is 252 sessions.
        assert one_session * 252 == pytest.approx(1.0, rel=1e-9)

    def test_is_market_open(self) -> None:
        assert is_market_open(et(2026, 7, 27, 10, 0))
        assert not is_market_open(et(2026, 7, 27, 8, 0))
        assert not is_market_open(et(2026, 7, 26, 12, 0))  # Sunday
        assert not is_market_open(et(2026, 7, 27, 16, 0))  # close is exclusive

    def test_next_session_open(self) -> None:
        # Sunday evening -> Monday 09:30.
        assert next_session_open(et(2026, 7, 26, 21, 0)) == et(2026, 7, 27, 9, 30)
        # Mid-session -> now.
        assert next_session_open(et(2026, 7, 27, 11, 0)) == et(2026, 7, 27, 11, 0)


class TestIndexDetection:
    @pytest.mark.parametrize(
        "text,symbol",
        [
            ("Will the S&P 500 be above 7545?", "^GSPC"),
            ("Will the Nasdaq-100 be above 29800?", "^NDX"),
            ("Will the Dow Jones close above 45000?", "^DJI"),
            ("Will the Russell 2000 finish above 2400?", "^RUT"),
        ],
    )
    def test_recognised_indices(self, text: str, symbol: str) -> None:
        assert detect_index(text)[0] == symbol

    def test_unrecognised_returns_none(self) -> None:
        assert detect_index("Will it rain in Chicago tomorrow?") is None
        # A single stock is deliberately not modelled.
        assert detect_index("Will Apple close above $250?") is None


class TestGbmOnTradingTime:
    def test_deep_itm_and_otm(self) -> None:
        tau = 0.5 / TRADING_HOURS_PER_YEAR
        assert gbm_probability_above(7412.0, 6000.0, 0.18, tau) > 0.999
        assert gbm_probability_above(7412.0, 9000.0, 0.18, tau) < 0.001

    def test_at_the_money_is_near_half(self) -> None:
        tau = 6.5 / TRADING_HOURS_PER_YEAR
        assert gbm_probability_above(7412.0, 7412.0, 0.18, tau) == pytest.approx(0.5, abs=0.01)

    def test_monotonic_in_strike(self) -> None:
        tau = 6.5 / TRADING_HOURS_PER_YEAR
        probs = [gbm_probability_above(7412.0, k, 0.18, tau) for k in (7300, 7400, 7500, 7600)]
        assert all(probs[i] >= probs[i + 1] for i in range(len(probs) - 1))

    def test_calendar_time_would_inflate_a_tail_strike(self) -> None:
        """The exact error the trading calendar exists to prevent."""
        spot, sigma, strike = 7411.98, 0.177, 7500.0
        trading_tau = 0.5 / TRADING_HOURS_PER_YEAR
        calendar_tau = 12.4 / (24 * 365.25)
        correct = gbm_probability_above(spot, strike, sigma, trading_tau)
        inflated = gbm_probability_above(spot, strike, sigma, calendar_tau)
        assert correct < 0.005
        assert inflated > 0.02
        # Two orders of magnitude on a contract quoted near a cent.
        assert inflated / max(correct, 1e-12) > 10

    def test_degenerate_inputs(self) -> None:
        assert gbm_probability_above(0.0, 7500.0, 0.18, 0.01) == 0.0
        assert gbm_probability_above(7412.0, 7500.0, 0.18, 0.0) == 0.0
        assert gbm_probability_above(7600.0, 7500.0, 0.18, 0.0) == 1.0


def _market(**overrides) -> NormalizedMarket:  # noqa: ANN003
    base = dict(
        platform=Platform.KALSHI,
        platform_market_id="KXINXU-TEST",
        title="Will the S&P 500 be above 7500 on Jul 27, 2026 at 10am EDT?",
        subtitle="7,500 or above",
        category=Category.FINANCE,
        strike_type="greater_or_equal",
        floor_strike=Decimal("7500"),
    )
    base.update(overrides)
    return NormalizedMarket(**base)


class TestEquityModelGuards:
    """Every branch that must decline to produce a number."""

    @pytest.mark.asyncio
    async def test_no_index_in_text(self) -> None:
        model = EquityIndexThresholdModel()
        try:
            result = await model.estimate(
                ModelContext(
                    market=_market(
                        title="Will Apple close above $250?",
                        subtitle="",
                        event_occurrence_time=utcnow() + timedelta(days=1),
                    )
                )
            )
        finally:
            await model.aclose()
        assert result.probability is None
        assert "index" in result.detail

    @pytest.mark.asyncio
    async def test_settled_market_declines(self) -> None:
        model = EquityIndexThresholdModel()
        try:
            result = await model.estimate(
                ModelContext(
                    market=_market(event_occurrence_time=utcnow() - timedelta(hours=1))
                )
            )
        finally:
            await model.aclose()
        assert result.probability is None
        assert "passed" in result.detail

    @pytest.mark.asyncio
    async def test_no_trading_time_left_declines(self) -> None:
        """A Saturday-settling market has no trading time and cannot be modelled."""
        model = EquityIndexThresholdModel()
        saturday = datetime(2026, 8, 1, 14, 0, tzinfo=ZoneInfo("UTC"))
        friday_evening = datetime(2026, 7, 31, 22, 0, tzinfo=ZoneInfo("UTC"))
        try:
            result = await model.estimate(
                ModelContext(
                    market=_market(event_occurrence_time=saturday), now=friday_evening
                )
            )
        finally:
            await model.aclose()
        assert result.probability is None
        assert "trading hours" in result.detail

    @pytest.mark.asyncio
    async def test_between_without_both_bounds_declines(self) -> None:
        model = EquityIndexThresholdModel()
        try:
            result = await model.estimate(
                ModelContext(
                    market=_market(
                        strike_type="between",
                        cap_strike=None,
                        event_occurrence_time=utcnow() + timedelta(days=1),
                    )
                )
            )
        finally:
            await model.aclose()
        assert result.probability is None
        assert "bounds" in result.detail

    @pytest.mark.asyncio
    async def test_barrier_wording_declines(self) -> None:
        """No touch model is fitted for indices, so refuse rather than misprice."""
        model = EquityIndexThresholdModel()
        try:
            result = await model.estimate(
                ModelContext(
                    market=_market(
                        title="Will the S&P 500 reach 7500 this week?",
                        event_occurrence_time=utcnow() + timedelta(days=1),
                    )
                )
            )
        finally:
            await model.aclose()
        assert result.probability is None
        assert "touch" in result.detail

    @pytest.mark.asyncio
    async def test_no_settlement_instant_declines(self) -> None:
        model = EquityIndexThresholdModel()
        try:
            result = await model.estimate(ModelContext(market=_market()))
        finally:
            await model.aclose()
        assert result.probability is None
        assert "settlement instant" in result.detail

    def test_model_is_independent_and_capped(self) -> None:
        model = EquityIndexThresholdModel()
        assert model.independent is True
        assert model.categories == (Category.FINANCE,)
        # Below the crypto cap: scheduled macro events are invisible to realised vol.
        assert Decimal("0") < model.max_confidence <= Decimal("0.65")
