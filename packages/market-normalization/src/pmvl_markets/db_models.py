"""Re-export the ORM models under a short local name.

The engines import dozens of model classes; routing them through one module keeps
`from ..db_models import Market, Recommendation` readable and gives a single place to
adjust if the shared package's layout ever changes.
"""

from pmvl_shared.db.models import (  # noqa: F401
    ArbitrageOpportunity,
    BacktestRun,
    BacktestTrade,
    Event,
    EvidenceItem,
    JobRun,
    Market,
    MarketMatch,
    MarketRule,
    MarketRuleVersion,
    ModelPrediction,
    ModelVersion,
    OrderbookLevel,
    OrderbookSnapshot,
    Outcome,
    PlatformRow,
    PriceSnapshot,
    Recommendation,
    RecommendationSnapshot,
    Settlement,
    Trade,
)

__all__ = [
    "ArbitrageOpportunity", "BacktestRun", "BacktestTrade", "Event", "EvidenceItem",
    "JobRun", "Market", "MarketMatch", "MarketRule", "ModelPrediction", "ModelVersion",
    "OrderbookLevel", "OrderbookSnapshot", "Outcome", "PlatformRow", "PriceSnapshot",
    "Recommendation", "RecommendationSnapshot", "Settlement", "Trade",
]
