"""Historical retrodiction: replaying models against markets that already settled.

Deliberately a separate package from ``backtest``. The two answer questions that
sound alike and carry very different weight as evidence - see the module docstring
in :mod:`.harness` for why they must never be pooled into one number.
"""

from .harness import (
    DEFAULT_LEAD_TIMES,
    RETRODICTION_PROVENANCE,
    Forecast,
    PriceHistorySource,
    ProviderPriceHistory,
    RetrodictionHarness,
    RetrodictionResult,
    SettledMarket,
)
from .run import run_retrodiction
from .rewind import RewindError, RewoundMarket, assert_no_outcome_leak, rewind_market
from .sampling import (
    DEFAULT_CATEGORIES,
    SamplingCriteria,
    describe_sample,
    load_settled_markets,
)

__all__ = [
    "DEFAULT_CATEGORIES",
    "DEFAULT_LEAD_TIMES",
    "RETRODICTION_PROVENANCE",
    "Forecast",
    "PriceHistorySource",
    "ProviderPriceHistory",
    "RetrodictionHarness",
    "RetrodictionResult",
    "RewindError",
    "RewoundMarket",
    "SamplingCriteria",
    "SettledMarket",
    "assert_no_outcome_leak",
    "describe_sample",
    "load_settled_markets",
    "rewind_market",
    "run_retrodiction",
]
