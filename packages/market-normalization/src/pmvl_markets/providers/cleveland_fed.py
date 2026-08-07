"""The Cleveland Fed's inflation nowcast, and the error history that goes with it.

Backs :mod:`pmvl_markets.probability.categories.economics`. Keyless, like every
other source here.

## Why this and not FRED

The plan was FRED plus this. FRED turned out to be unnecessary for the contracts
that actually exist. The nowcast file carries, for each of 158 target months back
to 2013:

* a **daily** nowcast of CPI, core CPI, PCE and core PCE, and
* the **realised** value once BLS publishes it.

Both halves in one keyless file is what makes the model possible without an
account. It gives the point estimate *and* lets the dispersion be measured from
the nowcast's own track record rather than assumed - which matters, because for a
bucket contract the width of the distribution is most of the answer.

Adding a FRED key anyway would mean a registration step and a secret to manage in
exchange for data already in hand. FRED is still the right source for the releases
this does not cover - payrolls, GDP, unemployment - and none of those have a
nowcast here, so a model for them is a separate piece of work with a separate
dependency, not a reason to take the dependency now.

## Format

The file is a chart payload rather than a data API: a list of one entry per target
month, each with ``chart.subcaption`` naming the month (``"2026-8"``),
``categories`` holding ``MM/DD`` labels, and ``dataset`` holding one series per
measure plus a matching ``Actual ...`` series. The labels carry no year, so it is
reconstructed from the target month - a January label under a December target
belongs to the following year.

It is a public chart asset with no stability guarantee. Every failure degrades to
``None`` and the model then declines.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pmvl_shared.logging_setup import get_logger
from pmvl_shared.timeutil import ensure_utc

from .http import HttpClient, ProviderError

log = get_logger(__name__)

CLEVELAND_FED_BASE = "https://www.clevelandfed.org"
_MONTH_PATH = "/-/media/files/webcharts/inflationnowcasting/nowcast_month.json"
_YEAR_PATH = "/-/media/files/webcharts/inflationnowcasting/nowcast_year.json"


class Measure(StrEnum):
    """Which inflation series a contract settles on."""

    CPI = "CPI Inflation"
    CORE_CPI = "Core CPI Inflation"
    PCE = "PCE Inflation"
    CORE_PCE = "Core PCE Inflation"

    @property
    def actual_series(self) -> str:
        return f"Actual {self.value}"


class Frequency(StrEnum):
    MONTH_OVER_MONTH = "mom"
    YEAR_OVER_YEAR = "yoy"


@dataclass(frozen=True)
class NowcastPoint:
    observed_on: date
    value: float


@dataclass
class TargetMonth:
    """One month's nowcast track and, once published, its realised value."""

    month: date
    nowcasts: dict[Measure, list[NowcastPoint]] = field(default_factory=dict)
    actuals: dict[Measure, NowcastPoint] = field(default_factory=dict)

    def latest_before(
        self, measure: Measure, cutoff: date
    ) -> NowcastPoint | None:
        """The freshest nowcast published on or before ``cutoff``.

        The cutoff is what makes this model replayable: a nowcast made after the
        instant being forecast from is not evidence that was available then.
        """
        points = [p for p in self.nowcasts.get(measure, []) if p.observed_on <= cutoff]
        return max(points, key=lambda p: p.observed_on) if points else None


@dataclass(frozen=True)
class ErrorModel:
    """Empirical dispersion of ``actual - nowcast`` at a given lead time."""

    stdev: float
    sample_size: int
    lead_bucket: str
    mean_error: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "stdev": round(self.stdev, 5),
            "mean_error": round(self.mean_error, 5),
            "sample_size": self.sample_size,
            "lead_bucket": self.lead_bucket,
        }


#: Lead-time buckets in days before publication, and their labels.
#:
#: Bucketed rather than fitted as a smooth function of lead because the nowcast's
#: accuracy improves in steps as the month's own source data arrives, not
#: smoothly, and because a bucket's sample size is legible to a reader in a way a
#: fitted curve's effective sample size is not.
_LEAD_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (0, 7, "0-7d"),
    (8, 14, "8-14d"),
    (15, 30, "15-30d"),
    (31, 45, "31-45d"),
    (46, 10_000, "46d+"),
)

#: Below this many observations a bucket's standard deviation is not usable.
_MIN_ERROR_SAMPLE = 12


def lead_bucket(days: int) -> str:
    for low, high, label in _LEAD_BUCKETS:
        if low <= days <= high:
            return label
    return _LEAD_BUCKETS[-1][2]


