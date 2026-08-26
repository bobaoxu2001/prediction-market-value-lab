"""Assembling the first admissible Model-vs-Market cohort.

One row is one *contract observation*: a market, at a decision instant, with the
forecast and book that were genuinely available then, and the settlement label
taken from the CLI daily maximum the contract's own rules name.

The statistical unit is the harder part. Forty-two contracts on one station-day
all resolve from a single number - that station's daily maximum - so they are
forty-two views of one realisation, not forty-two bets. Reporting them as
independent would inflate N by the size of the contract ladder, which is the
easiest way to manufacture significance from nothing. Both counts are therefore
carried separately and authorisation uses the conservative one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .exclusions import Exclusion
from .parity import Parity


@dataclass(frozen=True)
class ContractObservation:
    """One market at one decision instant, with full provenance."""

    # -- identity ----------------------------------------------------------
    market_ticker: str
    station: str
    target_date: date
    #: The station-day whose single daily maximum resolves this contract, and
    #: every other contract in its ladder. THE unit of independence.
    event_key: str

    # -- decision point ----------------------------------------------------
    decision_at: datetime

    # -- forecast side -----------------------------------------------------
    forecast_available_at: datetime
    forecast_value_f: Decimal
    forecast_age_seconds: float

    # -- market side -------------------------------------------------------
    book_available_at: datetime
    book_age_seconds: float
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_ask: Decimal | None

    # -- contract semantics ------------------------------------------------
    lower_inclusive: Decimal | None
    upper_inclusive: Decimal | None

    # -- model -------------------------------------------------------------
    model_probability: Decimal
    model_version: str
    sigma_f: float
    sigma_provisional: bool

    # -- label (never touches anything above) ------------------------------
    settlement_value_f: Decimal
    settlement_product: str
    settlement_available_at: datetime
    outcome_yes: bool

    parity: Parity

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_ticker": self.market_ticker,
            "station": self.station,
            "target_date": self.target_date.isoformat(),
            "event_key": self.event_key,
            "decision_at": self.decision_at.isoformat(),
            "forecast_available_at": self.forecast_available_at.isoformat(),
            "forecast_value_f": str(self.forecast_value_f),
            "forecast_age_seconds": round(self.forecast_age_seconds, 1),
            "book_available_at": self.book_available_at.isoformat(),
            "book_age_seconds": round(self.book_age_seconds, 1),
            "yes_bid": str(self.yes_bid) if self.yes_bid is not None else None,
            "yes_ask": str(self.yes_ask) if self.yes_ask is not None else None,
            "no_ask": str(self.no_ask) if self.no_ask is not None else None,
            "lower_inclusive": (
                str(self.lower_inclusive) if self.lower_inclusive is not None else None
            ),
            "upper_inclusive": (
                str(self.upper_inclusive) if self.upper_inclusive is not None else None
            ),
            "model_probability": str(self.model_probability),
            "model_version": self.model_version,
            "sigma_f": self.sigma_f,
            "sigma_provisional": self.sigma_provisional,
            "settlement_value_f": str(self.settlement_value_f),
            "settlement_product": self.settlement_product,
            "settlement_available_at": self.settlement_available_at.isoformat(),
            "outcome_yes": self.outcome_yes,
            "parity": self.parity.value,
        }


def event_key_for(station: str, target_date: date) -> str:
    """The unit of independence: one station's daily maximum on one day.

    Not the ticker. A ladder of buckets and thresholds over the same station-day
    resolves from one number, so treating each contract as independent evidence
    multiplies the apparent sample by the ladder width while adding no new
    information about the world.
    """
    return f"{station}:{target_date.isoformat()}"


@dataclass
class Cohort:
    """The assembled dataset, with both sample counts kept apart."""

    observations: list[ContractObservation] = field(default_factory=list)
    exclusions: dict[Exclusion, int] = field(default_factory=dict)

    def exclude(self, reason: Exclusion, n: int = 1) -> None:
        self.exclusions[reason] = self.exclusions.get(reason, 0) + n

    @property
    def contract_observations(self) -> int:
        """Rows. NOT the inferential sample size."""
        return len(self.observations)

    @property
    def unique_markets(self) -> int:
        return len({o.market_ticker for o in self.observations})

    @property
    def independent_events(self) -> int:
        """Distinct station-days. The count authorisation must use."""
        return len({o.event_key for o in self.observations})

    def admissible(self, allow_approximation: bool = False) -> list[ContractObservation]:
        """Observations whose settlement parity permits research use.

        ``MODELLING_APPROXIMATION`` is excluded by default and requires an
        explicit decision to include, because "the value should be identical"
        is a hypothesis until someone measures it.
        """
        allowed = {Parity.EXACT_MATCH}
        if allow_approximation:
            allowed.add(Parity.MODELLING_APPROXIMATION)
        return [o for o in self.observations if o.parity in allowed]

    def summary(self, *, allow_approximation: bool = False) -> dict[str, Any]:
        adm = self.admissible(allow_approximation)
        by_parity: dict[str, int] = {}
        for o in self.observations:
            by_parity[o.parity.value] = by_parity.get(o.parity.value, 0) + 1
        return {
            "contract_observations": self.contract_observations,
            "unique_markets": self.unique_markets,
            "independent_settlement_events": self.independent_events,
            "admissible_contract_observations": len(adm),
            "admissible_independent_events": len({o.event_key for o in adm}),
            "by_parity": dict(sorted(by_parity.items())),
            "exclusions": {c.value: n for c, n in sorted(
                self.exclusions.items(), key=lambda kv: -kv[1]
            )},
        }
