"""Controlled vocabularies shared by the database, API and UI."""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    DEMO = "demo"


class DataProvenance(StrEnum):
    """Where a row's numbers came from.

    This is the single most important field in the schema. Every market, quote,
    recommendation and arbitrage row carries it, and production surfaces filter on
    ``LIVE``. A ``DEMO`` row can never be rendered as a real opportunity.
    """

    LIVE = "live"           # fetched from a venue's production API
    FIXTURE = "fixture"     # a recorded real response, replayed (tests)
    DEMO = "demo"           # synthetic, illustrative only - never a real opportunity


class MarketStatus(StrEnum):
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class Side(StrEnum):
    YES = "yes"
    NO = "no"


class Category(StrEnum):
    WEATHER = "weather"
    SPORTS = "sports"
    ECONOMICS = "economics"
    CRYPTO = "crypto"
    FINANCE = "finance"
    POLITICS = "politics"
    GEOPOLITICS = "geopolitics"
    CULTURE = "culture"
    TECH = "tech"
    MENTIONS = "mentions"
    OTHER = "other"


class RuleCompatibility(StrEnum):
    """How confidently two markets can be treated as the same underlying question.

    Only ``IDENTICAL`` may back an "executable arbitrage" claim. Everything else is
    downgraded to a labelled, risk-flagged theoretical opportunity.
    """

    IDENTICAL = "identical"       # same event, source, cutoff, and measurement basis
    EQUIVALENT = "equivalent"     # semantically same, immaterial wording differences
    SIMILAR = "similar"           # same subject, at least one material term differs
    INCOMPATIBLE = "incompatible"


class ArbitrageKind(StrEnum):
    COMPLETE_SET = "complete_set"
    CROSS_PLATFORM = "cross_platform"
    MULTI_OUTCOME = "multi_outcome"
    LOGICAL_CONSTRAINT = "logical_constraint"
    STALE_QUOTE = "stale_quote"


class ArbitrageLabel(StrEnum):
    """Honest classification. ``EXECUTABLE`` has a hard, auditable definition."""

    EXECUTABLE = "executable"
    THEORETICAL = "theoretical_arbitrage"
    RULE_MISMATCH_RISK = "rule_mismatch_risk"
    EXECUTION_RISK = "execution_risk"
    STALE_QUOTE = "stale_quote"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    NOT_GUARANTEED = "not_guaranteed"
    LOGICAL_MISPRICING = "logical_mispricing"


class RecommendationState(StrEnum):
    """Lifecycle of a published recommendation. Snapshots are never overwritten."""

    STILL_ACTIONABLE = "still_actionable"
    EDGE_REDUCED = "edge_reduced"
    NO_LONGER_ACTIONABLE = "no_longer_actionable"
    MARKET_CLOSED = "market_closed"
    SETTLED = "settled"


class SettlementResult(StrEnum):
    YES = "yes"
    NO = "no"
    VOID = "void"
    FIFTY_FIFTY = "fifty_fifty"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceStance(StrEnum):
    SUPPORTS_YES = "supports_yes"
    SUPPORTS_NO = "supports_no"
    NEUTRAL = "neutral"
    CONFLICTING = "conflicting"


class DataQuality(StrEnum):
    """Backtest honesty flag: what the simulated fill was actually derived from."""

    ORDERBOOK = "orderbook"        # real depth snapshot at decision time
    QUOTE = "quote"                # top-of-book only, depth unknown
    CANDLE = "candle"              # OHLC bar; NOT an executable price
    UNKNOWN = "unknown"
