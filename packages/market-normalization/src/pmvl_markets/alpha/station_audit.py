"""Do all five representations of a station agree?

A weather contract's location is described in five independent places, and every
one of them was populated by a different person at a different time for a
different reason:

1. the **probability model's** coordinate table (``_STATIONS``, keyed on city
   tokens, hand-entered);
2. ``markets.settlement_station_id`` (parsed from the venue's rules);
3. the **settlement rule text** itself (the venue's own words);
4. the **forecast gridpoint** the model resolves those coordinates to;
5. the **observation endpoint** the station collector polls.

They are supposed to describe one place. When they do not, the gap is a
**stable, correctly-signed, entirely fake signal**: a persistent per-station
offset has the same sign every day, survives every robustness check that does
not know to look for it, and reads as a tradeable divergence.

Two are already known:

* Los Angeles, fixed earlier - the grid cell forecast 89 °F against a settlement
  station forecasting 81 °F;
* Austin, still open - the model uses Camp Mabry, the rules say Bergstrom.

Fixing Austin alone would be the wrong response, because the question is not
"is Austin wrong" but "which of the seven disagree, and on which axis". This
module answers that for all of them, deterministically.

What this does NOT do
---------------------
It does not calibrate a bias, adjust a forecast, or change any statistical
model. A disagreement is reported with its magnitude and left for a human: which
coordinate a *forecast* should use is a modelling decision, and only the
settlement station is a matter of fact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from ..normalize.weather_stations import (
    STATIONS,
    Station,
    parse_settlement_provenance,
    parse_settlement_station,
)


class Axis(str, Enum):
    """Which of the five representations disagreed."""

    MODEL_COORDINATE = "model_coordinate"
    PERSISTED_STATION_ID = "persisted_station_id"
    SETTLEMENT_RULE_TEXT = "settlement_rule_text"
    FORECAST_GRIDPOINT = "forecast_gridpoint"
    OBSERVATION_ENDPOINT = "observation_endpoint"


class Severity(str, Enum):
    """How much a disagreement matters.

    ``FACTUAL`` is a plain error: the repository's own semantics say these must
    match and they do not. Safe to correct.

    ``MODELLING`` is a choice: the forecast could legitimately use a different
    point from the settlement instrument, and deciding is not a data-cleaning
    task. Reported and left alone.
    """

    FACTUAL = "factual"
    MODELLING = "modelling"
    INFORMATIONAL = "informational"


#: Kilometres of separation below which two coordinates are the same place.
#:
#: An NWS gridpoint cell is ~2.5 km, so anything inside that resolves to the
#: same forecast and is not a disagreement worth a human's attention.
SAME_PLACE_KM = 2.5


@dataclass(frozen=True)
class Disagreement:
    """One axis on which a station's representations differ."""

    station_id: str
    axis: Axis
    severity: Severity
    expected: str
    actual: str
    detail: str
    distance_km: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "axis": self.axis.value,
            "severity": self.severity.value,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
            "distance_km": (
                round(self.distance_km, 2) if self.distance_km is not None else None
            ),
        }


@dataclass
class StationAudit:
    """Every representation of one station, side by side."""

    station: Station
    model_token: str = ""
    model_coordinate: tuple[float, float] | None = None
    persisted_station_ids: tuple[str, ...] = ()
    rule_phrases: tuple[str, ...] = ()
    product_codes: tuple[str, ...] = ()
    vendors: tuple[str, ...] = ()
    forecast_gridpoints: tuple[str, ...] = ()
    observation_endpoint: str = ""
    markets: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)

    @property
    def agrees(self) -> bool:
        return not self.disagreements

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station.station_id,
            "name": self.station.name,
            "settlement_coordinate": [
                self.station.latitude,
                self.station.longitude,
            ],
            "model_token": self.model_token,
            "model_coordinate": (
                list(self.model_coordinate) if self.model_coordinate else None
            ),
            "persisted_station_ids": list(self.persisted_station_ids),
            "rule_phrases": list(self.rule_phrases),
            "product_codes": list(self.product_codes),
            "settlement_vendors": list(self.vendors),
            "forecast_gridpoints": list(self.forecast_gridpoints),
            "observation_endpoint": self.observation_endpoint,
            "markets": self.markets,
            "agrees": self.agrees,
            "disagreements": [d.as_dict() for d in self.disagreements],
        }


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance. Used to size a disagreement, never to resolve one."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _model_token_for(
    station: Station, model_stations: Mapping[str, tuple[float, float]]
) -> str:
    """The model's key for this station, from the station's own declaration.

    Read off ``Station.model_token`` rather than inferred. The first version of
    this matched on name similarity and reported KNYC's coordinate as *absent* -
    the model has it, under the token "new york city", which does not resemble
    "Central Park". A fuzzy matcher in an audit whose whole subject is silent
    mismatches is the wrong tool: it manufactures findings and hides real ones.

    Proximity matching would be worse still, because it lets a wrong coordinate
    select itself as the right one.
    """
    token = station.model_token
    return token if token and token in model_stations else ""


