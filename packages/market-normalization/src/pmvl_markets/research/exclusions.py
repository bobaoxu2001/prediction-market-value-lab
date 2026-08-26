"""Why an observation did not make it into the dataset.

Every rejection is a structured code, never a silent drop. A dataset that
reports "1,204 observations" without saying what happened to the other 40,000 is
not auditable, and the shape of the exclusions is usually more informative than
the surviving rows - a pipeline that quietly discards 97% of its input because of
a timezone bug looks identical, from the outside, to one that works.
"""

from __future__ import annotations

from enum import Enum


class Exclusion(str, Enum):
    """Structured rejection reasons, one per failure mode."""

    # --- forecast side -----------------------------------------------------
    NO_FORECAST_AVAILABLE = "NO_FORECAST_AVAILABLE"
    FORECAST_AFTER_MARKET = "FORECAST_AFTER_MARKET"
    UNVERIFIED_FORECAST_TIMESTAMP = "UNVERIFIED_FORECAST_TIMESTAMP"
    FORECAST_TOO_STALE = "FORECAST_TOO_STALE"

    # --- market side -------------------------------------------------------
    NO_ORDERBOOK = "NO_ORDERBOOK"
    ORDERBOOK_TOO_STALE = "ORDERBOOK_TOO_STALE"
    BOOK_INVALID = "BOOK_INVALID"
    SPREAD_INVALID = "SPREAD_INVALID"
    NO_EXECUTABLE_PRICE = "NO_EXECUTABLE_PRICE"

    # --- contract semantics ------------------------------------------------
    CONTRACT_PARSE_FAILED = "CONTRACT_PARSE_FAILED"
    STATION_MISMATCH = "STATION_MISMATCH"
    STATION_UNMAPPED = "STATION_UNMAPPED"
    TARGET_DATE_MISMATCH = "TARGET_DATE_MISMATCH"
    SETTLEMENT_RULE_MISMATCH = "SETTLEMENT_RULE_MISMATCH"

    # --- outcome -----------------------------------------------------------
    NO_SETTLEMENT = "NO_SETTLEMENT"
    #: Every book for this market was captured at or after it settled, so its
    #: price embeds the outcome and must never be scored.
    #:
    #: Shortfall-class, not defect-class. The exclusion firing is the safeguard
    #: working: these are rows the collector took before the near-dated horizon
    #: existed, when it still polled markets whose resolution time had passed.
    #: It would be a defect only if it were still happening, which
    #: `settled_markets_still_captured` checks separately - a live count of zero
    #: with a historical count of 84 is a repaired bug leaving residue, and
    #: reporting that as an active defect would send someone hunting for a bug
    #: that is already fixed.
    BOOK_AFTER_SETTLEMENT = "BOOK_AFTER_SETTLEMENT"

    # --- model -------------------------------------------------------------
    MODEL_PROBABILITY_INVALID = "MODEL_PROBABILITY_INVALID"
    MODEL_INSUFFICIENT_HISTORY = "MODEL_INSUFFICIENT_HISTORY"
    LADDER_INCOHERENT = "LADDER_INCOHERENT"


#: Codes that indicate a defect to fix rather than data that has not arrived.
#: Separated because "we have not collected enough yet" and "the pipeline is
#: broken" both produce zero rows and require opposite responses.
DEFECT_CODES = frozenset(
    {
        Exclusion.FORECAST_AFTER_MARKET,
        Exclusion.CONTRACT_PARSE_FAILED,
        Exclusion.STATION_MISMATCH,
        Exclusion.TARGET_DATE_MISMATCH,
        Exclusion.SETTLEMENT_RULE_MISMATCH,
        Exclusion.BOOK_INVALID,
        Exclusion.MODEL_PROBABILITY_INVALID,
        Exclusion.LADDER_INCOHERENT,
    }
)
