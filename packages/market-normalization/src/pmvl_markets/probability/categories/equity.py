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
    EFFECTIVE_HOURS_PER_YEAR,
    OVERNIGHT_VARIANCE_WEIGHT,
    effective_volatility_hours,
    is_market_open,
)
from .crypto import is_barrier_market, normal_cdf

log = get_logger(__name__)

#: Market text -> Yahoo symbol. Indices only: single names would need an earnings
#: calendar and a borrow/dividend model to be priced honestly.
#: ``(pattern, cash symbol, futures symbol, display name)``. The futures leg is the
#: overnight anchor; ``None`` means no usable front-month contract.
_INDEX_SYMBOLS: list[tuple[re.Pattern[str], str, str | None, str]] = [
    (re.compile(r"\bs&p\s*500\b|\bsp500\b|\bspx\b|\bs and p 500\b", re.I),
     "^GSPC", "ES=F", "S&P 500"),
    (re.compile(r"\bnasdaq[\s-]*100\b|\bndx\b", re.I), "^NDX", "NQ=F", "Nasdaq-100"),
    (re.compile(r"\bdow\s*(jones)?\b|\bdjia\b", re.I), "^DJI", "YM=F", "Dow Jones"),
    (re.compile(r"\brussell\s*2000\b|\brut\b", re.I), "^RUT", "RTY=F", "Russell 2000"),
    # VIX futures are a different animal (term structure, not a spot proxy).
    (re.compile(r"\bvix\b|\bvolatility index\b", re.I), "^VIX", None, "VIX"),
]

#: Reject a futures-implied level that differs from the cash close by more than this.
#: A >8% overnight move is possible but far more likely to be a bad symbol, a roll
#: artefact or a stale bar, and silently anchoring on it would be worse than not
#: adjusting at all.
_MAX_OVERNIGHT_ADJUSTMENT = 0.08

#: The futures bar used as the cash-close reference must be within this many seconds
#: of the actual cash close, or the implied return is measured over the wrong window.
_MAX_ANCHOR_GAP_SECONDS = 2 * 3600

#: Minimum daily closes before a volatility estimate is trustworthy.
_MIN_CLOSES = 30

#: RiskMetrics decay. 0.94 is the standard daily value; the effective window is
#: roughly 1/(1-lambda) ~ 17 sessions, which matches the horizon these markets settle
#: over far better than an equal-weight quarter does.
_EWMA_LAMBDA = 0.94

#: Floor on volatility time so a market minutes from settlement does not divide by ~0.
_MIN_EFFECTIVE_HOURS = 0.05


