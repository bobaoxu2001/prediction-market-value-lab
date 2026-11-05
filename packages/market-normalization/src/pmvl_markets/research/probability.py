"""Contract probability from an archived forecast, as of a cutoff.

The repository already has a ``WeatherThresholdModel``, and it is not usable
here: it calls the NWS API at evaluation time. For live scoring that is correct;
for research it returns *today's* forecast no matter which historical instant is
being evaluated, which is total lookahead. This module reads the archive
instead, and cannot reach the network at all.

The probability is deliberately simple - a normal error distribution around the
forecast high - because the point of this phase is auditable provenance, not
meteorological skill. A sophisticated model whose information set is unclear is
worth less here than a crude one whose information set is provable.

The error width is the honest weak point. It is currently a documented prior,
not an estimate, because estimating it requires paired forecast/realisation
history that this archive does not yet contain. Every probability produced under
that prior is labelled, so a downstream consumer cannot mistake it for a fitted
quantity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .contract import ContractSpec
from .exclusions import Exclusion

#: Standard deviation of (realised - forecast) high temperature, in F, by lead
#: time. A documented prior from published NWS verification, NOT fitted here -
#: fitting it needs paired history the archive does not yet hold.
#:
#: Labelled PROVISIONAL wherever it is used. The interface exists so that the
#: estimate can be swapped for a fitted one without touching call sites.
PROVISIONAL_SIGMA_F: dict[int, float] = {
    0: 2.0,
    1: 2.5,
    2: 3.2,
    3: 4.0,
    4: 4.8,
    5: 5.5,
}
_MAX_SIGMA = 6.5

#: Set on every estimate produced from the prior above.
PROVISIONAL_MODEL_VERSION = "weather-normal-provisional-v1"


def sigma_for_lead(lead_days: int) -> float:
    """Error width at a lead time, saturating rather than extrapolating."""
    if lead_days <= 0:
        return PROVISIONAL_SIGMA_F[0]
    return PROVISIONAL_SIGMA_F.get(min(lead_days, 5), _MAX_SIGMA)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class ProbabilityEstimate:
    """A contract probability, with everything needed to reproduce it."""

    probability: Decimal
    model_version: str
    #: The instant beyond which no information entered this estimate.
    information_cutoff: datetime
    forecast_value_f: Decimal
    forecast_available_at: datetime
    lead_days: int
    sigma_f: float
    #: True while sigma is a documented prior rather than a fitted estimate.
    provisional: bool
    reason: Exclusion | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_probability": str(self.probability),
            "model_version": self.model_version,
            "model_information_cutoff": self.information_cutoff.isoformat(),
            "forecast_value_f": str(self.forecast_value_f),
            "forecast_available_at": self.forecast_available_at.isoformat(),
            "lead_days": self.lead_days,
            "sigma_f": self.sigma_f,
            "provisional_sigma": self.provisional,
        }


def contract_probability(
    spec: ContractSpec,
    *,
    forecast_high_f: Decimal,
    forecast_available_at: datetime,
    information_cutoff: datetime,
    lead_days: int,
) -> ProbabilityEstimate:
    """P(settlement value falls in the contract's region | forecast at cutoff).

    Integer settlement is respected. The reading is a whole degree, so an
    inclusive bucket [87, 88] covers the continuous interval [86.5, 88.5) once
    rounding is accounted for. Ignoring that half-degree on each side
    understates every interior bucket and overstates the tails - consistently,
    which makes it look like signal rather than like a bug.
    """
    if forecast_available_at > information_cutoff:
        raise ValueError(
            "forecast is not available at the information cutoff: "
            f"{forecast_available_at.isoformat()} > {information_cutoff.isoformat()}"
        )

    sigma = sigma_for_lead(lead_days)
    mu = float(forecast_high_f)

    def cdf_at(bound: Decimal | None, *, upper: bool) -> float:
        if bound is None:
            return 1.0 if upper else 0.0
        # Continuity correction for the integer reading.
        edge = float(bound) + (0.5 if upper else -0.5)
        return _normal_cdf((edge - mu) / sigma)

    probability = cdf_at(spec.upper, upper=True) - cdf_at(spec.lower, upper=False)
    probability = min(1.0, max(0.0, probability))

    return ProbabilityEstimate(
        probability=Decimal(str(round(probability, 6))),
        model_version=PROVISIONAL_MODEL_VERSION,
        information_cutoff=information_cutoff,
        forecast_value_f=forecast_high_f,
        forecast_available_at=forecast_available_at,
        lead_days=lead_days,
        sigma_f=sigma,
        provisional=True,
    )


def validate_probability(value: Decimal) -> Exclusion | None:
    if not (Decimal(0) <= value <= Decimal(1)):
        return Exclusion.MODEL_PROBABILITY_INVALID
    return None