class ClevelandFedProvider:
    """Fetches and parses the nowcast chart files."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self._client = client or HttpClient(
            CLEVELAND_FED_BASE,
            name="cleveland_fed",
            rate_per_second=1.0,
            # The file is ~7 MB and updates once a day; re-fetching it per market
            # would be absurd. One hour keeps a long scan on a single copy.
            cache_ttl_seconds=3600.0,
        )
        self._parsed: dict[Frequency, dict[date, TargetMonth] | None] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def target_months(
        self, frequency: Frequency
    ) -> dict[date, TargetMonth] | None:
        if frequency in self._parsed:
            return self._parsed[frequency]

        path = _MONTH_PATH if frequency is Frequency.MONTH_OVER_MONTH else _YEAR_PATH
        try:
            payload = await self._client.get_json(path, allow_404=True)
        except ProviderError as exc:
            log.debug("cleveland fed nowcast unavailable (%s): %s", frequency, exc)
            self._parsed[frequency] = None
            return None

        parsed = parse_nowcast_payload(payload) if isinstance(payload, list) else None
        self._parsed[frequency] = parsed
        return parsed

    async def nowcast(
        self,
        measure: Measure,
        frequency: Frequency,
        month: date,
        *,
        as_of: date,
    ) -> NowcastPoint | None:
        months = await self.target_months(frequency)
        if not months:
            return None
        entry = months.get(date(month.year, month.month, 1))
        return entry.latest_before(measure, as_of) if entry else None

    async def error_model(
        self,
        measure: Measure,
        frequency: Frequency,
        *,
        lead_days: int,
        since: date | None = None,
        before: date | None = None,
        exclude_month: date | None = None,
    ) -> ErrorModel | None:
        """Dispersion of the nowcast's own past errors at this lead time.

        ``since`` restricts the fit to target months from that date onward, which
        is how the caller asks for "the current regime" instead of all history.
        The two answers are far apart and both are true: measured over 2013-2026
        the core CPI nowcast's error is about 0.157pp, and over the last three
        years about 0.113pp. The difference is 2020-21, where single-month errors
        reached 0.78pp.

        ``before`` excludes months whose *actual* was published on or after that
        date. Under retrodiction the whole error history after the evaluation
        instant is future information, and fitting the dispersion on it would be
        look-ahead of a subtler kind than reading the answer - the shape of the
        distribution would already know about the regime it is being asked to
        forecast in.

        ``exclude_month`` drops the month being forecast, which only ever has an
        actual in a retrodiction - and including it would be scoring a forecast
        against a distribution fitted partly on its own answer.
        """
        months = await self.target_months(frequency)
        if not months:
            return None

        bucket = lead_bucket(lead_days)
        errors: list[float] = []
        for entry in months.values():
            if since is not None and entry.month < since:
                continue
            if exclude_month is not None and entry.month == date(
                exclude_month.year, exclude_month.month, 1
            ):
                continue
            actual = entry.actuals.get(measure)
            if actual is None:
                continue
            if before is not None and actual.observed_on >= before:
                continue
            for point in entry.nowcasts.get(measure, []):
                days = (actual.observed_on - point.observed_on).days
                if days >= 0 and lead_bucket(days) == bucket:
                    errors.append(actual.value - point.value)

        if len(errors) < _MIN_ERROR_SAMPLE:
            return None
        return ErrorModel(
            # Population stdev around zero rather than around the sample mean: the
            # quantity that matters is how far the actual lands from the nowcast,
            # and a systematic bias is part of that error, not something to
            # subtract out before measuring it.
            stdev=(sum(e * e for e in errors) / len(errors)) ** 0.5,
            mean_error=statistics.fmean(errors),
            sample_size=len(errors),
            lead_bucket=bucket,
        )


def parse_nowcast_payload(payload: list[Any]) -> dict[date, TargetMonth]:
    """Turn the chart payload into target months keyed by first-of-month."""
    out: dict[date, TargetMonth] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        month = _parse_subcaption((entry.get("chart") or {}).get("subcaption"))
        if month is None:
            continue

        labels = _labels(entry)
        if not labels:
            continue
        dates = _dates_for(labels, month)

        target = TargetMonth(month=month)
        for series in entry.get("dataset") or []:
            if not isinstance(series, dict):
                continue
            name = str(series.get("seriesname") or "")
            is_actual = name.startswith("Actual ")
            measure = _measure_for(name)
            if measure is None:
                continue

            points: list[NowcastPoint] = []
            for index, cell in enumerate(series.get("data") or []):
                if index >= len(dates) or not isinstance(cell, dict):
                    continue
                raw = cell.get("value")
                if raw in (None, ""):
                    continue
                try:
                    points.append(NowcastPoint(dates[index], float(raw)))
                except (TypeError, ValueError):
                    continue

            if not points:
                continue
            if is_actual:
                # One published value per month; the last is the one that stands.
                target.actuals[measure] = max(points, key=lambda p: p.observed_on)
            else:
                target.nowcasts[measure] = sorted(points, key=lambda p: p.observed_on)

        out[month] = target
    return out


def _measure_for(series_name: str) -> Measure | None:
    name = series_name.removeprefix("Actual ").strip()
    for measure in Measure:
        if measure.value == name:
            return measure
    return None


def _parse_subcaption(value: Any) -> date | None:
    """``"2026-8"`` -> 1 August 2026."""
    if not isinstance(value, str) or "-" not in value:
        return None
    year_text, _, month_text = value.partition("-")
    try:
        return date(int(year_text), int(month_text), 1)
    except ValueError:
        return None


def _labels(entry: dict[str, Any]) -> list[str]:
    categories = entry.get("categories") or []
    if not categories or not isinstance(categories[0], dict):
        return []
    return [
        str(c.get("label") or "")
        for c in categories[0].get("category") or []
        if isinstance(c, dict)
    ]


def _dates_for(labels: list[str], target_month: date) -> list[date]:
    """Attach a year to bare ``MM/DD`` labels.

    Nowcasts for a month continue into the following month, up to the release, so
    a label whose month is earlier than the target's belongs to the next calendar
    year: a ``01/13`` label under a ``2026-12`` target is January 2027.
    """
    out: list[date] = []
    for label in labels:
        month_text, _, day_text = label.partition("/")
        try:
            month, day = int(month_text), int(day_text)
            year = target_month.year + (1 if month < target_month.month else 0)
            out.append(date(year, month, day))
        except ValueError:
            # Keep positional alignment with the data arrays; an unparseable label
            # simply cannot be matched to a value.
            out.append(date(target_month.year, target_month.month, 1))
    return out


def as_of_date(moment: datetime | None) -> date | None:
    stamp = ensure_utc(moment)
    return stamp.date() if stamp else None
