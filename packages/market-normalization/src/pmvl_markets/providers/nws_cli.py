"""Collecting the NWS Climatological Report - the settlement truth for these contracts.

Kalshi's high-temperature markets settle on the daily maximum in this bulletin.
Until it is archived, the settled quantity has never been observed and every
contract is a SETTLEMENT_MISMATCH no matter how good the model is.

Like the observations provider, this never caches. A cached body stamped with a
fresh ``available_at`` describes a poll that never reached the network, which is
a fabricated availability rather than a stale number.

Two collection modes exist and they are not interchangeable:

**Live** polling records genuine availability - we learned this value at this
instant, and a point-in-time query may use it.

**Backfill** sweeps the products archive for bulletins issued before this
collector existed. Those values are perfectly good settlement *labels* and are
not evidence that we could have known them at the historical instant they
describe. Rows carry ``is_backfill`` so a PIT query can refuse them while a
labelling query accepts them, and the distinction is structural rather than a
convention someone has to remember.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import ensure_utc, utcnow

from ..normalize.cli_product import (
    CliDailyClimate,
    CliParseError,
    cli_location_for,
    parse_cli_product,
)
from .http import HttpClient

log = get_logger(__name__)

NWS_API_BASE = "https://api.weather.gov"
COLLECTOR_VERSION = "nws-cli-v1.0.0"
PARSER_VERSION = "cli-parser-v1.0.0"


@dataclass
class CliFetch:
    """One parsed bulletin plus the timing that makes it admissible."""

    station_id: str
    climate: CliDailyClimate
    published_at: datetime | None
    available_at: datetime
    source_product_id: str
    source_endpoint: str
    raw_payload_hash: str
    raw_text: str
    is_backfill: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            **self.climate.as_dict(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "available_at": self.available_at.isoformat(),
            "source_product_id": self.source_product_id,
            "raw_payload_hash": self.raw_payload_hash,
            "is_backfill": self.is_backfill,
        }


@dataclass
class CliReport:
    """What one collection pass did, including what it refused."""

    fetched: int = 0
    parsed: int = 0
    finals: int = 0
    preliminary: int = 0
    parse_failures: list[str] = field(default_factory=list)
    unmapped_stations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "parsed": self.parsed,
            "finals": self.finals,
            "preliminary": self.preliminary,
            "parse_failures": self.parse_failures[:10],
            "unmapped_stations": self.unmapped_stations,
            "errors": self.errors[:10],
        }


class NwsCliProvider:
    """Fetches CLI bulletins for settlement stations. Never caches."""

    name = "nws_cli"

    def __init__(self, client: HttpClient | None = None) -> None:
        self._client = client or HttpClient(
            NWS_API_BASE,
            name="nws_cli",
            rate_per_second=2.0,
            # Zero, not merely unused - see the module docstring.
            cache_ttl_seconds=0.0,
            headers={"Accept": "application/ld+json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cache_hits(self) -> int:
        return int(self._client.stats.get("cache_hits", 0))

    async def fetch_recent(
        self, station_id: str, *, limit: int = 4, is_backfill: bool = False
    ) -> tuple[list[CliFetch], CliReport]:
        """Recent bulletins for one station, newest first.

        ``limit`` bounds how far back a pass reaches. The default covers roughly
        two days of twice-daily issuance, enough to pick up a late revision
        without re-reading the archive on every tick.
        """
        report = CliReport()
        location = cli_location_for(station_id)
        if location is None:
            # No fuzzy matching. A station without an explicit CLI location is
            # unsupported, not approximately supported.
            report.unmapped_stations.append(station_id)
            return [], report

        listing_path = f"/products/types/CLI/locations/{location}"
        try:
            listing = await self._client.get_json(listing_path, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{station_id}: listing failed: {exc}")
            return [], report

        entries = (listing or {}).get("@graph") or []
        out: list[CliFetch] = []
        for entry in entries[:limit]:
            product_id = str(entry.get("id") or "")
            if not product_id:
                continue
            path = f"/products/{product_id}"
            try:
                body = await self._client.get_json(path, use_cache=False)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{station_id}/{product_id}: {exc}")
                continue
            # Stamped the instant the response is in hand, before parsing. The
            # parse is our work, not the source's, and charging its duration to
            # availability would misdate every row by a variable amount.
            available_at = utcnow()
            report.fetched += 1

            text = str((body or {}).get("productText") or "")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            try:
                climate = parse_cli_product(text)
            except CliParseError as exc:
                # A bulletin we cannot read is a missing observation, never an
                # opportunity to fall back to an hourly-derived maximum.
                report.parse_failures.append(f"{station_id}/{product_id}: {exc}")
                continue

            published_raw = (body or {}).get("issuanceTime") or entry.get("issuanceTime")
            published_at = None
            if published_raw:
                try:
                    published_at = ensure_utc(datetime.fromisoformat(str(published_raw)))
                except ValueError:
                    published_at = None

            report.parsed += 1
            if climate.is_final:
                report.finals += 1
            else:
                report.preliminary += 1

            out.append(
                CliFetch(
                    station_id=station_id,
                    climate=climate,
                    published_at=published_at,
                    available_at=available_at,
                    source_product_id=product_id,
                    source_endpoint=path,
                    raw_payload_hash=digest,
                    raw_text=text,
                    is_backfill=is_backfill,
                )
            )
        return out, report
