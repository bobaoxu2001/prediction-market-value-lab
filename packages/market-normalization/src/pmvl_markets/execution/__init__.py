"""Kalshi execution subsystem.

Phase 5A: authenticated account access, a demo execution harness, order/fill
reconciliation and a hard risk kill switch - built *before* the research system
is authorized to trade, so that authorization later is a policy decision rather
than an engineering scramble.

Real-money order placement is not implemented and is not reachable by
configuration. See :mod:`pmvl_markets.execution.modes` for the six independent
gates and why the lowest of them is the one that carries the guarantee.
"""

from .client import KalshiAccountClient, KalshiExecutionClient
from .credentials import (
    CredentialsUnavailable,
    KalshiCredentials,
    credentials_present,
    load_credentials,
)
from .harness import ExecutionHarness, PlannedOrder
from .idempotency import derive_client_order_id, new_attempt_id
from .ledger import ExecutionLedger, LedgerRecord, default_execution_db_path
from .models import (
    AccountBalance,
    CancelResult,
    ExposureSnapshot,
    Fill,
    Order,
    OrderAction,
    OrderIntent,
    OrderResult,
    OrderStatus,
    Outcome,
    Position,
    TimeInForce,
)
from .modes import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    ExecutionMode,
    KalshiEnvironment,
    LiveTradingNotAuthorized,
    ModeProfile,
    assert_transport_write_allowed,
)
from .reconcile import (
    Discrepancy,
    DiscrepancyKind,
    ReconciliationReport,
    reconcile_orders,
    reconcile_positions,
    recover_after_restart,
)
from .resolve import MarketResolution, ResolutionStatus, resolve_market
from .risk import (
    CONSERVATIVE_LIMITS,
    BlockReason,
    KillSwitch,
    RiskDecision,
    RiskGate,
    RiskLimits,
    RiskVerdict,
)

#: There is no live mode. This constant exists so that a caller asking the
#: question gets an unambiguous answer rather than inferring one from an absence.
LIVE_AUTONOMOUS_TRADING_AUTHORIZED = False

__all__ = [
    "AccountBalance",
    "BlockReason",
    "CONSERVATIVE_LIMITS",
    "CancelResult",
    "CredentialsUnavailable",
    "DEMO_BASE_URL",
    "Discrepancy",
    "DiscrepancyKind",
    "ExecutionHarness",
    "ExecutionLedger",
    "ExecutionMode",
    "ExposureSnapshot",
    "Fill",
    "KalshiAccountClient",
    "KalshiCredentials",
    "KalshiEnvironment",
    "KalshiExecutionClient",
    "KillSwitch",
    "LIVE_AUTONOMOUS_TRADING_AUTHORIZED",
    "LedgerRecord",
    "LiveTradingNotAuthorized",
    "MarketResolution",
    "ModeProfile",
    "Order",
    "OrderAction",
    "OrderIntent",
    "OrderResult",
    "OrderStatus",
    "Outcome",
    "PRODUCTION_BASE_URL",
    "PlannedOrder",
    "Position",
    "ReconciliationReport",
    "ResolutionStatus",
    "RiskDecision",
    "RiskGate",
    "RiskLimits",
    "RiskVerdict",
    "TimeInForce",
    "assert_transport_write_allowed",
    "credentials_present",
    "default_execution_db_path",
    "derive_client_order_id",
    "load_credentials",
    "new_attempt_id",
    "reconcile_orders",
    "reconcile_positions",
    "recover_after_restart",
    "resolve_market",
]
