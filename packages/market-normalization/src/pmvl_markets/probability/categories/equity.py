"""Equity index threshold model.

Same driftless-GBM machinery as the crypto model, with one decisive difference:
**time is measured in trading hours, not calendar hours.** See
:mod:`pmvl_markets.probability.trading_calendar` for why that matters so much - on the
overnight index markets Kalshi runs, calendar time overstates ``sigma*sqrt(tau)`` by
around 5x, which is more than enough to invent a large edge on a correctly-priced
out-of-the-money strike.

    P(S_T > K) = Phi( [ln(S_0/K) - sigma^2 * tau / 2] / (sigma * sqrt(tau)) )

Spot and history come from Yahoo Finance's public chart endpoint, which needs no
credentials. Kalshi settles these on the official published index level, and the
Yahoo series tracks the same index, so the input is the settlement quantity itself.

Model risk deliberately surfaced
--------------------------------
* Realised volatility from daily closes is backward-looking and understates
  event-driven moves (FOMC, CPI, earnings clusters). Confidence is capped.
* Close-to-close realised vol excludes overnight gaps *within* the sample but the
  markets being priced frequently span an overnight gap. This biases the estimate
  low for overnight horizons, so an overnight-gap premium is applied.
* Yahoo's ``regularMarketPrice`` is delayed outside regular hours and is the last
  regular-session print, which is the correct anchor for a driftless model but means
  the estimate ignores after-hours futures moves. Data freshness is reported so the
  ranking layer can discount it.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, clamp_prob, quantize_prob
from pmvl_shared.timeutil import ensure_utc, utcnow

from ...providers.http import HttpClient, ProviderError
from ..base import ModelContext, ModelEstimate, ProbabilityModel, no_opinion
from ..trading_calendar import (
    TRADING_HOURS_PER_YEAR,
    is_market_open,
    trading_hours_between,
)
from .crypto import is_barrier_market, normal_cdf

log = get_logger(__name__)

#: Market text -> Yahoo symbol. Indices only: single names would need an earnings
#: calendar and a borrow/dividend model to be priced honestly.
_INDEX_SYMBOLS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bs&p\s*500\b|\bsp500\b|\bspx\b|\bs and p 500\b", re.I), "^GSPC", "S&P 500"),
    (re.compile(r"\bnasdaq[\s-]*100\b|\bndx\b", re.I), "^NDX", "Nasdaq-100"),
    (re.compile(r"\bdow\s*(jones)?\b|\bdjia\b", re.I), "^DJI", "Dow Jones"),
    (re.compile(r"\brussell\s*2000\b|\brut\b", re.I), "^RUT", "Russell 2000"),
    (re.compile(r"\bvix\b|\bvolatility index\b", re.I), "^VIX", "VIX"),
]

#: Minimum daily closes before a volatility estimate is trustworthy.
_MIN_CLOSES = 30

#: Multiplier on sigma when the horizon spans an overnight gap. Close-to-close
#: realised vol already contains gap risk, but the *rate* at which variance accrues
#: across a gap is higher than the intraday rate, and the trading-time clock assigns
#: a gap zero width. Without this, an overnight market is priced as if nothing can
#: happen between the close and the next open.
_OVERNIGHT_GAP_PREMIUM = 1.35

#: Floor on trading time so a market minutes from settlement does not divide by ~0.
_MIN_TRADING_HOURS = 0.05


def detect_index(text: str) -> tuple[str, str] | None:
    """Return ``(yahoo_symbol, display_name)`` for a recognised index."""
    for pattern, symbol, name in _INDEX_SYMBOLS:
        if pattern.search(text):
            return symbol, name
    return None


def gbm_probability_above(
    spot: float, strike: float, sigma_annual: float, tau_years: float
) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    if tau_years <= 0 or sigma_annual <= 0:
        return 1.0 if spot > strike else 0.0
    vol_sqrt_t = sigma_annual * math.sqrt(tau_years)
    d2 = (math.log(spot / strike) - 0.5 * sigma_annual**2 * tau_years) / vol_sqrt_t
    return normal_cdf(d2)


class EquityIndexThresholdModel(ProbabilityModel):
    """Driftless-GBM index threshold model on trading-time, from Yahoo Finance."""

    name = "equity_index_gbm_threshold"
    categories = (Category.FINANCE,)
    independent = True
    #: Below the crypto cap: index markets face scheduled macro events that realised
    #: vol cannot see, and the overnight-gap adjustment is an approximation.
    max_confidence = Decimal("0.62")

    def __init__(self, client: HttpClient | None = None) -> None:
        settings = get_settings()
        self._client = client or HttpClient(
            settings.yahoo_finance_base,
            name="yahoo",
            rate_per_second=3.0,
            cache_ttl_seconds=180.0,
            # Yahoo rejects requests without a browser-like agent.
            headers={"User-Agent": "Mozilla/5.0 (compatible; pmvl-research/0.1)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _chart(self, symbol: str, *, range_: str, interval: str) -> dict | None:
        try:
            data = await self._client.get_json(
                f"/v8/finance/chart/{symbol}",
                params={"range": range_, "interval": interval},
                allow_404=True,
            )
        except ProviderError as exc:
            log.debug("yahoo chart failed for %s: %s", symbol, exc)
            return None
        if not isinstance(data, dict):
            return None
        results = (data.get("chart") or {}).get("result") or []
        return results[0] if results else None

    async def _spot_and_vol(self, symbol: str) -> tuple[float, float, int, float] | None:
        """``(spot, sigma_annual, n_returns, quote_age_seconds)``.

        Volatility is close-to-close over ~3 months of daily bars, annualised with
        252 trading days - consistent with the trading-time tau used downstream.
        """
        result = await self._chart(symbol, range_="3mo", interval="1d")
        if result is None:
            return None

        meta = result.get("meta") or {}
        spot = meta.get("regularMarketPrice")
        if spot is None:
            return None
        try:
            spot = float(spot)
        except (TypeError, ValueError):
            return None
        if spot <= 0:
            return None

        quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = [float(c) for c in (quotes.get("close") or []) if c is not None]
        if len(closes) < _MIN_CLOSES:
            return None

        returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0
        ]
        if len(returns) < _MIN_CLOSES - 1:
            return None

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
        sigma_annual = math.sqrt(variance) * math.sqrt(252.0)

        market_time = meta.get("regularMarketTime")
        age = 0.0
        if market_time:
            try:
                age = max(0.0, utcnow().timestamp() - float(market_time))
            except (TypeError, ValueError):
                age = 0.0

        return spot, sigma_annual, len(returns), age

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        market = ctx.market
        text = f"{market.title} {market.subtitle} {market.description[:400]}"
        index = detect_index(text)
        if index is None:
            return no_opinion("no recognised equity index in market text")
        symbol, display = index

        comparator = (market.strike_type or "").lower()
        floor_strike = market.floor_strike
        cap_strike = market.cap_strike

        if comparator == "between":
            if floor_strike is None or cap_strike is None:
                return no_opinion("between-market missing one of its two bounds")
            strike = floor_strike
        else:
            strike = floor_strike if floor_strike is not None else cap_strike
            if strike is None or strike <= 0:
                return no_opinion("no numeric strike could be established")

        now = ctx.now or utcnow()
        # Settlement is at a specific clock instant, so use the occurrence time when
        # the venue gives one - expected_resolution_time includes a reporting lag
        # during which the index no longer moves.
        # ensure_utc is mandatory: SQLite drops tzinfo on round-trip, so a market
        # loaded from the database has naive datetimes and comparing one against an
        # aware `now` raises TypeError.
        target = ensure_utc(
            market.event_occurrence_time
            or market.close_time
            or market.expected_resolution_time
        )
        if target is None:
            return no_opinion("no settlement instant on the market")
        if target <= now:
            return no_opinion(
                "settlement instant has passed; the level is determined and the "
                "market knows it"
            )

        trading_hours = trading_hours_between(now, target)
        calendar_hours = (target - now).total_seconds() / 3600.0
        if trading_hours < _MIN_TRADING_HOURS:
            return no_opinion(
                f"only {trading_hours:.3f} trading hours remain "
                f"({calendar_hours:.1f}h calendar); the level is effectively fixed"
            )

        data = await self._spot_and_vol(symbol)
        if data is None:
            return no_opinion(f"no usable price history for {symbol}")
        spot, sigma, n_returns, quote_age = data
        if sigma <= 0:
            return no_opinion("realised volatility estimate collapsed to zero")

        # Variance does not accrue while the market is shut, but a gap is riskier per
        # unit of trading time than continuous trading is.
        spans_overnight = not is_market_open(now) or calendar_hours > trading_hours + 0.5
        if spans_overnight:
            sigma *= _OVERNIGHT_GAP_PREMIUM

        tau = trading_hours / TRADING_HOURS_PER_YEAR
        barrier = is_barrier_market(f"{market.title} {market.subtitle}")
        if barrier:
            # Index markets on this venue settle on a level at an instant, not on a
            # touch. Refuse rather than silently mispricing if the wording says touch.
            return no_opinion(
                "barrier/touch wording on an index market; terminal model would "
                "understate it and no touch model is fitted for indices"
            )

        is_below = comparator.startswith("less")

        def probability_at(vol: float) -> float:
            if comparator == "between":
                above_low = gbm_probability_above(spot, float(floor_strike), vol, tau)
                above_high = gbm_probability_above(spot, float(cap_strike), vol, tau)
                return max(0.0, above_low - above_high)
            above = gbm_probability_above(spot, float(strike), vol, tau)
            return (1.0 - above) if is_below else above

        probability = probability_at(sigma)

        # Interval from the standard error of the volatility estimate, sigma/sqrt(2n).
        vol_se = sigma / math.sqrt(2 * n_returns)
        p_low = probability_at(max(1e-6, sigma - vol_se))
        p_high = probability_at(sigma + vol_se)
        stdev = abs(p_high - p_low) / 2.0

        confidence = self.max_confidence
        # Very short trading horizons make the answer hinge on microstructure the
        # model does not see; very long ones outrun the realised-vol assumption.
        if trading_hours < 0.5:
            confidence *= Decimal("0.7")
        if trading_hours > 6.5 * 10:
            confidence *= Decimal("0.85")
        if spans_overnight:
            confidence *= Decimal("0.9")
        # A stale spot is a real problem for a threshold sitting near the money.
        if quote_age > 6 * 3600:
            confidence *= Decimal("0.8")

        strike_label = (
            f"{float(floor_strike):,.0f}-{float(cap_strike):,.0f}"
            if comparator == "between"
            else f"{'<' if is_below else '>='}{float(strike):,.2f}"
        )

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(D(str(probability)))),
            confidence=quantize_prob(confidence),
            stdev=max(D(str(stdev)), Decimal("0.005")),
            independent=True,
            detail=(
                f"GBM {display}: spot={spot:,.2f} strike {strike_label} "
                f"sigma={sigma:.1%}/yr tau={trading_hours:.2f} trading-h "
                f"(vs {calendar_hours:.1f}h calendar) -> P={probability:.3f}"
            ),
            data_freshness_seconds=int(quote_age),
            data={
                "symbol": symbol,
                "index": display,
                "spot": f"{spot:.2f}",
                "strike": strike_label,
                "comparator": comparator or "unknown",
                "sigma_annual": f"{sigma:.4f}",
                "trading_hours": f"{trading_hours:.3f}",
                "calendar_hours": f"{calendar_hours:.3f}",
                "spans_overnight": spans_overnight,
                "n_returns": n_returns,
                "model": "driftless_gbm_trading_time",
            },
        )