def audit_stations(
    *,
    model_stations: Mapping[str, tuple[float, float]],
    market_rows: Sequence[tuple[str, str, str, str]] = (),
    observed_gridpoints: Mapping[str, Sequence[str]] | None = None,
) -> list[StationAudit]:
    """Compare all five representations for every research station.

    ``market_rows`` is ``(series_ticker, settlement_station_id, rules_raw,
    platform_market_id)`` straight from the database, so the audit sees what was
    actually persisted rather than re-deriving it and agreeing with itself.
    """
    observed_gridpoints = observed_gridpoints or {}
    audits: list[StationAudit] = []

    by_station: dict[str, list[tuple[str, str, str, str]]] = {}
    for row in market_rows:
        mapping = parse_settlement_station(row[2], series_ticker=row[0])
        if mapping.mapped:
            by_station.setdefault(mapping.station_id, []).append(row)

    for station in STATIONS:
        rows = by_station.get(station.station_id, [])
        token = _model_token_for(station, model_stations)
        coordinate = model_stations.get(token) if token else None

        phrases, codes, vendors, persisted = set(), set(), set(), set()
        for series, persisted_id, rules, _market in rows:
            mapping = parse_settlement_station(rules, series_ticker=series)
            provenance = parse_settlement_provenance(rules, series_ticker=series)
            if mapping.matched_phrase:
                phrases.add(mapping.matched_phrase)
            if provenance.product_code:
                codes.add(provenance.product_code)
            if provenance.source_vendor:
                vendors.add(provenance.source_vendor)
            persisted.add(persisted_id or "")

        audit = StationAudit(
            station=station,
            model_token=token,
            model_coordinate=coordinate,
            persisted_station_ids=tuple(sorted(persisted)),
            rule_phrases=tuple(sorted(phrases)),
            product_codes=tuple(sorted(codes)),
            vendors=tuple(sorted(vendors)),
            forecast_gridpoints=tuple(sorted(observed_gridpoints.get(station.station_id, ()))),
            observation_endpoint=f"/stations/{station.station_id}/observations",
            markets=len(rows),
        )

        # Axis 1: the model's forecast coordinate vs the settlement instrument.
        if coordinate is None:
            audit.disagreements.append(
                Disagreement(
                    station.station_id, Axis.MODEL_COORDINATE, Severity.FACTUAL,
                    expected=f"{station.latitude:.4f},{station.longitude:.4f}",
                    actual="absent",
                    detail=(
                        "the probability model has no coordinate for this "
                        "settlement station, so it cannot forecast the contract "
                        "it would be scoring"
                    ),
                )
            )
        else:
            distance = haversine_km(coordinate, (station.latitude, station.longitude))
            if distance > SAME_PLACE_KM:
                audit.disagreements.append(
                    Disagreement(
                        station.station_id, Axis.MODEL_COORDINATE, Severity.MODELLING,
                        expected=(
                            f"{station.station_id} "
                            f"{station.latitude:.4f},{station.longitude:.4f}"
                        ),
                        actual=f"{token} {coordinate[0]:.4f},{coordinate[1]:.4f}",
                        detail=(
                            f"the model forecasts a point {distance:.1f} km from "
                            "the settlement instrument. A persistent offset "
                            "between two sites is indistinguishable from a "
                            "tradeable divergence"
                        ),
                        distance_km=distance,
                    )
                )

        # Axis 2: what the parser says vs what the column holds.
        wrong = {p for p in persisted if p and p != station.station_id}
        if wrong:
            audit.disagreements.append(
                Disagreement(
                    station.station_id, Axis.PERSISTED_STATION_ID, Severity.FACTUAL,
                    expected=station.station_id, actual=", ".join(sorted(wrong)),
                    detail="the persisted column disagrees with the parsed rules",
                )
            )
        if "" in persisted and rows:
            audit.disagreements.append(
                Disagreement(
                    station.station_id, Axis.PERSISTED_STATION_ID, Severity.FACTUAL,
                    expected=station.station_id, actual="empty",
                    detail=(
                        f"{sum(1 for p in persisted if not p)} distinct market(s) "
                        "parse to this station but have an empty column; run "
                        "`pmvl map-stations`"
                    ),
                )
            )

        # Axis 3: the venue changed its own words.
        if len(vendors) > 1:
            audit.disagreements.append(
                Disagreement(
                    station.station_id, Axis.SETTLEMENT_RULE_TEXT,
                    Severity.INFORMATIONAL,
                    expected="one settlement source", actual=", ".join(sorted(vendors)),
                    detail=(
                        "the venue names more than one settlement source across "
                        "this station's contracts. Not an error - a change, and "
                        "one whose timing equivalence is unestablished"
                    ),
                )
            )

        # Axis 4: one station resolving to several forecast cells.
        if len(audit.forecast_gridpoints) > 1:
            audit.disagreements.append(
                Disagreement(
                    station.station_id, Axis.FORECAST_GRIDPOINT, Severity.FACTUAL,
                    expected="one gridpoint",
                    actual=", ".join(audit.forecast_gridpoints),
                    detail=(
                        "one settlement station resolved to multiple forecast "
                        "cells; observations for it are not comparable across time"
                    ),
                )
            )

        audits.append(audit)
    return audits


