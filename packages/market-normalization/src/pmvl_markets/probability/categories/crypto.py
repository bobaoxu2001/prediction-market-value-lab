"""Crypto / equity threshold model.

For a market of the form "will ASSET be above K at time T", the probability is
computed from a geometric Brownian motion fitted to recent realised volatility:

    P(S_T > K) = Phi( [ln(S_0/K) - sigma^2 * tau / 2] / (sigma * sqrt(tau)) )

with zero drift. Zero drift is a deliberate choice, not an omission: estimating drift
from a few days of hourly returns produces an enormous standard error, and any drift
large enough to matter over a 24-hour horizon would be an arbitrage in the spot
market. Assuming a martingale is both the standard and the conservative option.

Spot and realised volatility come from Coinbase's public market-data API, which
requires no credentials. This is genuinely independent of the prediction market's own
price, which is what makes it usable for edge detection.

**Model risk that is deliberately surfaced:** realised volatility is backward-looking.
Around scheduled events (CPI prints, ETF decisions, halvings) implied volatility
exceeds realised and this model will understate the probability of tail outcomes.
Confidence is capped accordingly and the interval is widened by the standard error of
the volatility estimate itself.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ZERO, clamp_prob, quantize_prob
from pmvl_shared.timeutil import utcnow, years_until

from ...providers.http import HttpClient, ProviderError
from ..base import ModelContext, ModelEstimate, ProbabilityModel, no_opinion

log = get_logger(__name__)

#: Market text -> Coinbase product id. Only assets with a liquid USD spot pair.
_ASSET_PRODUCTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bbitcoin\b|\bbtc\b", re.I), "BTC-USD"),
    (re.compile(r"\bethereum\b|\beth\b", re.I), "ETH-USD"),
    (re.compile(r"\bsolana\b|\bsol\b", re.I), "SOL-USD"),
    (re.compile(r"\bripple\b|\bxrp\b", re.I), "XRP-USD"),
    (re.compile(r"\bdogecoin\b|\bdoge\b", re.I), "DOGE-USD"),
    (re.compile(r"\bcardano\b|\bada\b", re.I), "ADA-USD"),
    (re.compile(r"\blitecoin\b|\bltc\b", re.I), "LTC-USD"),
    (re.compile(r"\bchainlink\b|\blink\b", re.I), "LINK-USD"),
    (re.compile(r"\bavalanche\b|\bavax\b", re.I), "AVAX-USD"),
]

#: Below this many hours of history the volatility estimate is too noisy to use.
_MIN_CANDLES = 48


def detect_product(text: str) -> str | None:
    for pattern, product in _ASSET_PRODUCTS:
        if pattern.search(text):
            return product
    return None


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


#: Phrases that make a market a **barrier** (touch) event rather than a terminal one.
#: "Will BTC dip to 60k this week" pays if the price touches 60k at any moment, which
#: is strictly more likely than being below 60k at expiry - roughly twice as likely
#: for a driftless process. Pricing these with the terminal distribution understates
#: them badly and shows up as a large, fake model-vs-market divergence.
_BARRIER_RE = re.compile(
    r"\bdip(s|ped)?\s+to\b|\breach(es|ed)?\b|\bhit(s)?\b|\btouch(es|ed)?\b"
    r"|\bat any (time|point)\b|\bever\b|\bintraday\b",
    re.IGNORECASE,
)


def is_barrier_market(text: str) -> bool:
    return bool(_BARRIER_RE.search(text))


def gbm_probability_above(
    spot: float, strike: float, sigma_annual: float, tau_years: float
) -> float:
    """P(S_T > K) under driftless GBM. Degenerate cases resolve to the spot answer."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if tau_years <= 0 or sigma_annual <= 0:
        return 1.0 if spot > strike else 0.0
    vol_sqrt_t = sigma_annual * math.sqrt(tau_years)
    d2 = (math.log(spot / strike) - 0.5 * sigma_annual**2 * tau_years) / vol_sqrt_t
    return normal_cdf(d2)


