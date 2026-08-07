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
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ZERO, clamp_prob, quantize_prob
from pmvl_shared.timeutil import iso, utcnow, years_until

from ...providers.http import HttpClient, ProviderError
from ..base import (
    ModelContext,
    ModelEstimate,
    ProbabilityModel,
    lookahead_guard,
    no_opinion,
)

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

_CANDLE_GRANULARITY_SECONDS = 3600
#: How far back a windowed (historical) candle request reaches. Coinbase returns at
#: most 300 candles per request, so this is the ceiling, not a preference.
_HISTORY_WINDOW_HOURS = 290

#: A *price* band written into the contract text: "60,000-62,000", "$60,000 to
#: $62,000", "between $60,000 and $62,000".
#:
#: Needed because the venue does not always classify these. Kalshi's crypto range
#: boards arrive with ``strike_type`` unset on roughly half the board, and a band
#: read as a one-sided threshold is the single most overstated estimate this model
#: can produce: "between $60,000 and $62,000" with spot at $63,784 scored 0.999999
#: against a market price of 0.016, because the $62,000 cap was simply dropped.
#:
#: Both sides must look like money — a currency symbol or thousands separators.
#: A bare ``\d+-\d+`` also matches the date range in "Will Bitcoin reach $70,000
#: August 3-9?", and reading that as a band gives bounds of 3 and 70,000: the
#: model then declines a contract it can price perfectly well, which is a quieter
#: failure than the one being fixed but still a wrong answer.
_MONEY = r"(?:\$\s*\d[\d,]*(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?)"
_TEXT_RANGE_RE = re.compile(
    rf"({_MONEY})\s*(?:-|–|—|\bto\b|\band\b)\s*({_MONEY})",
    re.I,
)


