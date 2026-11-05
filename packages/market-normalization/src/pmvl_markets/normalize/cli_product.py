"""Parsing the NWS Climatological Report (CLI), the quantity these contracts settle on.

Kalshi's high-temperature markets settle on the daily maximum published in this
product, so this is the settlement truth. It is a fixed-width text bulletin, and
two properties of it decide whether a parsed value may be used as a label.

**A CLI is issued twice a day and only one of them is final.** The afternoon
issuance carries a line like ``VALID TODAY AS OF 0500 PM LOCAL TIME`` and reports
the maximum *so far*; the early-morning issuance covers the completed previous
day and carries no such line. Treating a preliminary maximum as settlement truth
biases every label downward, and does so precisely on the hot afternoons where a
threshold contract is most likely to be near its strike.

**The summary date is not the issuance date.** The product issued at 06:14 UTC on
August 26 is the summary for August 25. Reading the issuance date as the
observation date shifts every label by one day, which is undetectable in
aggregate statistics and fatal to a per-day join.

Both are parsed explicitly and neither is inferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

#: "...THE PHILADELPHIA PA CLIMATE SUMMARY FOR AUGUST 25 2026..."
_SUMMARY_FOR = re.compile(
    r"CLIMATE\s+SUMMARY\s+FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})", re.I
)
#: The partial-day marker. Offices word it differently and the difference is not
#: cosmetic: Mount Holly writes "VALID TODAY AS OF 0500 PM LOCAL TIME" while
#: Austin/San Antonio writes "VALID AS OF 0400 PM LOCAL TIME" with no "TODAY".
#:
#: Requiring the word "TODAY" silently classified every Austin afternoon bulletin
#: as final, which would have labelled contracts with the maximum *so far* -
#: worst on hot afternoons, exactly where a threshold contract sits near its
#: strike. "TODAY" is therefore optional, and a report is final only when no
#: validity qualifier appears at all.
_VALID_AS_OF = re.compile(
    r"^\s*VALID\s+(?:TODAY\s+)?AS\s+OF\s+(.+?)\s*\.?\s*$", re.I | re.M
)
#: "  MAXIMUM         86   3:33 PM 100    1948  85      1       80"
#:
#: The value may carry a single-letter CLI flag with no separating space -
#: "85R" when the reading tied or broke a record, "E" when estimated. Requiring
#: a word boundary after the digits rejected every record-setting day, which is
#: a systematic loss concentrated on exactly the extreme readings a temperature
#: threshold market is about.
#:
#: "M" means the value is missing. It matches no digits here, so the parse
#: fails and the caller records a missing observation - never a substituted one.
_MAXIMUM = re.compile(r"^\s*MAXIMUM\s+(-?\d+)[A-Z]?\s", re.I | re.M)
_MINIMUM = re.compile(r"^\s*MINIMUM\s+(-?\d+)[A-Z]?\s", re.I | re.M)
#: The product identifier line, e.g. "CLIPHL".
_PRODUCT_CODE = re.compile(r"^\s*(CLI[A-Z]{3})\s*$", re.I | re.M)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER "
        "OCTOBER NOVEMBER DECEMBER".split()
    )
}


class CliParseError(ValueError):
    """The product could not be read well enough to be trusted as a label."""


@dataclass(frozen=True)
class CliDailyClimate:
    """One CLI report, parsed. Settlement truth when ``is_final``."""

    product_code: str
    #: The day the report summarises, read from the summary line - never the
    #: issuance date.
    observation_date: date
    daily_max_f: Decimal | None
    daily_min_f: Decimal | None
    #: False when the report says it is valid only as of some intraday time.
    is_final: bool
    #: The verbatim "as of" text when preliminary, for the audit trail.
    valid_as_of: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_code": self.product_code,
            "observation_date": self.observation_date.isoformat(),
            "daily_max_f": str(self.daily_max_f) if self.daily_max_f is not None else None,
            "daily_min_f": str(self.daily_min_f) if self.daily_min_f is not None else None,
            "is_final": self.is_final,
            "valid_as_of": self.valid_as_of,
        }


def parse_cli_product(text: str) -> CliDailyClimate:
    """Read one CLI bulletin.

    Raises rather than returning a partial record. A CLI whose maximum cannot be
    located is not a report with a missing field - it is a report this parser did
    not understand, and the caller must not be handed something that looks like
    an observation.
    """
    if not text or not text.strip():
        raise CliParseError("empty product text")

    code_match = _PRODUCT_CODE.search(text)
    if code_match is None:
        raise CliParseError("no CLIxxx product identifier line")
    product_code = code_match.group(1).upper()

    summary = _SUMMARY_FOR.search(text)
    if summary is None:
        raise CliParseError(f"{product_code}: no 'CLIMATE SUMMARY FOR <date>' line")
    month_name, day_s, year_s = summary.groups()
    month = _MONTHS.get(month_name.upper())
    if month is None:
        raise CliParseError(f"{product_code}: unrecognised month {month_name!r}")
    try:
        observation_date = date(int(year_s), month, int(day_s))
    except ValueError as exc:
        raise CliParseError(f"{product_code}: bad summary date: {exc}") from exc

    as_of = _VALID_AS_OF.search(text)
    is_final = as_of is None

    max_match = _MAXIMUM.search(text)
    if max_match is None:
        # Never fall back to an hourly-derived maximum. A CLI without a maximum
        # is a missing settlement observation, and substituting a proxy here is
        # exactly the shortcut this whole layer exists to prevent.
        raise CliParseError(f"{product_code}: no MAXIMUM row")
    min_match = _MINIMUM.search(text)

    return CliDailyClimate(
        product_code=product_code,
        observation_date=observation_date,
        daily_max_f=Decimal(max_match.group(1)),
        daily_min_f=Decimal(min_match.group(1)) if min_match else None,
        is_final=is_final,
        valid_as_of="" if as_of is None else as_of.group(1).strip(),
    )


#: Kalshi settlement station -> the CLI location code used by the products API.
#: Explicit, because a station identifier and a CLI location are different
#: namespaces that merely look similar (KMDW -> MDW is a coincidence of naming,
#: KNYC -> NYC is not derivable in general).
STATION_TO_CLI_LOCATION: dict[str, str] = {
    "KNYC": "NYC",
    "KPHL": "PHL",
    "KMDW": "MDW",
    "KLAX": "LAX",
    "KMIA": "MIA",
    "KAUS": "AUS",
    "KDEN": "DEN",
}


def cli_location_for(station_id: str) -> str | None:
    """The CLI location for a station, or None. Never guesses by truncation."""
    return STATION_TO_CLI_LOCATION.get(station_id.upper())


def expected_product_code(station_id: str) -> str | None:
    location = cli_location_for(station_id)
    return f"CLI{location}" if location else None
