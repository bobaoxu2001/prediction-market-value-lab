"""Which station a Kalshi weather contract actually settles on.

The failure this prevents
-------------------------
A gridpoint forecast covers a ~2.5 km cell. A contract settles on one
instrument's official observation. They are different places, and the gap
between them is a **stable, correctly-signed, entirely fake signal**: a
persistent per-station offset looks exactly like a tradeable divergence, has the
same sign every day, and will survive every robustness check that does not know
to look for it.

This repository has already shipped one. The Los Angeles grid cell forecast 89 °F
while the LAX settlement station forecast 81 °F - an eight-degree "disagreement"
against a market that was pricing the real forecast correctly.

And there is a live one right now. The probability model maps ``"austin"`` to
Camp Mabry (30.3210, -97.7600). The settlement rule says **Austin Bergstrom**.
Those are different airports on opposite sides of the city.

So the mapping is parsed from the venue's own settlement text, deterministically,
and anything the parser does not recognise is **listed as unmapped**. There is no
fallback to a coordinate, no nearest-station guess, and no inference from the
series ticker: a wrong station is worse than no station, because no station
declines to trade while a wrong one trades confidently.

What "deterministic" means here
-------------------------------
Same rules text in, same station out, with no clock, no network and no ordering
dependence. Patterns are matched longest-phrase-first so "Chicago Midway" cannot
lose to a bare "Chicago", and the matched phrase is returned as provenance so a
reviewer can see *why* a market was mapped rather than only *that* it was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: Series tickers that make up the weather research universe.
#:
#: ``category == 'weather'`` is far wider than this - it includes CO2
#: concentration, earthquake and global-warming contracts, none of which settle
#: on a station observation. Scoping the universe by series rather than category
#: is what stops a supervolcano market from being counted as an unmapped station.
RESEARCH_SERIES_PREFIX = "KXHIGH"


class MappingMethod(str, Enum):
    """How a station was arrived at. Provenance, not decoration."""

    #: An explicit phrase in the venue's own settlement rules matched.
    SETTLEMENT_RULES_PHRASE = "settlement_rules_phrase"
    #: Nothing matched. The market is unmapped and stays that way.
    UNMAPPED = "unmapped"


class UnmappedReason(str, Enum):
    """Why a market has no station. A closed vocabulary, so gaps can be counted."""

    NO_RULES_TEXT = "no_rules_text"
    NO_KNOWN_PHRASE = "no_known_phrase"
    AMBIGUOUS_PHRASES = "ambiguous_phrases"
    NOT_RESEARCH_SERIES = "not_research_series"


@dataclass(frozen=True)
class StationMapping:
    """The result of parsing one market's settlement rules."""

    station_id: str
    method: MappingMethod
    #: The exact phrase that matched, so a reviewer can check the decision.
    matched_phrase: str = ""
    reason: UnmappedReason | None = None
    #: False when the venue's wording names a city rather than an instrument.
    #: Still mapped - the station is not in doubt - but flagged, because a
    #: vaguer rule is one the venue could later resolve differently.
    exact: bool = True

    @property
    def mapped(self) -> bool:
        return self.method is not MappingMethod.UNMAPPED

    def as_dict(self) -> dict[str, object]:
        return {
            "station_id": self.station_id,
            "method": self.method.value,
            "matched_phrase": self.matched_phrase,
            "reason": self.reason.value if self.reason else None,
            "exact": self.exact,
        }


UNMAPPED = StationMapping(
    station_id="", method=MappingMethod.UNMAPPED, reason=UnmappedReason.NO_KNOWN_PHRASE
)


@dataclass(frozen=True)
class Station:
    """A settlement station and the phrases the venue uses to name it.

    ``latitude``/``longitude`` are recorded for reference only. They are
    deliberately NOT used to map anything: resolving a market by proximity is the
    exact mistake this module exists to prevent.
    """

    station_id: str
    name: str
    latitude: float
    longitude: float
    #: Phrases that appear in Kalshi's settlement text, verbatim and lowercased.
    phrases: tuple[str, ...]
    #: Phrases that name a city rather than the instrument. Mapped, but flagged
    #: inexact so a reviewer knows the venue's wording left room.
    loose_phrases: tuple[str, ...] = ()
    #: The key this station has in the probability model's own coordinate table.
    #:
    #: Declared, not inferred. An audit that matched model tokens to stations by
    #: name similarity or by proximity would be making exactly the guess it
    #: exists to catch - and a proximity match lets a WRONG coordinate select
    #: itself as correct. Empty means the model genuinely has no entry.
    model_token: str = ""


