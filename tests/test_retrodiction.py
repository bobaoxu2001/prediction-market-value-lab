"""Retrodiction harness, and above all its look-ahead defences.

The harness reaches backwards through live endpoints, so unlike the snapshot
backtest it has no structural guarantee against look-ahead - only defences. These
tests are those defences' only proof. A retrodiction result that beats the market is
a claim about the product; if it is produced by a leak, every one of these tests is
what should have failed first.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, DataQuality, MarketStatus, Platform
from pmvl_shared.schemas import NormalizedMarket

from pmvl_markets.probability.base import (
    ModelContext,
    ModelEstimate,
    ProbabilityModel,
    lookahead_guard,
    no_opinion,
)
from pmvl_markets.probability.ensemble import ProbabilityEnsemble
from pmvl_markets.retrodiction import (
    RetrodictionResult,
    Forecast,
    RetrodictionHarness,
    RewindError,
    SettledMarket,
    assert_no_outcome_leak,
    rewind_market,
)

UTC = timezone.utc
RESOLUTION = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_market(**overrides) -> NormalizedMarket:
    base = dict(
        platform=Platform.KALSHI,
        platform_market_id="TEST-MKT",
        title="Will BTC be above $70,000 on June 1?",
        category=Category.CRYPTO,
        open_time=RESOLUTION - timedelta(days=30),
        close_time=RESOLUTION,
        expected_resolution_time=RESOLUTION,
        status=MarketStatus.SETTLED,
        result="yes",
        actual_settlement_time=RESOLUTION + timedelta(hours=1),
        best_yes_bid=Decimal("1.00"),
        best_yes_ask=Decimal("1.00"),
        last_trade_price=Decimal("1.00"),
        volume_24h=Decimal("50000"),
    )
    base.update(overrides)
    return NormalizedMarket(**base)


# --------------------------------------------------------------------- rewind
def test_rewind_strips_the_outcome_off_the_market_record():
    """`result` is the answer. Nothing downstream may ever see it."""
    rewound = rewind_market(make_market(), as_of=RESOLUTION - timedelta(days=1))

    assert rewound.market.result is None
    assert rewound.market.actual_settlement_time is None
    assert rewound.market.status is MarketStatus.OPEN
    assert "result" in rewound.cleared_fields
    # And the assertion that backs it up agrees.
    assert_no_outcome_leak(rewound.market)


def test_rewind_clears_terminal_quotes():
    """A settled market's quotes are all 1.00 or 0.00 - the outcome, restated."""
    rewound = rewind_market(make_market(), as_of=RESOLUTION - timedelta(days=1))

    assert rewound.market.best_yes_bid is None
    assert rewound.market.best_yes_ask is None
    assert rewound.market.last_trade_price is None
    assert rewound.market.volume_24h is None


def test_rewind_preserves_what_the_models_actually_need():
    rewound = rewind_market(make_market(), as_of=RESOLUTION - timedelta(days=1))

    assert rewound.market.title.startswith("Will BTC")
    assert rewound.market.category is Category.CRYPTO
    assert rewound.market.expected_resolution_time == RESOLUTION


def test_rewind_refuses_an_instant_after_the_close():
    with pytest.raises(RewindError, match="at or after the market's close"):
        rewind_market(make_market(), as_of=RESOLUTION + timedelta(hours=1))


def test_rewind_refuses_an_instant_before_the_open():
    with pytest.raises(RewindError, match="precedes the market's open"):
        rewind_market(make_market(), as_of=RESOLUTION - timedelta(days=90))


def test_rewind_handles_naive_datetimes_from_sqlite():
    """SQLite drops tzinfo, so a market loaded from the database is naive.

    On the first live run this raised `can't compare offset-naive and offset-aware
    datetimes` for every single market, and the harness dutifully reported 36 skips
    and no forecasts. The skip reporting worked; the comparison did not.
    """
    naive = make_market(
        open_time=(RESOLUTION - timedelta(days=30)).replace(tzinfo=None),
        close_time=RESOLUTION.replace(tzinfo=None),
        expected_resolution_time=RESOLUTION.replace(tzinfo=None),
    )
    rewound = rewind_market(naive, as_of=RESOLUTION - timedelta(days=1))

    assert rewound.market.result is None