def gbm_probability_touch(
    spot: float, barrier: float, sigma_annual: float, tau_years: float
) -> float:
    """P(the path touches ``barrier`` at any time before T) under driftless GBM.

    Closed form from the reflection principle for arithmetic Brownian motion in log
    space with drift ``mu = -sigma^2 / 2``:

        P(min_t X_t <= b) = Phi((b - mu*T) / (sigma*sqrt(T)))
                          + exp(2*mu*b / sigma^2) * Phi((b + mu*T) / (sigma*sqrt(T)))

    where ``b = ln(barrier / spot)``. The mirrored expression applies for an upper
    barrier. Always at least the terminal probability, and tends to 1 as the barrier
    approaches spot - both asserted in the test suite.
    """
    if spot <= 0 or barrier <= 0 or tau_years <= 0 or sigma_annual <= 0:
        return 0.0
    # Already touched: the barrier is on the wrong side of spot right now.
    b = math.log(barrier / spot)
    if b == 0:
        return 1.0

    mu = -0.5 * sigma_annual**2
    vol_sqrt_t = sigma_annual * math.sqrt(tau_years)
    drift = mu * tau_years
    exponent = 2.0 * mu * b / (sigma_annual**2)
    # Guard the exponential against overflow for distant barriers.
    scale = math.exp(exponent) if -700 < exponent < 700 else (0.0 if exponent < 0 else float("inf"))

    if b < 0:  # downward barrier: P(min <= b)
        first = normal_cdf((b - drift) / vol_sqrt_t)
        second = normal_cdf((b + drift) / vol_sqrt_t)
    else:  # upward barrier: P(max >= b)
        first = 1.0 - normal_cdf((b - drift) / vol_sqrt_t)
        second = 1.0 - normal_cdf((-b + drift) / vol_sqrt_t)

    if scale == float("inf"):
        return 1.0
    return min(1.0, max(0.0, first + scale * second))