#: The seven stations the KXHIGH* board settles on.
#:
#: Every phrase below is copied from real captured settlement text, not invented.
#: Note KAUS: the rules say "Austin Bergstrom", while the probability model has
#: long used Camp Mabry's coordinates. That mismatch is the reason this table is
#: keyed on the venue's words rather than on the model's assumptions.
#: Kalshi re-worded the weather board in August 2026 and the parser caught it.
#:
#: Old form (July): "the highest temperature recorded in Central Park, New York
#: ... as reported by the National Weather Service's Climatological Report
#: (Daily)".
#:
#: New form (August): "the maximum temperature recorded at New York City
#: (CLINYC) ... according to The Weather Company".
#:
#: Two changes, and the second matters to the research premise. The station is
#: now named by its **NWS CLI product code**, which is more precise than the
#: prose. But the named settlement *source* moved from the NWS directly to The
#: Weather Company as vendor. The underlying value is still the NWS
#: Climatological Report - CLI codes are NWS product identifiers - but it now
#: reaches the venue through a third party with its own timing and revision
#: behaviour. That is a real weakening of "NWS is the same source Kalshi settles
#: on", which is the assumption that made weather the first research universe.
#:
#: This is exactly why the parser refuses instead of falling back to the series
#: ticker. A ticker-keyed default would have kept mapping KXHIGHAUS to KAUS and
#: nobody would have noticed the settlement source had changed at all.
STATIONS: tuple[Station, ...] = (
    Station(
        "KNYC", "Central Park, New York", 40.7794, -73.9692,
        phrases=("clinyc", "central park, new york", "central park"),
        model_token="new york city",
    ),
    Station(
        "KLAX", "Los Angeles International Airport", 33.9425, -118.4081,
        phrases=("clilax", "los angeles airport", "los angeles international airport"),
        model_token="los angeles",
    ),
    Station(
        "KMDW", "Chicago Midway", 41.7860, -87.7524,
        phrases=("climdw", "chicago midway",),
        model_token="chicago",
    ),
    Station(
        "KMIA", "Miami International Airport", 25.7906, -80.3164,
        phrases=("climia", "miami international airport",),
        model_token="miami",
    ),
    Station(
        "KAUS", "Austin-Bergstrom International Airport", 30.1975, -97.6664,
        phrases=("cliaus", "austin bergstrom", "austin-bergstrom"),
        model_token="austin",
    ),
    Station(
        "KPHL", "Philadelphia International Airport", 39.8683, -75.2311,
        phrases=("cliphl", "philadelphia international airport",),
        model_token="philadelphia",
    ),
    Station(
        # The rule text says only "Denver, CO". Kalshi's KXHIGHDEN board settles
        # on Denver International, but the wording names a city, so the mapping
        # is marked inexact rather than presented as certain.
        "KDEN", "Denver International Airport", 39.8467, -104.6564,
        phrases=("cliden", "denver international airport",),
        loose_phrases=("denver, co",),
        model_token="denver",
    ),
)

STATION_BY_ID: dict[str, Station] = {s.station_id: s for s in STATIONS}

#: (phrase, station_id, exact) sorted longest-first.
#:
#: Longest-first is load-bearing. "Chicago Midway" must beat a bare "Chicago",
#: and "los angeles international airport" must beat "los angeles airport", or
#: the mapping depends on table ordering rather than on the text.
_PHRASES: tuple[tuple[str, str, bool], ...] = tuple(
    sorted(
        [(p, s.station_id, True) for s in STATIONS for p in s.phrases]
        + [(p, s.station_id, False) for s in STATIONS for p in s.loose_phrases],
        key=lambda item: (-len(item[0]), item[0]),
    )
)

#: Collapse runs of whitespace so a line break inside the rules cannot hide a
#: phrase. Nothing else about the text is altered.
_WHITESPACE = re.compile(r"\s+")


def normalize_rules_text(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "")).strip().lower()


def parse_settlement_station(
    rules_text: str, *, series_ticker: str | None = None
) -> StationMapping:
    """Map settlement rules to a station id, or refuse.

    Pure: no clock, no network, no database. The same text always yields the same
    answer, which is what lets a mapping be re-derived and checked later rather
    than trusted because it is already in a column.
    """
    if series_ticker is not None and not series_ticker.startswith(
        RESEARCH_SERIES_PREFIX
    ):
        return StationMapping(
            station_id="",
            method=MappingMethod.UNMAPPED,
            reason=UnmappedReason.NOT_RESEARCH_SERIES,
        )

    haystack = normalize_rules_text(rules_text)
    if not haystack:
        return StationMapping(
            station_id="",
            method=MappingMethod.UNMAPPED,
            reason=UnmappedReason.NO_RULES_TEXT,
        )

    matches: list[tuple[str, str, bool]] = []
    for phrase, station_id, exact in _PHRASES:
        if phrase in haystack:
            # A longer phrase already resolved to this station; the shorter one
            # is the same finding, not a second one.
            if any(station_id == s for _, s, _ in matches):
                continue
            matches.append((phrase, station_id, exact))

    if not matches:
        return StationMapping(
            station_id="",
            method=MappingMethod.UNMAPPED,
            reason=UnmappedReason.NO_KNOWN_PHRASE,
        )
    if len({station_id for _, station_id, _ in matches}) > 1:
        # Two different stations named in one rule. Refusing is correct: picking
        # the first would be an arbitrary choice recorded as a fact.
        return StationMapping(
            station_id="",
            method=MappingMethod.UNMAPPED,
            reason=UnmappedReason.AMBIGUOUS_PHRASES,
        )

    phrase, station_id, exact = matches[0]
    return StationMapping(
        station_id=station_id,
        method=MappingMethod.SETTLEMENT_RULES_PHRASE,
        matched_phrase=phrase,
        exact=exact,
    )


