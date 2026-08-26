"""Does the quantity we model match the quantity the venue settles?

A model can be well calibrated against the wrong number. These contracts settle
on the daily maximum published in the NWS Climatological Report (the CLIxxx
product), or on The Weather Company's relay of it - and this repository archives
neither. It archives hourly station observations and NWS gridpoint forecasts.

The hourly maximum and the CLI daily maximum are usually equal and are not the
same quantity. CLI applies its own quality control, uses a local calendar day,
and can be corrected after publication. Scoring a model against the hourly max
and reporting it as settlement accuracy is a category error that no amount of
sample size will reveal, because the error is in the definition rather than in
the estimate.

So parity is classified explicitly and the classification travels with the data.
"""

from __future__ import annotations

from enum import Enum


class Parity(str, Enum):
    """How well the modelled quantity matches the settled quantity."""

    #: Same station, same product, same day definition, same rounding.
    EXACT_MATCH = "EXACT_MATCH"
    #: A different source for the same physical quantity, whose difference has
    #: been measured and is carried as a known adjustment.
    MODELLING_APPROXIMATION = "MODELLING_APPROXIMATION"
    #: A different source whose difference has NOT been measured.
    SETTLEMENT_MISMATCH = "SETTLEMENT_MISMATCH"
    #: Provenance insufficient to decide.
    UNKNOWN = "UNKNOWN"


#: Only these may enter core Model-vs-Market analysis.
ADMISSIBLE = frozenset({Parity.EXACT_MATCH, Parity.MODELLING_APPROXIMATION})


def classify(
    *,
    settlement_vendor: str | None,
    settlement_product_code: str | None,
    archived_observation_product: str | None,
    has_daily_summary: bool,
    archived_product_code: str | None = None,
) -> tuple[Parity, str]:
    """Classify one contract's parity, with the reason attached.

    Fails toward MISMATCH rather than UNKNOWN when the sources are known and
    known to differ: "we do not know" invites a later reader to assume it is
    probably fine, while "these are different sources and the difference is
    unmeasured" does not.
    """
    if not settlement_product_code and not settlement_vendor:
        return Parity.UNKNOWN, "no settlement product or vendor recorded"

    if not has_daily_summary:
        return (
            Parity.SETTLEMENT_MISMATCH,
            f"settles on the {settlement_product_code or 'daily'} daily maximum, "
            f"but only '{archived_observation_product}' observations are archived; "
            "no daily summary has been collected, so the settled quantity has "
            "never been observed",
        )

    if settlement_vendor and settlement_vendor != "national_weather_service":
        # A relay of the SAME product is a different claim from a different
        # product, and collapsing them loses the distinction that decides how
        # much work remains. The Weather Company redistributes the NWS CLI
        # bulletin the contract names; what is unknown is whether it relays the
        # value unchanged and how it handles a later NWS correction.
        if archived_product_code and archived_product_code == settlement_product_code:
            return (
                Parity.MODELLING_APPROXIMATION,
                f"settles via {settlement_vendor}'s relay of "
                f"{settlement_product_code}, which is the product archived here; "
                "the value should be identical but relay timing and revision "
                "handling are UNMEASURED, so this is an approximation and not "
                "parity",
            )
        return (
            Parity.SETTLEMENT_MISMATCH,
            f"settles via {settlement_vendor} on {settlement_product_code}, "
            f"archive holds {archived_product_code or 'a different product'}",
        )

    if archived_product_code and archived_product_code != settlement_product_code:
        return (
            Parity.SETTLEMENT_MISMATCH,
            f"settles on {settlement_product_code}, archive holds "
            f"{archived_product_code}",
        )

    return Parity.EXACT_MATCH, "same station, product and day definition"
