"""Historical retrodiction: what would the models have said, and were they right?

This answers one question the snapshot backtest structurally cannot answer yet:
**does the independent estimate beat the market's own price?** The snapshot backtest
reads only ``recommendation_snapshots``, so it can only score days the platform has
actually run. On a platform that has not run long enough to settle anything, it has
nothing to score, and "we do not know" is where it stops.

Retrodiction takes the other route. It picks markets that have *already settled*,
rewinds each one to an instant while it was still trading, asks the models what they
think using only data published before that instant, and compares the answer both to
the outcome and to what the market itself was charging at that moment.

## Why this is kept separate from the backtest, permanently

The snapshot backtest's central property is that look-ahead is *structurally
impossible*: it reads a frozen artefact written before the outcome existed. This
harness does not have that property and cannot be given it. It reaches backwards
through live endpoints, and its correctness rests on defences that can be wrong:

* :func:`~.rewind.rewind_market`, which strips the outcome off the market record;
* ``ProbabilityModel.supports_as_of``, a per-model claim that a human made;
* :func:`~.rewind.assert_no_outcome_leak`, the assertion that catches the first two
  failing;
* the price-history cutoff below, which drops any quote at or after the instant.

Defences that can be wrong are a different category of evidence from a property that
holds by construction, and the two must never be added together into one headline
number. So this writes its own result type, carries its own provenance label, and is
reported on its own page under its own caveats. A reader must be able to tell which
of the two produced any figure they are looking at.

## What the comparison is, exactly

For each sampled instant the harness records three numbers:

``model_probability``
    The ensemble's **independent** estimate - the one that never saw the target
    price. Scoring the market-informed estimate would be scoring the market.
``market_probability``
    The contract's own mid at that instant, from the venue's candles. Candles are
    OHLC summaries, not executable quotes, so this is graded ``DataQuality.CANDLE``
    and is fine for a *forecast accuracy* comparison and unfit for a P&L claim.
``outcome_value``
    1, 0, or 0.5, from the settlement record.

Brier improvement is ``market_brier - model_brier``: positive means the model added
information the price did not already contain. That single number is the whole point
of the exercise, and it is entirely capable of coming out negative.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, Sequence

from pmvl_shared.enums import Category, DataQuality, Platform
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ZERO, clamp_prob
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import ensure_utc, iso

from ..backtest.metrics import brier_score, calibration_curve, log_loss
from ..probability.base import ModelContext
from ..probability.ensemble import ProbabilityEnsemble
from .rewind import RewindError, assert_no_outcome_leak, rewind_market

log = get_logger(__name__)

#: How long before expected resolution each forecast is made.
#:
#: Several lead times rather than one because forecast skill is a function of
#: horizon, and a single lead time invites the flattering choice. A model that only
#: beats the market six hours out, when the price has already converged, is a
#: different and much weaker claim than one that beats it a week out.
DEFAULT_LEAD_TIMES: tuple[timedelta, ...] = (
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(days=3),
    timedelta(days=7),
)

#: A candle this far from the requested instant is not a quote *at* that instant.
#: Widening this would quietly let a stale price stand in for a missing one.
MAX_QUOTE_DISTANCE = timedelta(hours=2)

#: Provenance label written onto every row this harness produces. Distinct from
#: `live` and `demo` so no query can mix retrodicted forecasts into either.
RETRODICTION_PROVENANCE = "retrodiction"


class PriceHistorySource(Protocol):
    """Where the market's own past prices come from.

    A protocol rather than a concrete provider so the harness is testable without
    network access, in keeping with the rest of the suite.
    """

    async def price_at(
        self, market: NormalizedMarket, instant: datetime
    ) -> Decimal | None:
        """The market's implied YES probability at ``instant``, or None."""
        ...


@dataclass(frozen=True)
class SettledMarket:
    """A market with a known outcome, and the input to one evaluation."""

    market: NormalizedMarket
    #: 1 for YES, 0 for NO, 0.5 for a split resolution.
    yes_payout: Decimal
    settled_at: datetime | None = None


@dataclass
class Forecast:
    """One (market, instant) evaluation.

    Field names match :class:`~..backtest.metrics.Scoreable` so the shared Brier and
    reliability implementations apply unchanged.
    """

    predicted_probability: Decimal
    market_probability: Decimal | None
    outcome_value: Decimal

    platform: Platform
    platform_market_id: str
    category: Category
    as_of: datetime
    lead_time_hours: float
    #: Which components backed the independent estimate at that instant.
    independent_components: tuple[str, ...] = ()
    model_confidence: Decimal = ZERO
    #: Always CANDLE for now: the market price comes from OHLC bars.
    market_price_quality: DataQuality = DataQuality.CANDLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "platform_market_id": self.platform_market_id,
            "category": self.category.value,
            "as_of": iso(self.as_of),
            "lead_time_hours": round(self.lead_time_hours, 2),
            "model_probability": str(self.predicted_probability),
            "market_probability": (
                str(self.market_probability) if self.market_probability is not None else None
            ),
            "outcome_value": str(self.outcome_value),
            "independent_components": list(self.independent_components),
            "model_confidence": str(self.model_confidence),
            "market_price_quality": self.market_price_quality.value,
        }