#: Settlement source vendors the venue has named, normalised.
#:
#: Recorded rather than judged. Whether a vendor relay of the NWS CLI product is
#: timing-equivalent to NWS direct is a research question, and answering it needs
#: the change to have been observable when it happened.
SOURCE_VENDORS: tuple[tuple[str, str], ...] = (
    ("the weather company", "the_weather_company"),
    ("national weather service", "national_weather_service"),
    ("climatological report", "national_weather_service"),
)

#: NWS Climatological Report product codes, e.g. "CLINYC".
_PRODUCT_CODE = re.compile(r"\b(cli[a-z]{3})\b")


@dataclass(frozen=True)
class SettlementProvenance:
    """What the venue says settles this contract, parsed deterministically.

    Three separable facts that the prose runs together: which station, which
    NWS product, and who relays it. They change independently - Kalshi kept the
    station and the product in August 2026 and changed the relay - so recording
    them apart is what makes such a change visible instead of buried in a diff
    nobody reads.
    """

    station_id: str
    product_code: str
    source_vendor: str
    rules_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            "station_id": self.station_id,
            "product_code": self.product_code,
            "source_vendor": self.source_vendor,
            "rules_hash": self.rules_hash,
        }


def parse_settlement_provenance(
    rules_text: str, *, series_ticker: str | None = None
) -> SettlementProvenance:
    """Station, product code and named vendor, plus a hash of the text they came from.

    The hash is over the normalised rules so a re-wording is detectable even when
    every parsed field is unchanged - which is exactly the August 2026 case for
    the station and product, and exactly what a later equivalence audit needs to
    date the change from.
    """
    import hashlib

    normalized = normalize_rules_text(rules_text)
    mapping = parse_settlement_station(rules_text, series_ticker=series_ticker)

    code_match = _PRODUCT_CODE.search(normalized)
    product_code = code_match.group(1).upper() if code_match else ""

    vendor = ""
    for phrase, normalised_vendor in SOURCE_VENDORS:
        if phrase in normalized:
            vendor = normalised_vendor
            break

    return SettlementProvenance(
        station_id=mapping.station_id,
        product_code=product_code,
        source_vendor=vendor,
        rules_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32],
    )


def is_research_market(series_ticker: str | None, category: str | None = None) -> bool:
    """Whether a market belongs to the weather research universe.

    Keyed on the series, not the category. ``category == 'weather'`` also holds
    CO2-concentration, earthquake and global-warming contracts, none of which
    settle on a station observation - counting them as unmapped stations would
    make the coverage gate permanently unachievable for the wrong reason.
    """
    return bool(series_ticker and series_ticker.startswith(RESEARCH_SERIES_PREFIX))


def research_station_ids() -> tuple[str, ...]:
    """Stations the observation collector must poll. Sorted, for determinism."""
    return tuple(sorted(STATION_BY_ID))


def station_for(station_id: str) -> Station | None:
    return STATION_BY_ID.get(station_id)


def model_coordinate_disagreements(
    model_stations: dict[str, tuple[float, float]],
    *,
    tolerance_degrees: float = 0.05,
) -> list[str]:
    """Where the probability model's coordinates disagree with the settlement station.

    A reporting helper, not a fix. The model's ``_STATIONS`` table is keyed on
    city tokens and was populated independently of the venue's settlement text;
    this surfaces the gaps rather than silently reconciling them, because
    changing a forecast location is a modelling decision and not a data-cleaning
    one.
    """
    problems: list[str] = []
    by_city = {s.name.split(",")[0].lower(): s for s in STATIONS}
    for token, (lat, lon) in sorted(model_stations.items()):
        for city, station in by_city.items():
            if token not in city and city.split()[0] not in token:
                continue
            if (
                abs(lat - station.latitude) > tolerance_degrees
                or abs(lon - station.longitude) > tolerance_degrees
            ):
                problems.append(
                    f"model {token!r} at ({lat:.4f},{lon:.4f}) is not "
                    f"{station.station_id} {station.name} at "
                    f"({station.latitude:.4f},{station.longitude:.4f})"
                )
            break
    return problems