def summarise(audits: Sequence[StationAudit]) -> dict[str, Any]:
    """The report as plain data."""
    disagreements = [d for a in audits for d in a.disagreements]
    return {
        "scope": (
            "STATION MAPPING AUDIT. Compares the five places a station is "
            "described. Reports disagreements; calibrates nothing and adjusts "
            "no forecast."
        ),
        "stations": len(audits),
        "in_agreement": sum(1 for a in audits if a.agrees),
        "disagreeing": sum(1 for a in audits if not a.agrees),
        "by_severity": {
            severity.value: sum(1 for d in disagreements if d.severity is severity)
            for severity in Severity
        },
        "by_axis": {
            axis.value: sum(1 for d in disagreements if d.axis is axis)
            for axis in Axis
        },
        "audits": [a.as_dict() for a in audits],
    }


def render(report: dict[str, Any]) -> list[str]:
    lines = [
        "=" * 74,
        "SETTLEMENT STATION AUDIT",
        "=" * 74,
        report["scope"],
        "",
        f"{report['in_agreement']} of {report['stations']} stations agree on "
        f"every axis; {report['disagreeing']} do not.",
        "",
    ]
    for audit in report["audits"]:
        mark = "OK  " if audit["agrees"] else "DIFF"
        lines.append(
            f"[{mark}] {audit['station_id']}  {audit['name']}  "
            f"({audit['markets']} markets)"
        )
        lines.append(
            f"        settlement  {audit['settlement_coordinate'][0]:.4f},"
            f"{audit['settlement_coordinate'][1]:.4f}"
        )
        if audit["model_coordinate"]:
            lines.append(
                f"        model       {audit['model_token']} "
                f"{audit['model_coordinate'][0]:.4f},"
                f"{audit['model_coordinate'][1]:.4f}"
            )
        else:
            lines.append("        model       (absent)")
        lines.append(
            f"        rules       {', '.join(audit['rule_phrases']) or '(none)'}"
        )
        lines.append(
            f"        product     {', '.join(audit['product_codes']) or '(none)'}"
            f"   vendor  {', '.join(audit['settlement_vendors']) or '(none)'}"
        )
        lines.append(f"        observations {audit['observation_endpoint']}")
        for d in audit["disagreements"]:
            distance = f" ({d['distance_km']} km)" if d["distance_km"] else ""
            lines.append(
                f"        !! {d['severity'].upper():13s} {d['axis']}{distance}"
            )
            lines.append(f"           expected {d['expected']}")
            lines.append(f"           actual   {d['actual']}")
            lines.append(f"           {d['detail']}")
        lines.append("")
    lines.append(f"by severity: {report['by_severity']}")
    return lines