@dataclass
class SkipReason:
    """Why a candidate produced no forecast.

    Kept and reported rather than discarded. The skip counts are how a reader tells
    "the model beat the market on 9 of 12 contracts" from "the model declined 400
    contracts and beat the market on 9 of the 12 it liked", which are very different
    claims and produce the same headline number.
    """

    reason: str
    count: int = 0


@dataclass
class RetrodictionResult:
    forecasts: list[Forecast] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=dict)
    markets_considered: int = 0
    lead_times_hours: tuple[float, ...] = ()
    generated_at: datetime | None = None

    def metrics(self) -> dict[str, Any]:
        scored = [f for f in self.forecasts if f.market_probability is not None]
        model_brier = brier_score(scored)
        market_brier = brier_score(scored, use_market=True)
        improvement = (
            market_brier - model_brier
            if model_brier is not None and market_brier is not None
            else None
        )
        return {
            "provenance": RETRODICTION_PROVENANCE,
            "n_forecasts": len(self.forecasts),
            "n_scored_against_market": len(scored),
            "markets_considered": self.markets_considered,
            "brier_model": _round(model_brier),
            "brier_market": _round(market_brier),
            "brier_improvement_vs_market": _round(improvement),
            "beats_market": (improvement > 0) if improvement is not None else None,
            "log_loss_model": _round(log_loss(scored)),
            "log_loss_market": _round(log_loss(scored, use_market=True)),
            "reliability": calibration_curve(scored),
            "skips": dict(sorted(self.skips.items(), key=lambda kv: -kv[1])),
            "lead_times_hours": list(self.lead_times_hours),
            "market_price_source": "venue candles (OHLC), not executable quotes",
            "generated_at": iso(self.generated_at),
        }

    def by_category(self) -> dict[str, dict[str, Any]]:
        """Per-category Brier, because one aggregate hides a category that is lost.

        A strong crypto model and a sports model that is worse than a coin flip pool
        into a respectable-looking average. Splitting them is the only way the
        sports result is visible.
        """
        out: dict[str, dict[str, Any]] = {}
        for category in sorted({f.category for f in self.forecasts}, key=lambda c: c.value):
            subset = [
                f
                for f in self.forecasts
                if f.category is category and f.market_probability is not None
            ]
            if not subset:
                continue
            model_b = brier_score(subset)
            market_b = brier_score(subset, use_market=True)
            out[category.value] = {
                "n": len(subset),
                "brier_model": _round(model_b),
                "brier_market": _round(market_b),
                "brier_improvement_vs_market": _round(
                    market_b - model_b
                    if model_b is not None and market_b is not None
                    else None
                ),
            }
        return out

    def by_lead_time(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for lead in sorted({f.lead_time_hours for f in self.forecasts}):
            subset = [
                f
                for f in self.forecasts
                if f.lead_time_hours == lead and f.market_probability is not None
            ]
            if not subset:
                continue
            model_b = brier_score(subset)
            market_b = brier_score(subset, use_market=True)
            out[f"{lead:g}h"] = {
                "n": len(subset),
                "brier_model": _round(model_b),
                "brier_market": _round(market_b),
                "brier_improvement_vs_market": _round(
                    market_b - model_b
                    if model_b is not None and market_b is not None
                    else None
                ),
            }
        return out

    def as_report(self) -> dict[str, Any]:
        return {
            **self.metrics(),
            "by_category": self.by_category(),
            "by_lead_time": self.by_lead_time(),
        }


def _round(value: float | None, digits: int = 5) -> float | None:
    return round(value, digits) if value is not None else None


class RetrodictionHarness:
    """Replays the ensemble against settled markets at past instants."""

    def __init__(
        self,
        history: PriceHistorySource,
        *,
        ensemble: ProbabilityEnsemble | None = None,
        lead_times: Sequence[timedelta] = DEFAULT_LEAD_TIMES,
        max_concurrency: int = 4,
    ) -> None:
        self._history = history
        self._ensemble = ensemble or ProbabilityEnsemble()
        self._lead_times = tuple(lead_times)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def aclose(self) -> None:
        await self._ensemble.aclose()

    async def run(self, settled: Sequence[SettledMarket]) -> RetrodictionResult:
        result = RetrodictionResult(
            markets_considered=len(settled),
            lead_times_hours=tuple(
                lt.total_seconds() / 3600 for lt in self._lead_times
            ),
        )

        jobs = [
            self._evaluate(item, lead)
            for item in settled
            for lead in self._lead_times
        ]
        for outcome in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(outcome, Exception):
                # A crash is a skip with a name, never a silently dropped row.
                _count(result.skips, f"error:{type(outcome).__name__}")
                log.warning("retrodiction evaluation raised: %s", outcome)
            elif isinstance(outcome, Forecast):
                result.forecasts.append(outcome)
            elif isinstance(outcome, SkipReason):
                _count(result.skips, outcome.reason)

        result.generated_at = ensure_utc(datetime.now().astimezone())
        return result

    async def _evaluate(
        self, item: SettledMarket, lead: timedelta
    ) -> Forecast | SkipReason:
        async with self._semaphore:
            return await self._evaluate_inner(item, lead)

    async def _evaluate_inner(
        self, item: SettledMarket, lead: timedelta
    ) -> Forecast | SkipReason:
        market = item.market
        resolution = ensure_utc(market.expected_resolution_time or market.close_time)
        if resolution is None:
            return SkipReason("market has no resolution time to measure a lead from")

        as_of = resolution - lead

        try:
            rewound = rewind_market(market, as_of=as_of)
        except RewindError as exc:
            return SkipReason(f"outside trading window: {exc.args[0].split(';')[0]}")

        assert_no_outcome_leak(rewound.market)

        # The market's own price at that instant. Fetched before the model runs so a
        # missing price is a cheap skip rather than a wasted ensemble pass.
        market_price = await self._history.price_at(rewound.market, as_of)
        if market_price is None:
            return SkipReason("no venue price history at the evaluation instant")

        ctx = ModelContext(
            market=rewound.market,
            # Deliberately empty. Cross-venue quotes, sibling prices and research
            # evidence are all *current* data; there is no historical source for
            # them here. Passing today's values into a past instant would be the
            # same leak the rewind exists to prevent, and these components are
            # market-informed anyway, so they could not contribute to the
            # independent estimate that is being scored.
            target_book=None,
            cross_platform_quotes={},
            related_market_prices=[],
            sibling_outcome_prices=[],
            evidence=[],
            now=as_of,
            as_of=as_of,
        )

        output = await self._ensemble.estimate(ctx)
        fair = output.fair

        if not fair.has_independent_prior or fair.independent_probability is None:
            return SkipReason("no independent estimate at that instant")

        independence = fair.independence or {}
        components = tuple(independence.get("independent_components") or ())

        return Forecast(
            predicted_probability=clamp_prob(fair.independent_probability),
            market_probability=clamp_prob(market_price),
            outcome_value=item.yes_payout,
            platform=market.platform,
            platform_market_id=market.platform_market_id,
            category=market.category,
            as_of=as_of,
            lead_time_hours=lead.total_seconds() / 3600,
            independent_components=components,
            model_confidence=fair.model_confidence,
        )


def _count(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


class ProviderPriceHistory:
    """:class:`PriceHistorySource` backed by the venues' candlestick endpoints.

    Candles are cached per market because the four default lead times all fall
    inside one request's window, and fetching the same series four times would
    quadruple the load on an endpoint neither venue promises to keep fast.
    """

    def __init__(self, providers: dict[Platform, Any]) -> None:
        self._providers = providers
        self._cache: dict[tuple[str, str], list[tuple[datetime, Decimal]]] = {}

    async def price_at(
        self, market: NormalizedMarket, instant: datetime
    ) -> Decimal | None:
        series = await self._series(market)
        if not series:
            return None

        # Strictly at or before the instant. `<=` and not `<` because a candle
        # stamped exactly at the instant closed at it, and nothing in it postdates
        # the forecast.
        instant = ensure_utc(instant)
        prior = [(ts, price) for ts, price in series if ts <= instant]
        if not prior:
            return None
        ts, price = max(prior, key=lambda row: row[0])
        if instant - ts > MAX_QUOTE_DISTANCE:
            return None
        if not (ZERO < price < D(1)):
            # A candle at exactly 0 or 1 is a settled or degenerate bar, not a
            # forecast anyone could have traded against.
            return None
        return price

    async def _series(
        self, market: NormalizedMarket
    ) -> list[tuple[datetime, Decimal]]:
        key = (market.platform.value, market.platform_market_id)
        if key in self._cache:
            return self._cache[key]

        provider = self._providers.get(market.platform)
        resolution = ensure_utc(market.expected_resolution_time or market.close_time)
        if provider is None or resolution is None:
            self._cache[key] = []
            return []

        longest = max(DEFAULT_LEAD_TIMES)
        points = await provider.get_price_history(
            market,
            start=resolution - longest - MAX_QUOTE_DISTANCE,
            end=resolution,
            interval_minutes=60,
        )
        series = [
            (ensure_utc(p.timestamp), p.price)
            for p in points
            if p.timestamp is not None and p.price is not None
        ]
        series = [(ts, price) for ts, price in series if ts is not None]
        series.sort(key=lambda row: row[0])
        self._cache[key] = series
        return series