def test_harness_handles_naive_resolution_times():
    naive = make_market(
        open_time=(RESOLUTION - timedelta(days=30)).replace(tzinfo=None),
        close_time=RESOLUTION.replace(tzinfo=None),
        expected_resolution_time=RESOLUTION.replace(tzinfo=None),
    )
    result = run_harness(
        [_ReplayableModel("0.80")],
        _FakeHistory(Decimal("0.50")),
        settled=[SettledMarket(market=naive, yes_payout=Decimal("1"))],
    )

    assert len(result.forecasts) == 1
    assert not any(k.startswith("error:") for k in result.skips)


def test_assert_no_outcome_leak_catches_a_hand_built_leak():
    """The backstop for someone adding `result` to the preserved list."""
    leaked = make_market(status=MarketStatus.OPEN, actual_settlement_time=None)
    with pytest.raises(RewindError, match="outcome-revealing field"):
        assert_no_outcome_leak(leaked)


# ------------------------------------------------------------ lookahead guard
class _UnauditedModel(ProbabilityModel):
    """A model that has not declared itself replayable. Must be refused."""

    name = "unaudited"
    supports_as_of = False

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        if (declined := lookahead_guard(self, ctx)) is not None:
            return declined
        return ModelEstimate(probability=Decimal("0.99"), confidence=Decimal("0.9"))


class _ReplayableModel(ProbabilityModel):
    name = "crypto_gbm_threshold"  # a name declared INDEPENDENT in the registry
    supports_as_of = True

    def __init__(self, probability: str = "0.60") -> None:
        self._probability = Decimal(probability)

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        if (declined := lookahead_guard(self, ctx)) is not None:
            return declined
        return ModelEstimate(
            probability=self._probability,
            confidence=Decimal("0.5"),
            stdev=Decimal("0.1"),
            independent=True,
        )


def test_lookahead_guard_declines_an_unaudited_model_under_as_of():
    ctx = ModelContext(market=make_market(), as_of=RESOLUTION - timedelta(days=1))
    result = asyncio.run(_UnauditedModel().estimate(ctx))

    assert result.probability is None
    assert "cannot reconstruct its inputs" in result.detail


def test_lookahead_guard_is_inert_without_as_of():
    """The guard must not disturb the live path."""
    ctx = ModelContext(market=make_market(), now=RESOLUTION - timedelta(days=1))
    result = asyncio.run(_UnauditedModel().estimate(ctx))

    assert result.probability == Decimal("0.99")


def test_supports_as_of_defaults_to_false():
    """A new model is presumed unsafe to replay until someone audits it."""
    assert ProbabilityModel.supports_as_of is False


def test_every_live_ensemble_model_declaring_as_of_support_is_deliberate():
    """Pins the audited set, so adding to it requires editing this test.

    `supports_as_of` is a claim a human made about a data path. Letting it be
    granted in a drive-by edit is how the one defence that cannot be checked
    mechanically gets weakened by accident.
    """
    audited = {m.name for m in ProbabilityEnsemble()._models if m.supports_as_of}
    assert audited == {
        # Windowed Coinbase candle requests ending at the instant; ticker never used
        # on the historical path.
        "crypto_gbm_threshold",
        # Records counted from completed games strictly before the instant; the
        # scoreboard is used for identification only and cannot return a result.
        "sports_base_rate",
        # Nowcasts are dated, and the error history is cut at the same instant so
        # the dispersion cannot know about the regime it is forecasting in.
        "cpi_nowcast_bucket",
    }


# -------------------------------------------------------------------- harness
class _FakeHistory:
    def __init__(self, price: Decimal | None) -> None:
        self.price = price
        self.calls: list[datetime] = []

    async def price_at(self, market, instant):
        self.calls.append(instant)
        return self.price


def run_harness(models, history, **kwargs) -> "object":
    ensemble = ProbabilityEnsemble(models=models)
    harness = RetrodictionHarness(
        history,
        ensemble=ensemble,
        lead_times=kwargs.pop("lead_times", (timedelta(hours=24),)),
    )
    settled = kwargs.pop(
        "settled",
        [SettledMarket(market=make_market(), yes_payout=Decimal("1"))],
    )
    return asyncio.run(harness.run(settled))


