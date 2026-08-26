"""NWS settlement-station observations, collected with research timing discipline.

What this collects and why
--------------------------
Actual observed conditions at the station a contract settles on - not a forecast,
and not a grid cell. Two questions become answerable that otherwise are not:

**Forecast error vs market lag.** A divergence between a model and a price has two
explanations: the forecast was wrong, or the market was slow. Without observations
there is nothing to separate them, and a study's headline result is whichever
happens to dominate.

**The intraday path.** For a daily-maximum contract the realised maximum becomes a
hard floor as the day progresses - usually settled by mid-afternoon local. A
forecast-only model therefore degrades through the day in a way that correlates
perfectly with time-to-expiry, which looks exactly like an alpha regime and is
not one. Only hourly observations expose it.

Cache bypass
------------
Every request here passes ``use_cache=False``. The shared :class:`HttpClient`
caches gridpoint responses for 900 s, which is right for a scoring pass that runs
every two hours and wrong for research: a cached body would be stamped with a
*new* ``available_at`` describing a poll that never reached the network. That is
not a stale number, it is a fabricated availability - the precise failure the
whole point-in-time layer exists to prevent.

The bypass is local to this client, so production caching elsewhere is untouched.
``cache_ttl_seconds=0`` makes it structural rather than a flag a future caller
could forget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import parse_ts

from .http import HttpClient, ProviderError

log = get_logger(__name__)

#: Bumped when what this collector captures changes.
PARSER_VERSION = "nws-observations-1.0.0"

NWS_API_BASE = "https://api.weather.gov"


def celsius_to_fahrenheit(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


@dataclass
class ObservationRecord:
    """One station observation, with its provenance kept separate from its value.

    ``published_at`` is ``None`` when the product does not carry one. It is never
    filled from the poll time: a response with no publication timestamp would
    then report zero source-to-market lag for exactly the observations whose lag
    is unknown.
    """

    station_id: str
    observation_time: datetime
    temperature_f: Decimal | None
    published_at: datetime | None = None
    measurements: dict[str, Any] = field(default_factory=dict)
    raw_payload: bytes = b""
    #: Stable identity of this observation, for revision tracking.
    revision_id: str = ""


@dataclass
class ObservationFetch:
    """What one poll produced, including the reason it produced nothing."""

    station_id: str
    endpoint: str
    records: list[ObservationRecord] = field(default_factory=list)
    success: bool = False
    http_status: int | None = None
    error_type: str = ""
    error_detail: str = ""
    served_from_cache: bool = False
    raw_payload: bytes = b""


class NwsObservationsProvider:
    """Fetches observations for settlement stations. Never caches.

    Deliberately separate from :class:`WeatherThresholdModel`'s client. That one
    is a *forecast* reader with a 900 s cache tuned for a scoring job; sharing it
    would couple research availability to a production cache TTL, and the first
    symptom would be observations timestamped later than the network call that
    produced them.
    """

    name = "nws_observations"

    def __init__(self, client: HttpClient | None = None) -> None:
        self._client = client or HttpClient(
            NWS_API_BASE,
            name="nws_observations",
            rate_per_second=2.0,
            # Zero, not merely unused. A TTL of zero means no code path can
            # return a cached body, so the bypass cannot be lost by a caller
            # forgetting a keyword.
            cache_ttl_seconds=0.0,
            headers={"Accept": "application/geo+json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cache_hits(self) -> int:
        """Must remain zero for research polls. Asserted by the health report."""
        return int(self._client.stats.get("cache_hits", 0))

    async def fetch_recent(
        self, station_id: str, *, limit: int = 24
    ) -> ObservationFetch:
        """Recent observations for one station, newest first from the API.

        ``limit`` covers a station-day at hourly reporting with headroom. The
        endpoint returns a collection, so one poll captures the whole intraday
        path rather than only the latest point - which matters because a
        collector that misses an hour would otherwise lose that hour forever.
        """
        path = f"/stations/{station_id}/observations"
        fetch = ObservationFetch(station_id=station_id, endpoint=path)
        try:
            data = await self._client.get_json(
                path,
                params={"limit": limit},
                use_cache=False,
                allow_404=True,
            )
        except ProviderError as exc:
            fetch.error_type = _classify(exc)
            fetch.error_detail = str(exc)
            log.debug("NWS observations failed for %s: %s", station_id, exc)
            return fetch

        if data is None:
            fetch.error_type = "not_found"
            fetch.error_detail = f"{path} returned 404"
            fetch.http_status = 404
            return fetch
        if not isinstance(data, dict):
            fetch.error_type = "malformed_payload"
            fetch.error_detail = f"expected an object, got {type(data).__name__}"
            return fetch

        fetch.success = True
        fetch.http_status = 200
        fetch.raw_payload = _canonical(data)
        for feature in data.get("features") or []:
            record = _parse_feature(station_id, feature)
            if record is not None:
                fetch.records.append(record)
        # Oldest first, so a consumer writing them in order produces a
        # monotonically increasing available_at sequence per station.
        fetch.records.sort(key=lambda r: r.observation_time)
        return fetch


def _parse_feature(station_id: str, feature: Any) -> ObservationRecord | None:
    """One GeoJSON feature into an observation, or None if it carries no time.

    An observation with no timestamp cannot be placed on the intraday path, and
    placing it anywhere would be a guess that decides whether a decision saw it.
    """
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties")
    if not isinstance(props, dict):
        return None

    observed = parse_ts(props.get("timestamp"))
    if observed is None:
        return None

    temperature = props.get("temperature") or {}
    value = temperature.get("value")
    unit = str(temperature.get("unitCode", "")).lower()
    temperature_f: Decimal | None = None
    if value is not None:
        try:
            reading = float(value)
        except (TypeError, ValueError):
            reading = None
        if reading is not None:
            # The API publishes degC under the wmoUnit vocabulary. Converted
            # explicitly rather than assumed: a unit change would silently move
            # every observation by 30 degrees.
            in_f = celsius_to_fahrenheit(reading) if "degc" in unit else reading
            temperature_f = Decimal(f"{in_f:.4f}")

    # `rawMessage` is the METAR the observation was decoded from and carries its
    # own issue time; where present it is the closest thing this product has to
    # a publication timestamp. Absent, published_at stays None.
    published_at = parse_ts(props.get("rawMessageTimestamp")) or None

    keep = (
        "dewpoint", "windSpeed", "windDirection", "barometricPressure",
        "relativeHumidity", "heatIndex", "maxTemperatureLast24Hours",
        "textDescription", "rawMessage",
    )
    measurements = {k: props.get(k) for k in keep if props.get(k) is not None}

    return ObservationRecord(
        station_id=station_id,
        observation_time=observed,
        temperature_f=temperature_f,
        published_at=published_at,
        measurements=measurements,
        raw_payload=_canonical(feature),
        # Station plus observation instant. A corrected reading for the same
        # instant arrives as a new row with a later available_at, and this is
        # what links the two.
        revision_id=f"{station_id}:{observed.isoformat()}",
    )


def _canonical(payload: Any) -> bytes:
    """Canonical JSON bytes, so an unchanged response hashes identically.

    ``sort_keys`` is what makes deduplication work in the payload store and what
    makes "the source did not change" a checkable statement rather than an
    artefact of dict ordering.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _classify(exc: Exception) -> str:
    """Failure modes as a closed vocabulary, so they can be counted.

    "Collection failed 40 times" is not actionable. "Collection failed 40 times
    with rate_limited" names the fix.
    """
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "429" in text or "rate" in text:
        return "rate_limited"
    if "circuit" in text:
        return "circuit_open"
    for code in ("500", "502", "503", "504"):
        if code in text:
            return "http_5xx"
    if "404" in text:
        return "not_found"
    if "connect" in text or "dns" in text or "resolve" in text:
        return "connection"
    return "provider_error"


