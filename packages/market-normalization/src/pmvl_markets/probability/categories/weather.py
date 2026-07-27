"""Weather threshold model backed by the National Weather Service.

Kalshi's temperature markets settle on official NWS observations for a named station,
so the NWS gridpoint forecast is both free, keyless, and *the same source that
determines settlement*. That makes it a genuinely independent and unusually
high-quality signal.

The forecast is a point estimate. Converting it to a probability requires a forecast
error distribution: NWS day-ahead maximum-temperature forecasts have an approximately
Gaussian error whose standard deviation grows with lead time. The values in
:data:`_FORECAST_SIGMA_F` are a conservative published-accuracy approximation, and
are deliberately on the wide side - overstating forecast precision would manufacture
edge on exactly the markets where this model is most confident.

    P(high > K) = 1 - Phi( (K - forecast) / sigma(lead_time) )
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Category
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, clamp_prob, quantize_prob
from pmvl_shared.timeutil import ensure_utc, parse_ts, utcnow

from ...providers.http import HttpClient, ProviderError
from ..base import ModelContext, ModelEstimate, ProbabilityModel, no_opinion

log = get_logger(__name__)

#: Observing stations Kalshi's daily temperature markets settle on, as
#: **latitude/longitude**. The NWS forecast grid cell is resolved at runtime through
#: the documented ``/points/{lat},{lon}`` endpoint and cached.
#:
#: Grid indices were previously hardcoded, and they were guessed rather than derived.
#: The Los Angeles entry pointed at a cell forecasting 89F while the LAX settlement
#: station forecast 81F - an eight-degree error that manufactured a 48-point
#: "disagreement" against a market that was pricing the real forecast correctly.
#: Coordinates are checkable against the station; grid indices are not.
_STATIONS: dict[str, tuple[float, float]] = {
    # token          (latitude, longitude)   station
    "new york city": (40.7794, -73.9692),   # Central Park, KNYC
    "nyc":           (40.7794, -73.9692),
    "chicago":       (41.7860, -87.7524),   # Midway, KMDW
    "miami":         (25.7906, -80.3164),   # Miami Intl, KMIA
    "austin":        (30.3210, -97.7600),   # Camp Mabry, KATT
    "denver":        (39.8467, -104.6564),  # Denver Intl, KDEN
    "los angeles":   (33.9425, -118.4081),  # LAX, KLAX
    "philadelphia":  (39.8683, -75.2311),   # Philadelphia Intl, KPHL
    "washington dc": (38.8472, -77.0345),   # Reagan National, KDCA
    "boston":        (42.3606, -71.0097),   # Logan, KBOS
    "houston":       (29.9902, -95.3368),   # Bush Intercontinental, KIAH
    "phoenix":       (33.4278, -112.0038),  # Sky Harbor, KPHX
    "seattle":       (47.4444, -122.3139),  # Sea-Tac, KSEA
    "atlanta":       (33.6301, -84.4418),   # Hartsfield-Jackson, KATL
    "dallas":        (32.8471, -96.8517),   # Dallas Love Field, KDAL
}

#: Standard deviation of NWS maximum-temperature forecast error, in degrees F, by
#: lead time in days. Widened beyond published mean-absolute-error figures because
#: MAE understates the tails that binary thresholds are sensitive to.
_FORECAST_SIGMA_F: dict[int, float] = {
    0: 2.0, 1: 2.8, 2: 3.6, 3: 4.4, 4: 5.2, 5: 6.0, 6: 6.8, 7: 7.5,
}
_MAX_LEAD_DAYS = 7


def celsius_to_fahrenheit(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def forecast_sigma(lead_days: int) -> float:
    if lead_days <= 0:
        return _FORECAST_SIGMA_F[0]
    return _FORECAST_SIGMA_F.get(min(lead_days, _MAX_LEAD_DAYS), 8.0)


def detect_station(text: str) -> tuple[str, tuple[float, float]] | None:
    lowered = text.lower()
    # Longest token first so "new york city" beats a bare "york".
    for token in sorted(_STATIONS, key=len, reverse=True):
        if token in lowered:
            return token, _STATIONS[token]
    return None


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class WeatherThresholdModel(ProbabilityModel):
    """NWS-forecast-driven probability for daily temperature threshold markets."""

    name = "weather_nws_threshold"
    categories = (Category.WEATHER,)
    independent = True
    max_confidence = Decimal("0.75")

    def __init__(self, client: HttpClient | None = None) -> None:
        settings = get_settings()
        self._client = client or HttpClient(
            settings.nws_api_base,
            name="nws",
            rate_per_second=3.0,
            # Gridpoint forecasts update hourly; caching avoids hammering a
            # government API that asks for polite use.
            cache_ttl_seconds=900.0,
            headers={"Accept": "application/geo+json"},
        )
        #: "lat,lon" -> "/gridpoints/OFFICE/X,Y"
        self._grid_cache: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _resolve_grid(self, lat: float, lon: float) -> str | None:
        """Resolve a coordinate to its NWS forecast grid endpoint, cached.

        Grid geometry changes when the NWS re-tiles a region, so the mapping is
        looked up rather than stored.
        """
        key = f"{lat:.4f},{lon:.4f}"
        if key in self._grid_cache:
            return self._grid_cache[key]
        try:
            data = await self._client.get_json(f"/points/{key}", allow_404=True)
        except ProviderError as exc:
            log.debug("NWS points lookup failed for %s: %s", key, exc)
            return None
        if not isinstance(data, dict):
            return None
        properties = data.get("properties") or {}
        office, grid_x, grid_y = (
            properties.get("gridId"),
            properties.get("gridX"),
            properties.get("gridY"),
        )
        if not office or grid_x is None or grid_y is None:
            return None
        path = f"/gridpoints/{office}/{grid_x},{grid_y}"
        self._grid_cache[key] = path
        return path

    async def _max_temperature_f(
        self, lat: float, lon: float, target_date: datetime
    ) -> tuple[float, datetime] | None:
        """Forecast daily maximum in F for ``target_date``, with its issue time."""
        path = await self._resolve_grid(lat, lon)
        if path is None:
            return None
        try:
            data = await self._client.get_json(path, allow_404=True)
        except ProviderError as exc:
            log.debug("NWS gridpoint failed for %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None

        props = data.get("properties") or {}
        block = props.get("maxTemperature") or {}
        values = block.get("values") or []
        # The API publishes degC under the wmoUnit vocabulary; convert explicitly
        # rather than assuming, since a unit change would silently break every strike.
        is_celsius = "degc" in str(block.get("uom", "")).lower()
        issued = parse_ts(props.get("updateTime")) or utcnow()
        target_day = target_date.date()

        for entry in values:
            valid = str(entry.get("validTime", ""))
            start = parse_ts(valid.split("/")[0]) if valid else None
            if start is None:
                continue
            # A "daily maximum" interval is labelled at 12:00Z of the local
            # forecast day; compare on the date the interval covers.
            if start.date() != target_day:
                continue
            raw = entry.get("value")
            if raw is None:
                continue
            temp = float(raw)
            return (celsius_to_fahrenheit(temp) if is_celsius else temp), issued
        return None

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        market = ctx.market
        text = f"{market.title} {market.subtitle}"
        if not re.search(r"\btemp(erature)?\b|\bhigh\b|\blow\b", text, re.I):
            return no_opinion("not a temperature market")

        station = detect_station(f"{text} {market.description[:200]}")
        if station is None:
            return no_opinion("no supported NWS station in market text")
        token, (lat, lon) = station

        # Kalshi runs three shapes per station and they are NOT interchangeable:
        #   greater  floor=88            -> P(T > 88)
        #   less     cap=81              -> P(T < 81)
        #   between  floor=87 cap=88     -> P(87 <= T <= 88)
        # Collapsing a `between` bucket to a one-sided threshold was the original
        # behaviour and is badly wrong: on the NYC board it would price the "87-88"
        # bucket as P(T > 87) ~ 0.35 against a 2c market, inventing an enormous
        # edge on a contract that is genuinely worth about two cents.
        comparator = (market.strike_type or "").lower()
        floor_strike = market.floor_strike
        cap_strike = market.cap_strike

        if comparator == "between":
            if floor_strike is None or cap_strike is None:
                return no_opinion("between-market missing one of its two bounds")
        else:
            strike = floor_strike if floor_strike is not None else cap_strike
            if strike is None:
                return no_opinion("no numeric temperature strike")

        target = ensure_utc(
            market.event_occurrence_time or market.expected_resolution_time or market.close_time
        )
        if target is None:
            return no_opinion("no target date")
        now = ctx.now or utcnow()
        lead_days = (target.date() - now.date()).days

        if lead_days < 0:
            # The observation window has closed: the day's high is already an
            # established fact and the market has it, while a forecast does not.
            # Kalshi keeps these open for a settlement window after the event, so
            # without this check the model "forecasts" a day that already happened
            # and reports an enormous edge against a market that simply knows the
            # answer. That is the textbook case of the market holding information
            # the model lacks, and it must produce no opinion at all.
            return no_opinion(
                f"observation window closed {-lead_days}d ago; the outcome is already "
                "determined and a forecast is not evidence about a past day"
            )
        if lead_days > _MAX_LEAD_DAYS:
            return no_opinion(f"lead time {lead_days}d exceeds forecast skill horizon")

        result = await self._max_temperature_f(lat, lon, target)
        if result is None:
            return no_opinion(f"no NWS max-temperature forecast for {token} on {target.date()}")
        forecast_f, issued = result

        sigma = forecast_sigma(lead_days)

        def probability_at(scale: float) -> float:
            """Model probability with the forecast-error sigma scaled by ``scale``."""
            s = sigma * scale
            if comparator == "between":
                # Kalshi's daily-high buckets are integer-degree bands, and the
                # settled value is a whole degree. "87 to 88" therefore covers the
                # half-open interval [86.5, 88.5) once rounding is accounted for;
                # using [87, 88] would understate the bucket by a full degree of
                # probability mass.
                lo = (float(floor_strike) - 0.5 - forecast_f) / s
                hi = (float(cap_strike) + 0.5 - forecast_f) / s
                return max(0.0, normal_cdf(hi) - normal_cdf(lo))
            if comparator.startswith("less"):
                # "80 or below" with cap=81 means T <= 80, i.e. below 80.5.
                return normal_cdf((float(cap_strike) - 0.5 - forecast_f) / s)
            # "89 or above" with floor=88 means T >= 89, i.e. above 88.5.
            return 1.0 - normal_cdf((float(floor_strike) + 0.5 - forecast_f) / s)

        probability = probability_at(1.0)

        # Sensitivity to the assumed sigma, used as the interval half-width.
        p_tight = probability_at(0.75)
        p_wide = probability_at(1.35)
        stdev = abs(p_tight - p_wide) / 2.0

        strike_label = (
            f"{float(floor_strike):.0f}-{float(cap_strike):.0f}F"
            if comparator == "between"
            else (
                f"<={float(cap_strike) - 1:.0f}F"
                if comparator.startswith("less")
                else f">={float(floor_strike) + 1:.0f}F"
            )
        )

        confidence = float(self.max_confidence) * max(0.3, 1.0 - 0.11 * lead_days)
        age_hours = (now - ensure_utc(issued)).total_seconds() / 3600 if issued else 0
        if age_hours > 6:
            confidence *= 0.8

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(D(str(probability)))),
            confidence=quantize_prob(D(str(min(confidence, float(self.max_confidence))))),
            stdev=max(D(str(stdev)), Decimal("0.02")),
            independent=True,
            detail=(
                f"NWS {token}: forecast high {forecast_f:.1f}F vs {strike_label}, "
                f"lead {lead_days}d, sigma {sigma:.1f}F -> P={probability:.3f}"
            ),
            data_freshness_seconds=int(age_hours * 3600),
            data={
                "station": token,
                "coordinates": f"{lat:.4f},{lon:.4f}",
                "forecast_high_f": f"{forecast_f:.2f}",
                "strike": strike_label,
                "lead_days": lead_days,
                "sigma_f": f"{sigma:.2f}",
                "comparator": comparator or "unknown",
                "model": "gaussian_forecast_error",
            },
        )
