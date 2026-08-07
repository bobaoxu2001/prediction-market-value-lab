"""CPI bucket model: the Cleveland Fed nowcast plus its own measured error.

Kalshi's inflation boards are exhaustive 0.1-point buckets over the figure BLS will
publish:

    KXECONSTATCPICORE-26AUG-T0.3      "CPI core month-over-month in Aug 2026?"
                                      YES if the published figure is exactly 0.3%

Because BLS rounds to one decimal, "exactly 0.3%" is the interval [0.25, 0.35).
So the probability is the mass a normal distribution puts on that interval:

    P = Phi((hi - mu) / sigma) - Phi((lo - mu) / sigma)

``mu`` is the Cleveland Fed's nowcast for that month as of the evaluation instant.
``sigma`` is not assumed - it is the root-mean-square of the nowcast's *own* past
errors at the same lead time, computed from the 158 months of nowcast-and-actual
pairs in the same file. Neither input observes the contract's price.

## Two sigmas, and why

Measured over 2013-2026 the core CPI nowcast's error is ~0.157pp. Over the last
three years it is ~0.113pp. The gap is 2020-21, when single-month errors reached
0.78pp.

Neither number is the honest one on its own. The recent figure describes the
regime the next print will almost certainly land in; the long one describes what
happens when the regime breaks, which is exactly when a tail bucket pays. Picking
the short window alone would make the model confident and quietly wrong in a
shock; picking the long one alone would spread the distribution across three
buckets permanently, and on an exhaustive board that does not read as caution - it
systematically overprices every tail bucket and would generate a steady stream of
"the tails are cheap" recommendations, which is a position, not a forecast.

So the point estimate uses the current regime and the **interval** uses the full
history, via the same device the crypto model uses for volatility error: evaluate
twice and report the spread as the estimate's own standard deviation. The
conservative bound that gates eligibility then already contains the regime-shift
possibility, which is where it belongs.

## Coverage

Month-over-month and year-over-year, headline and core CPI. PCE is nowcast in the
same file and has no Kalshi board in this ticker family, so it is parsed and
unused rather than special-cased. Payrolls, GDP and unemployment have no nowcast
here and are left to :class:`~.structural.EconomicsModel` to decline - they would
need FRED or BLS, which is a separate dependency and a separate piece of work.
"""

from __future__ import annotations

import math
import re
from datetime import date
from decimal import Decimal

from pmvl_shared.enums import Category
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, clamp_prob, quantize_prob
from pmvl_shared.timeutil import ensure_utc, utcnow

from ...providers.cleveland_fed import (
    ClevelandFedProvider,
    Frequency,
    Measure,
)
from ..base import (
    ModelContext,
    ModelEstimate,
    ProbabilityModel,
    lookahead_guard,
    no_opinion,
)

log = get_logger(__name__)