# --------------------------------------------------------------------- forecasts
#
# The observations above answer "what actually happened". This answers "what did
# NWS say would happen, and WHEN did they say it" - and the second half is the
# whole source-lag hypothesis. `updateTime` is the only field in either product
# that records a publication instant, so it is the reason this collector exists
# separately from the probability model's forecast reader.


@dataclass
class ForecastFetch:
    """One gridpoint forecast poll, with its provenance kept apart from its value."""

    station_id: str
    grid_endpoint: str
    endpoint: str
    #: Forecast daily maximum in F, by the local date the interval covers.
    max_temperature_f: dict[str, Decimal] = field(default_factory=dict)
    published_at: datetime | None = None
    source_generated_at: datetime | None = None
    forecast_valid_start: datetime | None = None
    forecast_valid_end: datetime | None = None
    success: bool = False
    http_status: int | None = None
    error_type: str = ""
    error_detail: str = ""
    served_from_cache: bool = False
    raw_payload: bytes = b""

    @property
    def has_publication_time(self) -> bool:
        return self.published_at is not None


class NwsForecastProvider:
    """Gridpoint forecasts for settlement stations. Never caches.

    Separate from :class:`WeatherThresholdModel`'s client for the same reason the
    observations provider is: that one carries a 900 s cache sized for a
    two-hourly scoring job, and a cached body stamped with a fresh
    ``available_at`` is a fabricated availability rather than a stale number.
    """

    name = "nws_forecast"

    def __init__(self, client: HttpClient | None = None) -> None:
        self._client = client or HttpClient(
            NWS_API_BASE,
            name="nws_forecast_research",
            rate_per_second=2.0,
            cache_ttl_seconds=0.0,
            headers={"Accept": "application/geo+json"},
        )
        self._grid_cache: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cache_hits(self) -> int:
        return int(self._client.stats.get("cache_hits", 0))

    async def resolve_grid(self, latitude: float, longitude: float) -> str | None:
        """Coordinate to gridpoint endpoint.

        Cached in-process for the life of the provider: the mapping changes only
        when NWS re-tiles a region, and re-resolving it on every poll would
        double the request count for a value that does not move. This is a
        *topology* cache, not a data cache - no forecast body is reused.
        """
        key = f"{latitude:.4f},{longitude:.4f}"
        if key in self._grid_cache:
            return self._grid_cache[key]
        try:
            data = await self._client.get_json(
                f"/points/{key}", use_cache=False, allow_404=True
            )
        except ProviderError as exc:
            log.debug("NWS points lookup failed for %s: %s", key, exc)
            return None
        if not isinstance(data, dict):
            return None
        props = data.get("properties") or {}
        office, grid_x, grid_y = (
            props.get("gridId"), props.get("gridX"), props.get("gridY")
        )
        if not office or grid_x is None or grid_y is None:
            return None
        path = f"/gridpoints/{office}/{grid_x},{grid_y}"
        self._grid_cache[key] = path
        return path

    async def fetch_forecast(
        self, station_id: str, latitude: float, longitude: float
    ) -> ForecastFetch:
        grid = await self.resolve_grid(latitude, longitude)
        fetch = ForecastFetch(
            station_id=station_id, grid_endpoint=grid or "", endpoint=grid or "/points"
        )
        if grid is None:
            fetch.error_type = "grid_unresolved"
            fetch.error_detail = f"no grid endpoint for {latitude},{longitude}"
            return fetch

        try:
            data = await self._client.get_json(grid, use_cache=False, allow_404=True)
        except ProviderError as exc:
            fetch.error_type = _classify(exc)
            fetch.error_detail = str(exc)
            return fetch
        if data is None:
            fetch.error_type, fetch.http_status = "not_found", 404
            fetch.error_detail = f"{grid} returned 404"
            return fetch
        if not isinstance(data, dict):
            fetch.error_type = "malformed_payload"
            fetch.error_detail = f"expected an object, got {type(data).__name__}"
            return fetch

        fetch.success = True
        fetch.http_status = 200
        fetch.raw_payload = _canonical(data)
        props = data.get("properties") or {}

        # No `or utcnow()`. A missing publication time stays missing: defaulting
        # it would report zero source-to-market lag for exactly the observations
        # whose lag is unknown.
        fetch.published_at = parse_ts(props.get("updateTime"))
        fetch.source_generated_at = parse_ts(props.get("generatedAt"))

        block = props.get("maxTemperature") or {}
        is_celsius = "degc" in str(block.get("uom", "")).lower()
        for entry in block.get("values") or []:
            valid = str(entry.get("validTime", ""))
            start = parse_ts(valid.split("/")[0]) if valid else None
            raw = entry.get("value")
            if start is None or raw is None:
                continue
            try:
                reading = float(raw)
            except (TypeError, ValueError):
                continue
            in_f = celsius_to_fahrenheit(reading) if is_celsius else reading
            fetch.max_temperature_f[start.date().isoformat()] = Decimal(f"{in_f:.4f}")
            if fetch.forecast_valid_start is None:
                fetch.forecast_valid_start = start
                fetch.forecast_valid_end = (
                    parse_ts(valid.split("/")[1]) if "/" in valid else None
                )
        return fetch