def _text_price_band(headline: str) -> tuple[Decimal, Decimal] | None:
    """The two bounds of a price band stated in the contract text, if any.

    Bounds come from the matched pair specifically, not from every number in the
    title: "between $60,000 and $62,000 on July 31" also contains 31.
    """
    match = _TEXT_RANGE_RE.search(headline)
    if match is None:
        return None
    try:
        low = Decimal(match.group(1).replace("$", "").replace(",", "").strip())
        high = Decimal(match.group(2).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
    if low <= 0 or high <= 0:
        return None
    return (low, high) if low <= high else (high, low)

#: Directional markets ("will BTC close higher than it opened") have no threshold at
#: all - they compare against a reference price fixed at the open. A threshold model
#: cannot price them, and scraping a number out of the title produces nonsense.
_DIRECTIONAL_RE = re.compile(
    r"\bup or down\b|\bhigher or lower\b|\bup / down\b|\bclose higher\b"
    r"|\bclose lower\b|\bgreen or red\b",
    re.IGNORECASE,
)

#: A strike recovered from free text must be within this band of spot to be credible.
#: "Bitcoin Up or Down - July 26" yielded a strike of 26 against a spot of 65,052 and
#: was priced as a near-certainty at 29% ensemble weight. Venue-supplied strikes skip
#: this check; only text-scraped ones are subject to it.
_TEXT_STRIKE_MIN_RATIO = 0.02
_TEXT_STRIKE_MAX_RATIO = 50.0


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
        # The reflected term takes -b, not +b.
        #
        # This previously read `1 - normal_cdf((-b + drift) / vol_sqrt_t)`, which
        # is `normal_cdf((b - drift) / vol_sqrt_t)` - the same argument as `first`
        # but un-complemented, so for any barrier meaningfully above spot it was
        # ~1 instead of ~0. "Will Bitcoin reach $70,000" with spot at $64,254 and
        # five days to run returned 0.918 against a Monte Carlo value of 0.010,
        # and the market's 0.021.
        #
        # The existing property tests did not catch it because a value that is far
        # too high still satisfies every one of them: it is still >= the terminal
        # probability, still monotone in the barrier, and still tends to 1 as the
        # barrier approaches spot. Only a test that pins the NUMBER finds this,
        # which is why the suite now checks both branches against a simulation.
        first = 1.0 - normal_cdf((b - drift) / vol_sqrt_t)
        second = normal_cdf((-b - drift) / vol_sqrt_t)

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
    #: Both inputs are windowed candle requests ending at the evaluation instant,
    #: so this model can be replayed. See ``_candles``.
    supports_as_of = True

    def __init__(self, client: HttpClient | None = None) -> None:
        settings = get_settings()
        self._client = client or HttpClient(
            settings.coinbase_api_base, name="coinbase",
            rate_per_second=4.0, cache_ttl_seconds=120.0,
        )
        self._spot_cache: dict[str, tuple[float, float]] = {}
        #: Historical candle windows, keyed by (product, as_of). Spot and realised
        #: volatility are derived from the same window, and under retrodiction that
        #: window is fetched per evaluation rather than served from the client's
        #: short TTL cache - so without this the harness makes exactly twice the
        #: requests it needs, against a rate-limited public endpoint.
        self._history_cache: dict[tuple[str, str], list[list[float]] | None] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _candles(
        self, product: str, *, as_of: datetime | None
    ) -> list[list[float]] | None:
        """Hourly candles, newest first, ending at ``as_of`` when one is given.

        Coinbase caps a windowed request at 300 candles, which at hourly granularity
        is twelve and a half days - comfortably more than the volatility window needs
        and the reason the window is expressed in hours rather than days.
        """
        cache_key = (product, iso(as_of) or "")
        if as_of is not None and cache_key in self._history_cache:
            return self._history_cache[cache_key]

        params: dict[str, object] = {"granularity": _CANDLE_GRANULARITY_SECONDS}
        if as_of is not None:
            start = as_of - timedelta(hours=_HISTORY_WINDOW_HOURS)
            params["start"] = iso(start)
            params["end"] = iso(as_of)
        try:
            candles = await self._client.get_json(
                f"/products/{product}/candles", params=params
            )
        except ProviderError as exc:
            log.debug("coinbase candles failed for %s: %s", product, exc)
            if as_of is not None:
                self._history_cache[cache_key] = None
            return None
        if not isinstance(candles, list):
            if as_of is not None:
                self._history_cache[cache_key] = None
            return None

        rows = [c for c in candles if isinstance(c, list) and len(c) >= 5]
        if as_of is None:
            return rows

        # Belt and braces. The window was requested, but a provider that ignores or
        # misreads `end` would hand back candles from after the evaluation instant,
        # and every one of them would be look-ahead. Coinbase timestamps a candle
        # with its OPENING time, so a candle is only fully in the past once its
        # close - one granularity later - is at or before `as_of`.
        cutoff = as_of.timestamp() - _CANDLE_GRANULARITY_SECONDS
        windowed = [c for c in rows if float(c[0]) <= cutoff]
        self._history_cache[cache_key] = windowed
        return windowed

    async def _spot(self, product: str, *, as_of: datetime | None = None) -> float | None:
        """Spot price now, or the last completed hourly close at ``as_of``.

        The live path reads the ticker because it is the freshest number available.
        The historical path cannot: a ticker has no memory. It uses the close of the
        most recent candle that had finished by ``as_of``, which is the best estimate
        of "the price a trader could see at that moment" the free feed supports.
        """
        if as_of is not None:
            candles = await self._candles(product, as_of=as_of)
            if not candles:
                return None
            newest = max(candles, key=lambda c: float(c[0]))
            try:
                return float(newest[4])
            except (IndexError, TypeError, ValueError):
                return None

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

    async def _realised_vol(
        self, product: str, *, as_of: datetime | None = None
    ) -> tuple[float, int] | None:
        """Annualised realised volatility from hourly closes.

        Returns ``(sigma_annual, n_returns)``. The sample size is returned because the
        standard error of a volatility estimate is ``sigma / sqrt(2n)``, which the
        caller folds into the confidence interval.
        """
        candles = await self._candles(product, as_of=as_of)
        if candles is None or len(candles) < _MIN_CANDLES:
            return None

        # Coinbase returns [time, low, high, open, close, volume], newest first.
        # Sort rather than reverse: the windowed request is not documented to
        # preserve ordering, and a mis-ordered series turns real returns into noise.
        ordered = sorted(candles, key=lambda c: float(c[0]))
        closes = [float(c[4]) for c in ordered]
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
        if (declined := lookahead_guard(self, ctx)) is not None:
            return declined

        market = ctx.market
        text = f"{market.title} {market.subtitle} {market.description[:400]}"
        product = detect_product(text)
        if product is None:
            return no_opinion("no recognised crypto asset in market text")

        # Same three shapes as the weather board, and the same trap: a `between`
        # market ("$72,000 to $73,000") priced as a one-sided threshold would be
        # wildly overstated.
        if _DIRECTIONAL_RE.search(f"{market.title} {market.subtitle}"):
            return no_opinion(
                "directional up/down market: settles against the opening price, not a "
                "threshold, so a threshold model cannot price it"
            )

        from ...normalize.text import extract_features

        features = extract_features(market.title, subtitle=market.subtitle)
        headline = f"{market.title} {market.subtitle or ''}"

        comparator = (market.strike_type or "").lower()
        floor_strike = market.floor_strike
        cap_strike = market.cap_strike
        #: True when the strike came from the venue rather than from a regex.
        strike_from_venue = floor_strike is not None or cap_strike is not None

        # The venue does not always classify a band, so the text gets a vote.
        #
        # This guard used to depend entirely on `strike_type`. On the live board
        # 96 of 344 crypto contracts are bands and the venue left `strike_type`
        # unset on 49 of them; each of those fell through to the one-sided branch
        # below, which took the FIRST number in the title as a threshold and threw
        # the second away. That is how a contract the market priced at 1.6c was
        # scored at 99.9999%.
        text_band = _text_price_band(headline)
        if comparator != "between" and (
            features.comparator == "between" or text_band is not None
        ):
            comparator = "between"
            if floor_strike is None or cap_strike is None:
                if text_band is None:
                    return no_opinion(
                        "contract text describes a range but its two bounds could "
                        "not be recovered; refusing to price it as a threshold"
                    )
                floor_strike, cap_strike = text_band

        if comparator == "between":
            if floor_strike is None or cap_strike is None:
                return no_opinion("between-market missing one of its two bounds")
            if cap_strike <= floor_strike:
                return no_opinion("between-market bounds are not ordered")
            strike = floor_strike
        else:
            strike = floor_strike if floor_strike is not None else cap_strike
            if strike is None:
                strike = features.primary_threshold
            if strike is None or strike <= 0:
                return no_opinion("no numeric strike could be established")

        resolution = market.expected_resolution_time or market.close_time
        evaluated_at = ctx.evaluation_time or utcnow()
        tau = years_until(resolution, now=evaluated_at)
        if tau <= 0:
            return no_opinion("market has no remaining time to resolution")

        spot = await self._spot(product, as_of=ctx.as_of)
        vol = await self._realised_vol(product, as_of=ctx.as_of)
        if spot is None or vol is None:
            if ctx.as_of is not None:
                return no_opinion(
                    f"coinbase has no {product} history covering "
                    f"{ctx.as_of.isoformat()}; the window is capped at "
                    f"{_HISTORY_WINDOW_HOURS}h and old candles are not retained "
                    "indefinitely"
                )
            return no_opinion(f"coinbase data unavailable for {product}")
        sigma, n_returns = vol

        # A strike recovered from free text is only credible if it is the same order
        # of magnitude as spot. Without this check "Bitcoin Up or Down - July 26"
        # yielded strike=26 against spot=65,052 and was scored as a near-certainty.
        if not strike_from_venue:
            # Every text-derived level is checked, not only the first. A band takes
            # two numbers out of the title, and a plausible floor beside a nonsense
            # cap still produces a nonsense width.
            scraped = [strike] if comparator != "between" else [floor_strike, cap_strike]
            for level in scraped:
                ratio = float(level) / spot
                if not (_TEXT_STRIKE_MIN_RATIO <= ratio <= _TEXT_STRIKE_MAX_RATIO):
                    return no_opinion(
                        f"strike {level} scraped from title is implausible against "
                        f"spot {spot:,.0f} (ratio {ratio:.4g}); refusing to guess"
                    )

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
            # Live, the ticker is current. Replayed, the freshest thing available is
            # the last *completed* hourly candle, so the estimate is built on data up
            # to one granularity old. Reporting 0 there would overstate it.
            data_freshness_seconds=(
                0 if ctx.as_of is None else _CANDLE_GRANULARITY_SECONDS
            ),
            data={
                "product": product,
                "spot": f"{spot:.2f}",
                "evaluated_as_of": iso(ctx.as_of),
                "spot_source": "ticker" if ctx.as_of is None else "last_closed_hourly_candle",
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
