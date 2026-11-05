"""PMVL alpha engine: point-in-time features, alpha, execution, strategy.

The architecture, in one line:

    Data -> PIT feature store -> Outcome model -> Alpha model -> Execution model
         -> Strategy -> Backtest / Shadow -> Evidence

The separation that matters is the middle: PMVL used to go straight from a fair
probability to a recommendation, which conflates three different questions.

* :mod:`.models` holds all three and keeps them apart by type. ``OutcomeEstimate``
  answers "will it settle YES?"; ``AlphaScore`` answers "will the price move?";
  ``ExecutionAssessment`` answers "would we get filled, and at what?". A
  probability cannot be typed into the slot where an expected move belongs.
* :mod:`.pit` and :mod:`.registry` make historical features reproducible: every
  observation carries four separate timestamps and ``AsOfView`` refuses to hand
  over anything the decision could not have seen.
* :mod:`.execution` refuses to fill a maker order because a quote touched it.
* :mod:`.evidence` decides what a result is allowed to claim, from what exists
  rather than from what is hoped.

Importing this package registers every factor in :data:`.registry.REGISTRY`.

Nothing here places an order. There is no execution service and no venue
credential in this package by design.
"""

from __future__ import annotations

# Importing the factor module is what populates the registry. Ordered after the
# registry import and marked noqa because it is a side-effecting import, not an
# unused one - removing it leaves an empty registry and every research run
# reports zero coverage with no error.
from . import factors as _factors  # noqa: F401
from .evidence import (
    NO_VERIFIED_BACKTEST,
    EvidenceAssessment,
    EvidenceStatus,
    ResearchRecord,
    ResearchSplit,
    assess,
)
from .events import (
    Event,
    EventKind,
    SettlementPayload,
    Timeline,
    TimelineBuilder,
    TradePayload,
)
from .execution import (
    ConservativeFillModel,
    FillConfidence,
    FillResult,
    LevelTrade,
    MakerExecutionModel,
    MakerQuote,
    TakerExecutionModel,
    exit_proceeds_per_contract,
    maker_limit_price,
)
from .metrics import StrategyMetrics, expected_versus_realised, segment, summarise
from .models import (
    AlphaDirection,
    AlphaModel,
    AlphaScore,
    ExecutionAssessment,
    ExecutionModel,
    FrozenOutcomeModel,
    Liquidity,
    OutcomeEstimate,
    OutcomeModel,
    ProposedOrder,
    RuleBasedAlphaModel,
    outcome_from_prediction,
)
from .pit import AsOfView, LookaheadError, SourceObservation, observation
from .registry import (
    REGISTRY,
    FactorContext,
    FactorFamily,
    FactorRegistry,
    FactorSpec,
    FeatureValue,
    FeatureVector,
    Unavailable,
)
from .shadow import (
    ImmutabilityError,
    ShadowDecision,
    ShadowLedger,
    ShadowOutcome,
    ShadowStatus,
    decision_id_for,
    record_decision,
    verify_unchanged,
)
from .simulator import MarketHistory, SimulatedTrade, SimulationResult, run
from .sizing import (
    NO_TRADE,
    DeclineReason,
    ExposureLedger,
    ExposureLimits,
    SizeDecision,
    size_position,
)
from .strategy import (
    ConvergenceMode,
    ExitPolicy,
    ExitTrigger,
    OpenPosition,
    SettlementMode,
    Signal,
    SourceLagConvergenceStrategy,
    Strategy,
)

__all__ = [
    # point-in-time
    "AsOfView", "LookaheadError", "SourceObservation", "observation",
    # registry
    "REGISTRY", "FactorContext", "FactorFamily", "FactorRegistry", "FactorSpec",
    "FeatureValue", "FeatureVector", "Unavailable",
    # the three models
    "AlphaDirection", "AlphaModel", "AlphaScore", "ExecutionAssessment",
    "ExecutionModel", "FrozenOutcomeModel", "Liquidity", "OutcomeEstimate",
    "OutcomeModel", "ProposedOrder", "RuleBasedAlphaModel",
    "outcome_from_prediction",
    # execution
    "ConservativeFillModel", "FillConfidence", "FillResult", "LevelTrade",
    "MakerExecutionModel", "MakerQuote", "TakerExecutionModel",
    "exit_proceeds_per_contract", "maker_limit_price",
    # sizing
    "NO_TRADE", "DeclineReason", "ExposureLedger", "ExposureLimits",
    "SizeDecision", "size_position",
    # strategy
    "ConvergenceMode", "ExitPolicy", "ExitTrigger", "OpenPosition",
    "SettlementMode", "Signal", "SourceLagConvergenceStrategy", "Strategy",
    # events and simulation
    "Event", "EventKind", "SettlementPayload", "Timeline", "TimelineBuilder",
    "TradePayload", "MarketHistory", "SimulatedTrade", "SimulationResult", "run",
    # metrics
    "StrategyMetrics", "expected_versus_realised", "segment", "summarise",
    # shadow
    "ImmutabilityError", "ShadowDecision", "ShadowLedger", "ShadowOutcome",
    "ShadowStatus", "decision_id_for", "record_decision", "verify_unchanged",
    # evidence
    "NO_VERIFIED_BACKTEST", "EvidenceAssessment", "EvidenceStatus",
    "ResearchRecord", "ResearchSplit", "assess",
]