class CryptoThresholdModel(ProbabilityModel):
    """Driftless-GBM threshold model driven by Coinbase spot and realised vol."""

    name = "crypto_gbm_threshold"
    categories = (Category.CRYPTO, Category.FINANCE)
    independent = True
    #: Capped below 1 because realised vol is a backward-looking proxy for the
    #: forward vol that actually determines the outcome.
    max_confidence = Decimal("0.68")

    def __init__(self, client: HttpClient | None = None) -> None:
        settings = get_settings()
        self._client = client or HttpClient(
            settings.coinbase_api_base, name="coinbase",
            rate_per_second=4.0, cache_ttl_seconds=120.0,
        )
        self._spot_cache: dict[str, tuple[float, float]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _spot(self, product: str) -> float | None:
        try:
            data = await self._client.get_json(f"/products/{product}/ticker", allow_404=True)
        except ProviderError as exc:
            log.debug("coinbase ticker failed for %s: %s", product, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return float(data["price"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _realised_vol(self, product: str) -> tuple[float, int] | None:
        """Annualised realised volatility from hourly closes.

        Returns ``(sigma_annual, n_returns)``. The sample size is returned because the
        standard error of a volatility estimate is ``sigma / sqrt(2n)``, which the
        caller folds into the confidence interval.
        """
        try:
            candles = await self._client.get_json(
                f"/products/{product}/candles", params={"granularity": 3600}
            )
        except ProviderError as exc:
            log.debug("coinbase candles failed for %s: %s", product, exc)
            return None
        if not isinstance(candles, list) or len(candles) < _MIN_CANDLES:
            return None

        # Coinbase returns [time, low, high, open, close, volume], newest first.
        closes = [float(c[4]) for c in candles if isinstance(c, list) and len(c) >= 5]
        closes.reverse()
        returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0
        ]
        if len(returns) < _MIN_CANDLES // 2:
            return None

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
        hourly_sigma = math.sqrt(variance)
        annual_sigma = hourly_sigma * math.sqrt(24 * 365.25)
        return annual_sigma, len(returns)

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        market = ctx.market
        text = f"{market.title} {market.subtitle} {market.description[:400]}"
        product = detect_product(text)
        if product is None:
            return no_opinion("no recognised crypto asset in market text")

        # Same three shapes as the weather board, and the same trap: a `between`
        # market ("$72,000 to $73,000") priced as a one-sided threshold would be
        # wildly overstated.
        comparator = (market.strike_type or "").lower()
        floor_strike = market.floor_strike
        cap_strike = market.cap_strike

        if comparator == "between":
            if floor_strike is None or cap_strike is None:
                return no_opinion("between-market missing one of its two bounds")
            strike = floor_strike
        else:
            strike = floor_strike if floor_strike is not None else cap_strike
            if strike is None:
                from ...normalize.text import extract_features

                features = extract_features(market.title, subtitle=market.subtitle)
                strike = features.primary_threshold
            if strike is None or strike <= 0:
                return no_opinion("no numeric strike could be established")

        resolution = market.expected_resolution_time or market.close_time
        tau = years_until(resolution, now=ctx.now or utcnow())
        if tau <= 0:
            return no_opinion("market has no remaining time to resolution")

        spot = await self._spot(product)
        vol = await self._realised_vol(product)
        if spot is None or vol is None:
            return no_opinion(f"coinbase data unavailable for {product}")
        sigma, n_returns = vol

        # Direction: a "below/less than" market pays YES when the threshold is NOT
        # exceeded. Getting this backwards would invert every crypto recommendation.
        is_below = comparator.startswith("less") or (
            comparator not in ("greater", "between")
            and bool(re.search(r"\bbelow\b|\bunder\b|\bless than\b|\bat or below\b", text, re.I))
        )

        barrier = is_barrier_market(f"{market.title} {market.subtitle}")

        def probability_at(vol: float) -> float:
            if barrier and comparator != "between":
                # Touch semantics: the contract pays if the price reaches the level at
                # any point, not only at expiry.
                return gbm_probability_touch(spot, float(strike), vol, tau)
            if comparator == "between":
                # P(a < S_T <= b) = P(S_T > a) - P(S_T > b)
                above_low = gbm_probability_above(spot, float(floor_strike), vol, tau)
                above_high = gbm_probability_above(spot, float(cap_strike), vol, tau)
                return max(0.0, above_low - above_high)
            above = gbm_probability_above(spot, float(strike), vol, tau)
            return (1.0 - above) if is_below else above

        probability = probability_at(sigma)

        # Interval from volatility estimation error: sigma_hat has standard error
        # sigma / sqrt(2n), which propagates into the probability by re-evaluating
        # the model at +/- one standard error of vol.
        vol_se = sigma / math.sqrt(2 * n_returns)
        p_lo_vol = probability_at(max(1e-6, sigma - vol_se))
        p_hi_vol = probability_at(sigma + vol_se)
        stdev = abs(p_hi_vol - p_lo_vol) / 2.0

        strike_label = (
            f"{float(floor_strike):,.0f}-{float(cap_strike):,.0f}"
            if comparator == "between"
            else f"{'<' if is_below else '>'}{float(strike):,.0f}"
        )

        # Confidence degrades with horizon: a 30-day GBM extrapolation from 3 days of
        # hourly returns is much weaker than a 6-hour one.
        hours = tau * 24 * 365.25
        horizon_penalty = 1.0 if hours <= 48 else max(0.35, 48.0 / hours)
        confidence = float(self.max_confidence) * horizon_penalty
        # Near-certain outcomes are where GBM's thin tails are least trustworthy.
        if probability < 0.02 or probability > 0.98:
            confidence *= 0.5

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(D(str(probability)))),
            confidence=quantize_prob(D(str(min(confidence, float(self.max_confidence))))),
            stdev=max(D(str(stdev)), Decimal("0.01")),
            independent=True,
            detail=(
                f"GBM {product}: spot={spot:,.2f} strike {strike_label} "
                f"sigma={sigma:.1%}/yr tau={hours:.1f}h "
                f"{'[touch]' if barrier else '[terminal]'} -> P={probability:.3f}"
            ),
            data_freshness_seconds=0,
            data={
                "product": product,
                "spot": f"{spot:.2f}",
                "strike": strike_label,
                "sigma_annual": f"{sigma:.4f}",
                "tau_hours": f"{hours:.2f}",
                "comparator": comparator or "unknown",
                "settlement_style": "touch" if barrier else "terminal",
                "direction": "below" if is_below else "above",
                "n_returns": n_returns,
                "model": "driftless_gbm",
            },
        )
