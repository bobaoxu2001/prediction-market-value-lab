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
    EFFECTIVE_HOURS_PER_DAY,
    EFFECTIVE_HOURS_PER_YEAR,
    OVERNIGHT_VARIANCE_WEIGHT,
    REGULAR_CLOSE,
    TRADING_HOURS_PER_YEAR,
    effective_volatility_hours,
    is_futures_open,
    is_market_open,
    is_trading_day,
    next_session_open,
    session_close,
    trading_hours_between,
    trading_years_between,
    volatility_years_between,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

#: Monday 2026-07-27 10:00 ET, mid-session on a regular trading day.
#:
#: A guard that sits *behind* the trading-time check can only be reached from an
#: instant that still has trading time in front of it. Reading the wall clock
#: instead makes which guard fires depend on the day the suite happens to run:
#: from a Saturday, ``now + 1 day`` lands on a Sunday, the model correctly
#: declines for having no cash-hour-equivalents left, and the later guard under
#: test is never evaluated. The instant is pinned so the test asserts on the
#: branch it names, every day of the week.
MID_SESSION = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)


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

    @pytest.mark.parametrize(
        "text,futures",
        [
            ("Will the S&P 500 be above 7545?", "ES=F"),
            ("Will the Nasdaq-100 be above 29800?", "NQ=F"),
            ("Will the Dow Jones close above 45000?", "YM=F"),
            # VIX futures are a term-structure product, not a spot proxy.
            ("Will VIX close above 20?", None),
        ],
    )
    def test_futures_anchor_symbols(self, text: str, futures: str | None) -> None:
        assert detect_index(text)[1] == futures

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
        assert "cash-hour-equivalents" in result.detail

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
    async def test_barrier_wording_declines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No touch model is fitted for indices, so refuse rather than misprice.

        The barrier guard is the last one in ``estimate``: reaching it requires
        both trading time remaining and a usable price history. Both are supplied
        explicitly - a pinned mid-session clock and a stubbed spot/vol read -
        so that a decline for either of those reasons cannot be mistaken for the
        decline this test is about. The stub is also what keeps the suite off the
        network, as ``conftest`` promises.
        """
        model = EquityIndexThresholdModel()

        # (spot, sigma_annual, n_returns, quote_age_seconds, sigma_uncertainty)
        async def _stub_spot_and_vol(_symbol: str) -> tuple[float, float, int, float, float]:
            return (7412.0, 0.18, 250, 60.0, 0.01)

        monkeypatch.setattr(model, "_spot_and_vol", _stub_spot_and_vol)

        try:
            result = await model.estimate(
                ModelContext(
                    market=_market(
                        title="Will the S&P 500 reach 7500 this week?",
                        event_occurrence_time=MID_SESSION + timedelta(days=1),
                    ),
                    now=MID_SESSION,
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


class TestCryptoStrikeGuards:
    """Guards against pricing a market that has no usable threshold."""

    def _market(self, title: str, **overrides) -> NormalizedMarket:  # noqa: ANN003
        base = dict(
            platform=Platform.POLYMARKET,
            platform_market_id="x",
            title=title,
            category=Category.CRYPTO,
            expected_resolution_time=utcnow() + timedelta(hours=6),
        )
        base.update(overrides)
        return NormalizedMarket(**base)

    @pytest.mark.parametrize(
        "title",
        [
            "Bitcoin Up or Down - July 26, 9PM ET",
            "Bitcoin Up or Down on July 27?",
            "Will ETH close higher on Friday?",
            "Bitcoin green or red today?",
        ],
    )
    @pytest.mark.asyncio
    async def test_directional_markets_are_declined(self, title: str) -> None:
        """These settle against the opening price, not a threshold.

        Regression: 'Bitcoin Up or Down - July 26' had no strike, so the model
        scraped 26 out of the date and priced P(BTC > $26) = 1.0 against a market
        at 0.405, taking 29% of the ensemble weight.
        """
        from pmvl_markets.probability.categories.crypto import CryptoThresholdModel

        model = CryptoThresholdModel()
        try:
            result = await model.estimate(ModelContext(market=self._market(title)))
        finally:
            await model.aclose()
        assert result.probability is None
        assert "directional" in result.detail

    def test_directional_detection(self) -> None:
        from pmvl_markets.probability.categories.crypto import _DIRECTIONAL_RE

        assert _DIRECTIONAL_RE.search("Bitcoin Up or Down - July 26")
        assert _DIRECTIONAL_RE.search("Will ETH close higher?")
        assert not _DIRECTIONAL_RE.search("Will Bitcoin be above $70,000?")
        assert not _DIRECTIONAL_RE.search("Bitcoin price on Jul 27, 2026?")

    def test_text_strike_plausibility_band(self) -> None:
        """A scraped strike must be the same order of magnitude as spot."""
        from pmvl_markets.probability.categories.crypto import (
            _TEXT_STRIKE_MAX_RATIO,
            _TEXT_STRIKE_MIN_RATIO,
        )

        spot = 65052.0
        # The date-derived strike that caused the bug.
        assert not (_TEXT_STRIKE_MIN_RATIO <= 26 / spot <= _TEXT_STRIKE_MAX_RATIO)
        # A real threshold near spot passes.
        assert _TEXT_STRIKE_MIN_RATIO <= 64000 / spot <= _TEXT_STRIKE_MAX_RATIO
        assert _TEXT_STRIKE_MIN_RATIO <= 100000 / spot <= _TEXT_STRIKE_MAX_RATIO

    @pytest.mark.asyncio
    async def test_venue_strike_bypasses_the_plausibility_check(self) -> None:
        """A venue-supplied strike is authoritative even if it looks unusual."""
        from pmvl_markets.probability.categories.crypto import CryptoThresholdModel

        model = CryptoThresholdModel()
        try:
            result = await model.estimate(
                ModelContext(
                    market=self._market(
                        "Bitcoin price on Jul 27, 2026?",
                        floor_strike=Decimal("64000"),
                        strike_type="greater",
                    )
                )
            )
        finally:
            await model.aclose()
        assert result.probability is not None


class TestFuturesVolatilityTime:
    """Overnight variance accrues through futures, at a reduced rate."""

    def test_futures_session(self) -> None:
        assert not is_futures_open(et(2026, 7, 26, 17, 0))   # Sunday pre-open
        assert is_futures_open(et(2026, 7, 26, 19, 0))       # Sunday evening
        assert not is_futures_open(et(2026, 7, 25, 12, 0))   # Saturday
        assert not is_futures_open(et(2026, 7, 27, 17, 30))  # daily halt
        assert is_futures_open(et(2026, 7, 27, 10, 0))       # cash session
        assert not is_futures_open(et(2026, 7, 24, 18, 0))   # Friday after 17:00

    def test_overnight_counted_but_discounted(self) -> None:
        cash, overnight = effective_volatility_hours(
            et(2026, 7, 26, 21, 0), et(2026, 7, 27, 10, 0)
        )
        assert cash == pytest.approx(0.5)
        assert overnight == pytest.approx(12.5, abs=0.3)
        effective = cash + overnight * OVERNIGHT_VARIANCE_WEIGHT
        # Materially more than cash-only (0.5) but far less than calendar (13.0).
        assert 1.0 < effective < 3.0

    def test_weekend_gap_contributes_almost_nothing(self) -> None:
        """Saturday is closed for futures too, so a weekend is nearly dead time."""
        cash, overnight = effective_volatility_hours(
            et(2026, 7, 24, 16, 0), et(2026, 7, 27, 9, 30)
        )
        assert cash == 0.0
        # Friday 16:00-17:00 plus Sunday 18:00-09:30, not the full 65 clock hours.
        assert overnight < 20.0

    def test_one_close_to_close_day_is_one_trading_day(self) -> None:
        """The volatility clock must round-trip a sigma estimated close-to-close."""
        one_day = volatility_years_between(
            et(2026, 7, 27, 16, 0), et(2026, 7, 28, 16, 0)
        )
        assert one_day * 252 == pytest.approx(1.0, rel=1e-6)

    def test_effective_hours_constant_excludes_the_maintenance_halt(self) -> None:
        # 6.5 cash + 16.5 tradeable overnight x 0.10 (not 17.5 - the 17:00-18:00 ET
        # halt is dead time in which the index cannot move).
        assert EFFECTIVE_HOURS_PER_DAY == pytest.approx(8.15)
        assert EFFECTIVE_HOURS_PER_YEAR == pytest.approx(252 * 8.15)

    def test_overnight_is_a_minority_of_daily_variance(self) -> None:
        """Calibration target: overnight ~20% of close-to-close variance."""
        overnight_share = (16.5 * OVERNIGHT_VARIANCE_WEIGHT) / EFFECTIVE_HOURS_PER_DAY
        assert 0.15 < overnight_share < 0.25

    def test_cash_session_dominates_an_intraday_horizon(self) -> None:
        cash, overnight = effective_volatility_hours(
            et(2026, 7, 27, 10, 0), et(2026, 7, 27, 16, 0)
        )
        assert cash == pytest.approx(6.0)
        assert overnight == 0.0


class TestFuturesAnchorGuards:
    """The anchor must refuse rather than mislead."""

    def test_implausible_overnight_move_is_rejected(self) -> None:
        from pmvl_markets.probability.categories.equity import (
            _MAX_OVERNIGHT_ADJUSTMENT,
        )

        # A >8% overnight move is far more likely a bad symbol or roll artefact.
        assert 0.03 < _MAX_OVERNIGHT_ADJUSTMENT < 0.15

    def test_anchor_gap_bound_is_tight_enough(self) -> None:
        from pmvl_markets.probability.categories.equity import (
            _MAX_ANCHOR_GAP_SECONDS,
        )

        # The reference bar must be near the cash close or the return spans the
        # wrong window entirely.
        assert 0 < _MAX_ANCHOR_GAP_SECONDS <= 4 * 3600

    def test_ewma_lambda_is_the_riskmetrics_daily_value(self) -> None:
        from pmvl_markets.probability.categories.equity import _EWMA_LAMBDA

        assert _EWMA_LAMBDA == pytest.approx(0.94)
        # Effective window ~17 sessions, matched to how fast these markets settle.
        assert 10 < 1 / (1 - _EWMA_LAMBDA) < 25


class TestSiblingCompleteness:
    """A residual is only this outcome's probability on a COMPLETE outcome set."""

    def _ctx(
        self, n_siblings: int, expected: int, sibling_price: str = "0.05",
        own: str = "0.07",
    ) -> ModelContext:
        return ModelContext(
            market=NormalizedMarket(platform=Platform.POLYMARKET, platform_market_id="x"),
            sibling_outcome_prices=[
                (f"s{i}", Decimal(sibling_price)) for i in range(n_siblings)
            ],
            extra={
                "mutually_exclusive_exhaustive": True,
                "event_outcome_count": expected,
                "own_outcome_price": Decimal(own),
            },
        )

    @pytest.mark.asyncio
    async def test_partial_outcome_set_is_declined(self) -> None:
        """Regression: a Seoul temperature event priced 2 of many buckets.

        The unpriced buckets' mass was attributed entirely to this one, producing a
        residual of 0.895 against a market price of 0.001, at 26% ensemble weight.
        """
        from pmvl_markets.probability.consensus import SiblingCoherencePrior

        result = await SiblingCoherencePrior().estimate(self._ctx(2, 8))
        assert result.probability is None
        assert "outcomes priced" in result.detail

    @pytest.mark.asyncio
    async def test_incoherent_set_is_renormalised_not_residualised(self) -> None:
        """The correction belongs to the whole set, proportionally.

        The Seoul board summed to 0.627. Residualising handed the entire 0.373
        shortfall to whichever bucket was being scored, turning a 0.07 quote into
        0.44 - and would have done the same to every other bucket in turn.
        Renormalising gives 0.07 / 0.627 = 0.112.
        """
        from pmvl_markets.probability.consensus import SiblingCoherencePrior

        result = await SiblingCoherencePrior().estimate(
            self._ctx(7, 8, sibling_price="0.08", own="0.07")
        )
        assert result.probability is not None
        assert result.probability == pytest.approx(Decimal("0.112"), abs=Decimal("0.002"))
        # Far closer to the market's own 0.07 than the old 0.44.
        assert result.probability < Decimal("0.15")

    @pytest.mark.asyncio
    async def test_coherent_set_adds_nothing(self) -> None:
        """A set already summing to 1 carries no information beyond the quotes."""
        from pmvl_markets.probability.consensus import SiblingCoherencePrior

        result = await SiblingCoherencePrior().estimate(
            self._ctx(6, 7, sibling_price="0.15", own="0.10")
        )
        assert result.probability is None
        assert "coherent" in result.detail

    @pytest.mark.asyncio
    async def test_confidence_falls_as_the_set_drifts(self) -> None:
        from pmvl_markets.probability.consensus import SiblingCoherencePrior

        mild = await SiblingCoherencePrior().estimate(
            self._ctx(6, 7, sibling_price="0.16", own="0.10")
        )
        wild = await SiblingCoherencePrior().estimate(
            self._ctx(7, 8, sibling_price="0.08", own="0.07")
        )
        assert mild.confidence > wild.confidence

    @pytest.mark.asyncio
    async def test_unknown_outcome_count_is_declined(self) -> None:
        """Completeness cannot be verified, so the residual cannot be attributed."""
        from pmvl_markets.probability.consensus import SiblingCoherencePrior

        result = await SiblingCoherencePrior().estimate(self._ctx(2, 0))
        assert result.probability is None
        assert "outcome count unknown" in result.detail
