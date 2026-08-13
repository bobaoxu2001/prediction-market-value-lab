"""CPI bucket model, and the parsing of the Cleveland Fed's chart payload."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, Platform
from pmvl_shared.schemas import NormalizedMarket

from pmvl_markets.probability.base import ModelContext
from pmvl_markets.probability.categories.economics import (
    BUCKET_HALF_WIDTH,
    CpiNowcastModel,
    bucket_probability,
    parse_cpi_ticker,
)
from pmvl_markets.providers.cleveland_fed import (
    ErrorModel,
    Frequency,
    Measure,
    NowcastPoint,
    lead_bucket,
    parse_nowcast_payload,
)

UTC = timezone.utc
RELEASE = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)


# ------------------------------------------------------------------- buckets
def test_bucket_probability_is_the_rounding_interval():
    """BLS rounds to one decimal, so "exactly 0.3%" is [0.25, 0.35)."""
    centred = bucket_probability(0.3, mu=0.3, sigma=0.115)
    assert centred == pytest.approx(0.328, abs=0.01)


def test_bucket_probability_falls_away_from_the_nowcast():
    near = bucket_probability(0.2, mu=0.2, sigma=0.115)
    far = bucket_probability(0.8, mu=0.2, sigma=0.115)
    assert near > 0.3 > far
    assert far < 0.001


def test_bucket_probabilities_sum_to_one_over_an_exhaustive_board():
    """The property that makes this the right shape for these contracts.

    Kalshi's board is exhaustive, so a model that is internally coherent must put
    its whole mass across the strikes. Nothing enforces this in the code - it
    falls out of integrating one normal - which is exactly why it is worth a test.
    """
    strikes = [round(-1.0 + 0.1 * i, 1) for i in range(31)]
    total = sum(bucket_probability(s, mu=0.203, sigma=0.115) for s in strikes)
    assert total == pytest.approx(1.0, abs=0.001)


def test_a_wider_sigma_moves_mass_from_the_centre_to_the_tails():
    """The reason the model carries two sigmas rather than picking one."""
    centre_regime = bucket_probability(0.2, mu=0.2, sigma=0.115)
    centre_shock = bucket_probability(0.2, mu=0.2, sigma=0.157)
    tail_regime = bucket_probability(0.6, mu=0.2, sigma=0.115)
    tail_shock = bucket_probability(0.6, mu=0.2, sigma=0.157)

    assert centre_shock < centre_regime
    assert tail_shock > tail_regime


def test_zero_sigma_does_not_divide_by_zero():
    assert bucket_probability(0.2, mu=0.2, sigma=0.0) == 1.0
    assert bucket_probability(0.9, mu=0.2, sigma=0.0) == 0.0


# ------------------------------------------------------------ ticker parsing
def test_parse_cpi_ticker():
    parsed = parse_cpi_ticker("KXECONSTATCPICORE-26AUG-T0.3")

    assert parsed is not None
    assert parsed["series"] == "KXECONSTATCPICORE"
    assert parsed["target_month"] == date(2026, 8, 1)
    assert parsed["strike"] == 0.3


def test_parse_cpi_ticker_handles_a_negative_strike():
    """Deflation buckets exist on the board and the minus must survive parsing."""
    parsed = parse_cpi_ticker("KXECONSTATCPICORE-26AUG-T-0.2")

    assert parsed is not None
    assert parsed["strike"] == -0.2


def test_parse_cpi_ticker_rejects_other_boards():
    assert parse_cpi_ticker("KXMLBGAME-26AUG092020HOUSD-SD") is None
    assert parse_cpi_ticker("KXECONSTATCPICORE-26AUG") is None


# --------------------------------------------------- cleveland fed payload
def _payload(subcaption: str, labels: list[str], series: dict[str, list]) -> list:
    return [
        {
            "chart": {"subcaption": subcaption},
            "categories": [{"category": [{"label": x} for x in labels]}],
            "dataset": [
                {"seriesname": name, "data": [{"value": v} for v in values]}
                for name, values in series.items()
            ],
        }
    ]


def test_parse_payload_reads_nowcasts_and_the_actual():
    parsed = parse_nowcast_payload(
        _payload(
            "2026-5",
            ["05/01", "05/15", "06/05", "06/08"],
            {
                "Core CPI Inflation": ["0.212", "0.220", "0.226", ""],
                "Actual Core CPI Inflation": ["", "", "", "0.208"],
            },
        )
    )
    entry = parsed[date(2026, 5, 1)]

    assert len(entry.nowcasts[Measure.CORE_CPI]) == 3
    assert entry.actuals[Measure.CORE_CPI] == NowcastPoint(date(2026, 6, 8), 0.208)


def test_parse_payload_rolls_the_year_over_at_a_december_target():
    """Labels carry no year; a January label under a December target is next year."""
    parsed = parse_nowcast_payload(
        _payload(
            "2026-12",
            ["12/01", "01/13"],
            {"CPI Inflation": ["0.30", "0.31"]},
        )
    )
    points = parsed[date(2026, 12, 1)].nowcasts[Measure.CPI]

    assert points[0].observed_on == date(2026, 12, 1)
    assert points[1].observed_on == date(2027, 1, 13)


def test_latest_before_respects_the_cutoff():
    """What makes the model replayable: a later nowcast is not evidence."""
    parsed = parse_nowcast_payload(
        _payload(
            "2026-5",
            ["05/01", "05/15", "06/05"],
            {"Core CPI Inflation": ["0.212", "0.220", "0.226"]},
        )
    )
    entry = parsed[date(2026, 5, 1)]

    assert entry.latest_before(Measure.CORE_CPI, date(2026, 5, 20)).value == 0.220
    assert entry.latest_before(Measure.CORE_CPI, date(2026, 4, 1)) is None


def test_lead_buckets_are_contiguous():
    assert lead_bucket(0) == "0-7d"
    assert lead_bucket(7) == "0-7d"
    assert lead_bucket(8) == "8-14d"
    assert lead_bucket(45) == "31-45d"
    assert lead_bucket(400) == "46d+"


# -------------------------------------------------------------------- model
class _FakeCleveland:
    def __init__(self, nowcast=0.203, regime=0.115, full=0.157) -> None:
        self._nowcast = nowcast
        self._regime = regime
        self._full = full
        self.error_calls: list[dict] = []

    async def nowcast(self, measure, frequency, month, *, as_of):
        if self._nowcast is None:
            return None
        return NowcastPoint(observed_on=as_of - timedelta(days=1), value=self._nowcast)

    async def error_model(self, measure, frequency, *, lead_days, since=None,
                          before=None, exclude_month=None):
        self.error_calls.append(
            {"lead_days": lead_days, "since": since, "before": before,
             "exclude_month": exclude_month}
        )
        stdev = self._regime if since is not None else self._full
        return ErrorModel(
            stdev=stdev, sample_size=200, lead_bucket=lead_bucket(lead_days),
            mean_error=0.001,
        )

    async def aclose(self):
        return None


def make_market(ticker: str = "KXECONSTATCPICORE-26AUG-T0.2") -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id=ticker,
        series_ticker="KXECONSTATCPICORE",
        title="CPI core month-over-month in Aug 2026?",
        subtitle="Exactly 0.2%",
        category=Category.ECONOMICS,
        open_time=RELEASE - timedelta(days=90),
        close_time=RELEASE,
        expected_resolution_time=RELEASE,
    )


def estimate(model, market=None, **kw):
    ctx = ModelContext(market=market or make_market(), **kw)
    return asyncio.run(model.estimate(ctx))


def test_model_prices_the_bucket_at_the_nowcast():
    model = CpiNowcastModel(_FakeCleveland())
    result = estimate(model, now=datetime(2026, 8, 7, tzinfo=UTC))

    assert result.probability is not None
    assert float(result.probability) == pytest.approx(0.33, abs=0.02)


def test_model_declines_a_board_with_no_nowcast_behind_it():
    model = CpiNowcastModel(_FakeCleveland())
    result = estimate(
        model,
        market=make_market("KXECONSTATU3-26AUG-T4.2"),
        now=datetime(2026, 8, 7, tzinfo=UTC),
    )

    assert result.probability is None
    assert "no nowcast series backs" in result.detail


def test_model_declines_a_non_cpi_ticker():
    model = CpiNowcastModel(_FakeCleveland())
    result = estimate(
        model,
        market=make_market("KXMLBGAME-26AUG092020HOUSD-SD"),
        now=datetime(2026, 8, 7, tzinfo=UTC),
    )

    assert result.probability is None
    assert "not a recognised CPI bucket ticker" in result.detail


def test_model_declines_rather_than_assuming_a_dispersion():
    class _NoErrors(_FakeCleveland):
        async def error_model(self, *a, **kw):
            return None

    result = estimate(CpiNowcastModel(_NoErrors()), now=datetime(2026, 8, 7, tzinfo=UTC))

    assert result.probability is None
    assert "refusing to assume one" in result.detail


def test_interval_comes_from_the_gap_between_the_two_sigmas():
    tight = CpiNowcastModel(_FakeCleveland(regime=0.115, full=0.157))
    identical = CpiNowcastModel(_FakeCleveland(regime=0.115, full=0.115))

    spread = estimate(tight, now=datetime(2026, 8, 7, tzinfo=UTC)).stdev
    none_at_all = estimate(identical, now=datetime(2026, 8, 7, tzinfo=UTC)).stdev

    # Same sigma both ways leaves only the floor; a regime gap widens it.
    assert spread > none_at_all


def test_error_history_is_cut_at_the_evaluation_instant():
    """Fitting the dispersion on later months would be a subtler look-ahead.

    Not the answer itself, but the shape of the distribution would already know
    about the regime it is being asked to forecast in.
    """
    fake = _FakeCleveland()
    model = CpiNowcastModel(fake)
    as_of = datetime(2026, 8, 7, tzinfo=UTC)

    estimate(model, as_of=as_of)

    assert fake.error_calls
    for call in fake.error_calls:
        assert call["before"] == as_of.date()
        assert call["exclude_month"] == date(2026, 8, 1)


def test_the_forecast_month_is_excluded_from_its_own_error_fit():
    fake = _FakeCleveland()
    estimate(CpiNowcastModel(fake), now=datetime(2026, 8, 7, tzinfo=UTC))

    assert all(c["exclude_month"] == date(2026, 8, 1) for c in fake.error_calls)


def test_one_call_uses_the_regime_window_and_one_uses_all_history():
    fake = _FakeCleveland()
    estimate(CpiNowcastModel(fake), now=datetime(2026, 8, 7, tzinfo=UTC))

    windows = [c["since"] for c in fake.error_calls]
    assert None in windows, "no full-history fit was requested"
    assert any(w is not None for w in windows), "no regime-window fit was requested"


def test_confidence_falls_with_lead_time():
    near = make_market()
    far = NormalizedMarket(
        **{**make_market().model_dump(), "expected_resolution_time": RELEASE,
           "close_time": RELEASE}
    )
    model = CpiNowcastModel(_FakeCleveland())

    close_in = estimate(model, market=near, now=RELEASE - timedelta(days=3))
    far_out = estimate(model, market=far, now=RELEASE - timedelta(days=40))

    assert close_in.confidence > far_out.confidence


def test_confidence_never_exceeds_the_declared_ceiling():
    model = CpiNowcastModel(_FakeCleveland())
    result = estimate(model, now=RELEASE - timedelta(days=1))

    assert result.confidence <= CpiNowcastModel.max_confidence


def test_estimate_reports_both_sigmas_for_the_reader():
    model = CpiNowcastModel(_FakeCleveland())
    result = estimate(model, now=datetime(2026, 8, 7, tzinfo=UTC))

    assert result.data["sigma_regime"]["stdev"] == 0.115
    assert result.data["sigma_full_history"]["stdev"] == 0.157
    assert result.data["bucket"] == "[0.15, 0.25)"
    assert BUCKET_HALF_WIDTH == 0.05
