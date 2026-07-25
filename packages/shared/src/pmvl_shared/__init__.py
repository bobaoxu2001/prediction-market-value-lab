"""Shared foundation for the Prediction Market Value Lab."""

from .config import Settings, get_settings
from .enums import (
    ArbitrageKind,
    ArbitrageLabel,
    Category,
    DataProvenance,
    DataQuality,
    EvidenceStance,
    JobStatus,
    MarketStatus,
    Platform,
    RecommendationState,
    RuleCompatibility,
    SettlementResult,
    Side,
)
from .logging_setup import get_logger, setup_logging
from .money import D, ceil_cent, clamp_prob, floor_cent, quantize_price, quantize_usd, safe_div

__version__ = "0.1.0"

__all__ = [
    "ArbitrageKind",
    "ArbitrageLabel",
    "Category",
    "D",
    "DataProvenance",
    "DataQuality",
    "EvidenceStance",
    "JobStatus",
    "MarketStatus",
    "Platform",
    "RecommendationState",
    "RuleCompatibility",
    "Settings",
    "SettlementResult",
    "Side",
    "__version__",
    "ceil_cent",
    "clamp_prob",
    "floor_cent",
    "get_logger",
    "get_settings",
    "quantize_price",
    "quantize_usd",
    "safe_div",
    "setup_logging",
]