def test_harness_scores_model_and_market_against_the_outcome():
    result = run_harness([_ReplayableModel("0.80")], _FakeHistory(Decimal("0.50")))

    assert len(result.forecasts) == 1
    forecast = result.forecasts[0]
    assert forecast.predicted_probability == Decimal("0.80")
    assert forecast.market_probability == Decimal("0.50")
    assert forecast.outcome_value == Decimal("1")

    metrics = result.metrics()
    # Model said 0.80, market said 0.50, YES happened: the model was closer.
    assert metrics["brier_model"] == pytest.approx(0.04)
    assert metrics["brier_market"] == pytest.approx(0.25)
    assert metrics["brier_improvement_vs_market"] == pytest.approx(0.21)
    assert metrics["beats_market"] is True


def test_harness_reports_a_losing_model_as_losing():
    """The number is entirely capable of being negative, and must say so."""
    result = run_harness([_ReplayableModel("0.20")], _FakeHistory(Decimal("0.50")))

    metrics = result.metrics()
    assert metrics["brier_improvement_vs_market"] < 0
    assert metrics["beats_market"] is False


def test_harness_skips_when_no_model_can_replay():
    result = run_harness([_UnauditedModel()], _FakeHistory(Decimal("0.50")))

    assert result.forecasts == []
    assert result.skips == {"no independent estimate at that instant": 1}


def test_harness_skips_when_the_venue_has_no_price_at_that_instant():
    result = run_harness([_ReplayableModel()], _FakeHistory(None))

    assert result.forecasts == []
    assert "no venue price history at the evaluation instant" in result.skips


def test_harness_evaluates_at_the_requested_lead_time():
    history = _FakeHistory(Decimal("0.50"))
    run_harness(
        [_ReplayableModel()],
        history,
        lead_times=(timedelta(hours=6), timedelta(days=7)),
    )

    assert set(history.calls) == {
        RESOLUTION - timedelta(hours=6),
        RESOLUTION - timedelta(days=7),
    }


def test_harness_never_hands_a_model_the_outcome():
    """End-to-end version of the rewind tests: what does the model actually see?"""
    seen: list[NormalizedMarket] = []

    class _Spy(_ReplayableModel):
        async def estimate(self, ctx: ModelContext) -> ModelEstimate:
            seen.append(ctx.market)
            return await super().estimate(ctx)

    run_harness([_Spy()], _FakeHistory(Decimal("0.50")))

    assert seen, "the model was never called"
    for market in seen:
        assert market.result is None
        assert market.status is not MarketStatus.SETTLED
        assert market.last_trade_price is None


def test_harness_passes_as_of_and_not_just_now():
    """`now` alone would let an unaudited model answer with present-day data."""
    seen: list[ModelContext] = []

    class _Spy(_ReplayableModel):
        async def estimate(self, ctx: ModelContext) -> ModelEstimate:
            seen.append(ctx)
            return await super().estimate(ctx)

    run_harness([_Spy()], _FakeHistory(Decimal("0.50")))

    assert seen[0].as_of == RESOLUTION - timedelta(hours=24)
    assert seen[0].is_retrodiction is True


def test_harness_withholds_current_cross_venue_and_research_context():
    seen: list[ModelContext] = []

    class _Spy(_ReplayableModel):
        async def estimate(self, ctx: ModelContext) -> ModelEstimate:
            seen.append(ctx)
            return await super().estimate(ctx)

    run_harness([_Spy()], _FakeHistory(Decimal("0.50")))

    ctx = seen[0]
    assert ctx.cross_platform_quotes == {}
    assert ctx.sibling_outcome_prices == []
    assert ctx.evidence == []
    assert ctx.target_book is None


def test_result_splits_by_category_so_a_lost_category_is_visible():
    crypto = SettledMarket(market=make_market(), yes_payout=Decimal("1"))
    sports = SettledMarket(
        market=make_market(
            platform_market_id="SPORT-1",
            category=Category.SPORTS,
            title="Will the home team win?",
        ),
        yes_payout=Decimal("0"),
    )
    result = run_harness(
        [_ReplayableModel("0.80")],
        _FakeHistory(Decimal("0.50")),
        settled=[crypto, sports],
    )

    by_category = result.by_category()
    assert set(by_category) == {"crypto", "sports"}
    # Same model, opposite outcomes: crypto beat the market, sports lost to it.
    assert by_category["crypto"]["brier_improvement_vs_market"] > 0
    assert by_category["sports"]["brier_improvement_vs_market"] < 0