def detect_index(text: str) -> tuple[str, str | None, str] | None:
    """Return ``(cash_symbol, futures_symbol, display_name)`` for a known index."""
    for pattern, cash, futures, name in _INDEX_SYMBOLS:
        if pattern.search(text):
            return cash, futures, name
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

    async def _spot_and_vol(
        self, symbol: str
    ) -> tuple[float, float, int, float, float] | None:
        """``(spot, sigma_annual, n_returns, quote_age_seconds, sigma_uncertainty)``.

        Sigma is an EWMA of squared close-to-close returns (RiskMetrics, lambda=0.94),
        annualised with 252 days. EWMA rather than an equal-weight window because
        volatility is persistent and these markets settle within a day or two: a
        three-month equal-weight estimate still carries a shock from ten weeks ago at
        full weight, which is the wrong input for tomorrow's session.

        ``sigma_uncertainty`` is the spread between short (21d) and long (63d) window
        estimates. That disagreement is a far better measure of how unsure the model
        is about volatility than the textbook standard error sigma/sqrt(2n), which
        assumes returns are IID and therefore understates it badly.

        **This is realised volatility, not implied.** No forward-looking vol surface is
        keyless, so when the term structure is steep the model will disagree with the
        market about width. That disagreement is reported, not tuned away.
        """
        result = await self._chart(symbol, range_="6mo", interval="1d")
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

        def window_sigma(values: list[float]) -> float:
            if len(values) < 5:
                return 0.0
            mean = sum(values) / len(values)
            var = sum((r - mean) ** 2 for r in values) / max(1, len(values) - 1)
            return math.sqrt(var) * math.sqrt(252.0)

        # EWMA seeded on the first window, then decayed across the rest.
        seed = returns[: min(21, len(returns))]
        seed_mean = sum(seed) / len(seed)
        variance = sum((r - seed_mean) ** 2 for r in seed) / max(1, len(seed) - 1)
        for r in returns[len(seed):]:
            variance = _EWMA_LAMBDA * variance + (1.0 - _EWMA_LAMBDA) * r * r
        sigma_annual = math.sqrt(variance) * math.sqrt(252.0)

        short_sigma = window_sigma(returns[-21:])
        long_sigma = window_sigma(returns[-63:])
        sigma_uncertainty = abs(short_sigma - long_sigma) / 2.0

        market_time = meta.get("regularMarketTime")
        age = 0.0
        if market_time:
            try:
                age = max(0.0, utcnow().timestamp() - float(market_time))
            except (TypeError, ValueError):
                age = 0.0

        return spot, sigma_annual, len(returns), age, sigma_uncertainty

    async def _futures_implied_cash(
        self, futures_symbol: str, cash_spot: float, cash_close_ts: float
    ) -> tuple[float, float] | None:
        """Cash level implied by the futures move since the cash close.

        Returns ``(implied_cash, futures_return)``.

        Uses the futures **return** since the cash close rather than the outright
        futures price, so the calculation never needs to know the basis:

            implied_cash = cash_close * (futures_now / futures_at_cash_close)

        Carry and dividends cancel to first order, and a contract roll inside the
        window cancels too, because both legs are read from the same series.
        """
        result = await self._chart(futures_symbol, range_="5d", interval="30m")
        if result is None:
            return None

        timestamps = result.get("timestamp") or []
        quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quotes.get("close") or []
        bars = [
            (float(t), float(c))
            for t, c in zip(timestamps, closes)
            if t is not None and c is not None and float(c) > 0
        ]
        if len(bars) < 4:
            return None

        latest_ts, futures_now = bars[-1]

        # Bar closest to the cash close, which is the reference the return is measured
        # from. If the series does not reach back that far the adjustment is invalid.
        anchor_ts, futures_at_close = min(bars, key=lambda b: abs(b[0] - cash_close_ts))
        if abs(anchor_ts - cash_close_ts) > _MAX_ANCHOR_GAP_SECONDS:
            log.debug(
                "%s: nearest futures bar is %.0fs from the cash close; skipping anchor",
                futures_symbol, abs(anchor_ts - cash_close_ts),
            )
            return None
        if latest_ts <= anchor_ts or futures_at_close <= 0:
            return None

        futures_return = futures_now / futures_at_close - 1.0
        if abs(futures_return) > _MAX_OVERNIGHT_ADJUSTMENT:
            log.warning(
                "%s implies a %.1f%% overnight move; rejecting as implausible",
                futures_symbol, futures_return * 100,
            )
            return None

        return cash_spot * (1.0 + futures_return), futures_return

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        market = ctx.market
        text = f"{market.title} {market.subtitle} {market.description[:400]}"
        index = detect_index(text)
        if index is None:
            return no_opinion("no recognised equity index in market text")
        symbol, futures_symbol, display = index

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

        cash_hours, overnight_hours = effective_volatility_hours(now, target)
        calendar_hours = (target - now).total_seconds() / 3600.0
        effective_hours = cash_hours + overnight_hours * OVERNIGHT_VARIANCE_WEIGHT
        if effective_hours < _MIN_EFFECTIVE_HOURS:
            return no_opinion(
                f"only {effective_hours:.3f} cash-hour-equivalents remain "
                f"({calendar_hours:.1f}h calendar); the level is effectively fixed"
            )

        data = await self._spot_and_vol(symbol)
        if data is None:
            return no_opinion(f"no usable price history for {symbol}")
        spot, sigma, n_returns, quote_age, sigma_uncertainty = data
        if sigma <= 0:
            return no_opinion("realised volatility estimate collapsed to zero")

        cash_spot = spot
        futures_return: float | None = None
        anchor = "cash_close"

        # Overnight, the cash print is stale: it is Friday's (or yesterday's) close
        # while the index has kept moving through the futures market. Anchoring on it
        # produced a systematic, same-signed gap across an entire strike ladder - the
        # model read low on every S&P strike at once, which is the signature of a
        # stale anchor rather than of an edge.
        if futures_symbol and not is_market_open(now) and quote_age > 600:
            implied = await self._futures_implied_cash(
                futures_symbol, cash_spot, utcnow().timestamp() - quote_age
            )
            if implied is not None:
                spot, futures_return = implied
                anchor = futures_symbol

        tau = effective_hours / EFFECTIVE_HOURS_PER_YEAR
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

        # Interval width driven by how much the volatility estimate itself could be
        # wrong. The IID standard error is a floor, not the answer: disagreement
        # between the 21d and 63d windows captures regime uncertainty that the IID
        # formula cannot see, and is usually the larger of the two.
        vol_se = max(sigma / math.sqrt(2 * n_returns), sigma_uncertainty)
        p_low = probability_at(max(1e-6, sigma - vol_se))
        p_high = probability_at(sigma + vol_se)
        stdev = abs(p_high - p_low) / 2.0

        confidence = self.max_confidence
        # Very short horizons hinge on microstructure the model does not see; very
        # long ones outrun the realised-vol assumption.
        if effective_hours < 0.5:
            confidence *= Decimal("0.7")
        if effective_hours > 6.5 * 10:
            confidence *= Decimal("0.85")
        if overnight_hours > 0:
            # Overnight variance is the least well-estimated part of the model.
            confidence *= Decimal("0.9")
        if anchor == "cash_close" and quote_age > 6 * 3600:
            # A stale cash anchor with no futures correction is the weakest case, and
            # is exactly what the futures anchor exists to avoid.
            confidence *= Decimal("0.7")

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
                f"GBM {display}: spot={spot:,.2f}"
                + (
                    f" (cash {cash_spot:,.2f} {futures_return:+.2%} via {anchor})"
                    if futures_return is not None
                    else " (cash close, no futures anchor)"
                )
                + f" strike {strike_label} sigma={sigma:.1%}/yr "
                f"tau={effective_hours:.2f}h-equiv "
                f"({cash_hours:.2f} cash + {overnight_hours:.2f} overnight, "
                f"{calendar_hours:.1f}h calendar) -> P={probability:.3f}"
            ),
            data_freshness_seconds=int(quote_age),
            data={
                "symbol": symbol,
                "index": display,
                "spot": f"{spot:.2f}",
                "cash_spot": f"{cash_spot:.2f}",
                "anchor": anchor,
                "futures_return": (
                    f"{futures_return:.6f}" if futures_return is not None else None
                ),
                "strike": strike_label,
                "comparator": comparator or "unknown",
                "sigma_annual": f"{sigma:.4f}",
                "sigma_estimator": f"ewma_lambda_{_EWMA_LAMBDA}",
                "sigma_uncertainty": f"{sigma_uncertainty:.4f}",
                "cash_hours": f"{cash_hours:.3f}",
                "overnight_hours": f"{overnight_hours:.3f}",
                "effective_hours": f"{effective_hours:.3f}",
                "calendar_hours": f"{calendar_hours:.3f}",
                "n_returns": n_returns,
                "model": "driftless_gbm_volatility_time",
            },
        )