#: ``KXECONSTATCPICORE-26AUG-T0.3``, and the negative-strike form ``-T-0.2``.
_CPI_TICKER_RE = re.compile(
    # `[A-Z0-9]+` and not `[A-Z]+`: the unemployment board is KXECONSTATU3, and
    # excluding it here sent it to the "unrecognised ticker" branch instead of the
    # accurate "no nowcast backs this series" one. Both decline; only one tells the
    # reader what is actually missing.
    r"^(?P<series>KXECONSTAT[A-Z0-9]+)-"
    r"(?P<yy>\d{2})(?P<mon>[A-Z]{3})-"
    r"T(?P<strike>-?\d+(?:\.\d+)?)$"
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: Series ticker -> (which inflation measure, month-over-month or year-over-year).
SERIES_MEASURES: dict[str, tuple[Measure, Frequency]] = {
    "KXECONSTATCPI": (Measure.CPI, Frequency.MONTH_OVER_MONTH),
    "KXECONSTATCPICORE": (Measure.CORE_CPI, Frequency.MONTH_OVER_MONTH),
    "KXECONSTATCPIYOY": (Measure.CPI, Frequency.YEAR_OVER_YEAR),
    "KXECONSTATCORECPIYOY": (Measure.CORE_CPI, Frequency.YEAR_OVER_YEAR),
}

#: BLS publishes these to one decimal place, so a bucket is +/- half a step.
BUCKET_HALF_WIDTH = 0.05

#: How far back "the current regime" reaches, in months.
REGIME_WINDOW_MONTHS = 36


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_probability(strike: float, mu: float, sigma: float) -> float:
    """Mass on the rounding interval around ``strike``."""
    if sigma <= 0:
        # A zero-width distribution puts everything on one bucket. Degenerate, and
        # the caller treats it as no opinion rather than a certainty.
        return 1.0 if abs(strike - mu) <= BUCKET_HALF_WIDTH else 0.0
    low = (strike - BUCKET_HALF_WIDTH - mu) / sigma
    high = (strike + BUCKET_HALF_WIDTH - mu) / sigma
    return max(0.0, normal_cdf(high) - normal_cdf(low))


def parse_cpi_ticker(ticker: str) -> dict[str, object] | None:
    """Series, target month and the bucket's centre, or None."""
    match = _CPI_TICKER_RE.match(ticker.strip().upper())
    if match is None:
        return None
    month = _MONTHS.get(match.group("mon"))
    if month is None:
        return None
    try:
        target = date(2000 + int(match.group("yy")), month, 1)
        strike = float(match.group("strike"))
    except ValueError:
        return None
    return {
        "series": match.group("series"),
        "target_month": target,
        "strike": strike,
    }


class CpiNowcastModel(ProbabilityModel):
    """Bucket probabilities for CPI boards from the Cleveland Fed nowcast."""

    name = "cpi_nowcast_bucket"
    categories = (Category.ECONOMICS,)
    independent = True
    #: Higher than the sports prior and below the weather model. The nowcast is a
    #: serious published forecast, and the residual it cannot see - the surprise
    #: component of a single print - is genuinely large relative to a 0.1pp bucket.
    max_confidence = Decimal("0.55")
    #: Nowcasts are dated and the error history can be cut at the same instant, so
    #: this model can be replayed. See `providers.cleveland_fed.error_model`.
    supports_as_of = True

    def __init__(self, provider: ClevelandFedProvider | None = None) -> None:
        self._provider = provider or ClevelandFedProvider()

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def estimate(self, ctx: ModelContext) -> ModelEstimate:
        if (declined := lookahead_guard(self, ctx)) is not None:
            return declined

        parsed = parse_cpi_ticker(ctx.market.platform_market_id)
        if parsed is None:
            return no_opinion(
                "not a recognised CPI bucket ticker; this model prices only the "
                "KXECONSTAT inflation boards"
            )

        series = str(parsed["series"])
        mapping = SERIES_MEASURES.get(series)
        if mapping is None:
            return no_opinion(
                f"no nowcast series backs {series}; payrolls, GDP and unemployment "
                "would need FRED or BLS, which this model does not use"
            )
        measure, frequency = mapping

        target_month: date = parsed["target_month"]  # type: ignore[assignment]
        strike = float(parsed["strike"])
        evaluated_at = ensure_utc(ctx.evaluation_time or utcnow())
        today = evaluated_at.date()

        point = await self._provider.nowcast(
            measure, frequency, target_month, as_of=today
        )
        if point is None:
            return no_opinion(
                f"no {measure.value} nowcast for {target_month:%Y-%m} published on "
                f"or before {today.isoformat()}"
            )

        # Lead time is measured to the market's own resolution, which is the BLS
        # release. The nowcast's error history is keyed on the same quantity.
        resolution = ensure_utc(
            ctx.market.expected_resolution_time or ctx.market.close_time
        )
        lead_days = (
            max(0, (resolution.date() - point.observed_on).days)
            if resolution is not None
            else 0
        )

        regime_start = date(
            target_month.year - REGIME_WINDOW_MONTHS // 12,
            target_month.month,
            1,
        )
        recent = await self._provider.error_model(
            measure,
            frequency,
            lead_days=lead_days,
            since=regime_start,
            before=today,
            exclude_month=target_month,
        )
        full = await self._provider.error_model(
            measure,
            frequency,
            lead_days=lead_days,
            before=today,
            exclude_month=target_month,
        )
        if recent is None or full is None:
            return no_opinion(
                "too few past nowcast errors at this lead time to measure a "
                "dispersion; refusing to assume one"
            )

        probability = bucket_probability(strike, point.value, recent.stdev)
        # The interval admits the regime can break: the same bucket priced with the
        # full-history dispersion, and the gap between the two is this estimate's
        # own uncertainty.
        shocked = bucket_probability(strike, point.value, full.stdev)
        stdev = abs(probability - shocked)

        confidence = float(self.max_confidence)
        # A nowcast made long before the print has seen little of the month's data.
        if lead_days > 30:
            confidence *= 0.6
        elif lead_days > 14:
            confidence *= 0.8
        # A bucket far into the tail is where the normal assumption is weakest.
        if abs(strike - point.value) > 3 * recent.stdev:
            confidence *= 0.5

        return ModelEstimate(
            probability=quantize_prob(clamp_prob(D(str(probability)))),
            confidence=quantize_prob(D(str(confidence))),
            stdev=max(D(str(stdev)), Decimal("0.02")),
            independent=True,
            detail=(
                f"Cleveland Fed {measure.value} {frequency.value} for "
                f"{target_month:%Y-%m}: nowcast {point.value:.3f}% on "
                f"{point.observed_on.isoformat()}, sigma {recent.stdev:.3f} "
                f"(regime) / {full.stdev:.3f} (full history) at "
                f"{recent.lead_bucket} lead -> P(exactly {strike:g}%)="
                f"{probability:.3f}"
            ),
            data_freshness_seconds=max(0, (today - point.observed_on).days) * 86400,
            data={
                "measure": measure.value,
                "frequency": frequency.value,
                "target_month": target_month.isoformat(),
                "strike_percent": f"{strike:g}",
                "bucket": f"[{strike - BUCKET_HALF_WIDTH:g}, {strike + BUCKET_HALF_WIDTH:g})",
                "nowcast": f"{point.value:.4f}",
                "nowcast_observed_on": point.observed_on.isoformat(),
                "lead_days": lead_days,
                "sigma_regime": recent.as_dict(),
                "sigma_full_history": full.as_dict(),
                "regime_window_months": REGIME_WINDOW_MONTHS,
                "model": "nowcast_normal_bucket",
                "known_limitation": (
                    "A normal around a point nowcast. It cannot see the surprise "
                    "component of a single print, and the rounding bucket is "
                    "narrow relative to the nowcast's measured error."
                ),
            },
        )