def test_report_labels_its_provenance_and_price_quality():
    """A reader must never mistake this for the snapshot backtest."""
    result = run_harness([_ReplayableModel()], _FakeHistory(Decimal("0.50")))
    report = result.as_report()

    assert report["provenance"] == "retrodiction"
    assert "not executable quotes" in report["market_price_source"]
    assert result.forecasts[0].market_price_quality is DataQuality.CANDLE


# ------------------------------------------------------- segment searching
def _forecast(model_p: str, market_p: str, outcome: str, **kw) -> Forecast:
    defaults = dict(
        platform=Platform.KALSHI,
        platform_market_id="M",
        category=Category.CRYPTO,
        as_of=RESOLUTION - timedelta(hours=24),
        lead_time_hours=24.0,
        model_confidence=Decimal("0.5"),
    )
    defaults.update(kw)
    return Forecast(
        predicted_probability=Decimal(model_p),
        market_probability=Decimal(market_p),
        outcome_value=Decimal(outcome),
        **defaults,
    )


def test_segment_metrics_reports_a_paired_standard_error():
    """Both forecasts are scored against the same outcome, so the error is paired."""
    from pmvl_markets.retrodiction.harness import segment_metrics

    rows = [_forecast("0.8", "0.5", "1") for _ in range(10)]
    block = segment_metrics(rows)

    assert block["n"] == 10
    assert block["brier_improvement_vs_market"] == pytest.approx(0.21)
    # Identical differences means zero variance, so nothing is distinguishable.
    assert block["improvement_standard_error"] == pytest.approx(0.0)


def test_segment_metrics_declines_a_verdict_on_one_observation():
    from pmvl_markets.retrodiction.harness import segment_metrics

    block = segment_metrics([_forecast("0.8", "0.5", "1")])

    assert block["t_statistic"] is None
    assert block["distinguishable_from_zero"] is None


def test_segment_search_counts_what_it_searched():
    """A winner found by searching needs the search size next to it."""
    result = run_harness([_ReplayableModel("0.80")], _FakeHistory(Decimal("0.50")))
    search = result.as_report()["segment_search"]

    assert search["segments_examined"] > 0
    assert search["expected_false_positives_at_t2"] == pytest.approx(
        search["segments_examined"] * 0.05
    )
    assert "not pre-registered" in search["note"]


def test_disagreement_split_separates_agreement_from_departure():
    """The split a recommendation surface rests on: is the model right when it
    departs from the price, or only when it agrees with it?"""
    result = RetrodictionResult(
        forecasts=[
            _forecast("0.51", "0.50", "1"),
            _forecast("0.80", "0.50", "1"),
        ]
    )
    bands = result.by_disagreement()

    assert set(bands) == {"<2pp", "20pp+"}
    assert bands["<2pp"]["n"] == 1
    assert bands["20pp+"]["n"] == 1


def test_market_price_split_separates_longshots_from_favourites():
    result = RetrodictionResult(
        forecasts=[
            _forecast("0.03", "0.02", "0"),
            _forecast("0.50", "0.50", "1"),
            _forecast("0.98", "0.97", "1"),
        ]
    )
    bands = result.by_market_price()

    assert set(bands) == {"0-5c", "35-65c", "95c+"}


def test_skips_are_reported_alongside_the_score():
    """9 wins out of 12 tried means nothing without the 400 that were declined."""
    good = SettledMarket(market=make_market(), yes_payout=Decimal("1"))
    unusable = SettledMarket(
        market=make_market(
            platform_market_id="NO-RESOLUTION",
            close_time=None,
            expected_resolution_time=None,
        ),
        yes_payout=Decimal("1"),
    )
    result = run_harness(
        [_ReplayableModel()], _FakeHistory(Decimal("0.50")), settled=[good, unusable]
    )

    assert result.markets_considered == 2
    assert len(result.forecasts) == 1
    assert sum(result.skips.values()) == 1
    assert result.metrics()["skips"]
