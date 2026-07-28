"""Transport schemas shared by providers, engines and the HTTP API.

These are deliberately separate from the ORM models: providers produce them before
anything is persisted, and the engines operate on them in-memory during a scan.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    ArbitrageKind,
    ArbitrageLabel,
    Category,
    DataProvenance,
    EvidenceStance,
    MarketStatus,
    Platform,
    RuleCompatibility,
    Side,
)
from .money import D


class StrictModel(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        ser_json_timedelta="iso8601",
    )


# ------------------------------------------------------------------- orderbook
class BookLevel(StrictModel):
    """One price level. ``size`` is in contracts, not dollars."""

    price: Decimal
    size: Decimal

    @field_validator("price", "size", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        return D(v)

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


class OrderBook(StrictModel):
    """A two-sided book normalised to *asks* on both YES and NO.

    Kalshi publishes bids only; asks are derived (YES ask = $1 - best NO bid). Both
    venues therefore arrive here in the same shape, and every downstream calculation
    consumes asks, never last-trade prices.
    """

    platform: Platform
    platform_market_id: str
    observed_at: datetime
    source_timestamp: datetime | None = None
    yes_bids: list[BookLevel] = Field(default_factory=list)
    yes_asks: list[BookLevel] = Field(default_factory=list)
    no_bids: list[BookLevel] = Field(default_factory=list)
    no_asks: list[BookLevel] = Field(default_factory=list)
    provenance: DataProvenance = DataProvenance.LIVE
    raw: dict[str, Any] | None = None

    def asks(self, side: Side) -> list[BookLevel]:
        return self.yes_asks if side == Side.YES else self.no_asks

    def bids(self, side: Side) -> list[BookLevel]:
        return self.yes_bids if side == Side.YES else self.no_bids

    def best_ask(self, side: Side) -> Decimal | None:
        levels = self.asks(side)
        return levels[0].price if levels else None

    def best_bid(self, side: Side) -> Decimal | None:
        levels = self.bids(side)
        return levels[0].price if levels else None

    def depth_notional(self, side: Side) -> Decimal:
        return sum((lvl.notional for lvl in self.asks(side)), Decimal("0"))

    @property
    def is_empty(self) -> bool:
        return not (self.yes_asks or self.no_asks)


# --------------------------------------------------------------------- markets
class NormalizedMarket(StrictModel):
    """The unified market record. One row per tradeable binary question."""

    platform: Platform
    platform_market_id: str
    platform_event_id: str | None = None
    series_ticker: str | None = None

    title: str = ""
    subtitle: str = ""
    normalized_title: str = ""
    description: str = ""
    category: Category = Category.OTHER

    outcomes: list[str] = Field(default_factory=lambda: ["Yes", "No"])
    yes_token_id: str | None = None
    no_token_id: str | None = None
    condition_id: str | None = None

    open_time: datetime | None = None
    close_time: datetime | None = None
    event_occurrence_time: datetime | None = None
    expected_resolution_time: datetime | None = None
    actual_settlement_time: datetime | None = None
    source_timezone: str = "UTC"

    settlement_source: str = ""
    settlement_rules_raw: str = ""
    settlement_rules_normalized: str = ""
    resolution_hash: str = ""

    status: MarketStatus = MarketStatus.UNKNOWN
    result: str | None = None
    accepting_orders: bool = True
    market_type: str = "binary"
    strike_type: str | None = None
    floor_strike: Decimal | None = None
    cap_strike: Decimal | None = None

    tick_size: Decimal = Decimal("0.01")
    price_level_structure: str = "linear_cent"
    min_order_size: Decimal = Decimal("1")
    fee_rate: Decimal = Decimal("0")
    maker_fee_rate: Decimal = Decimal("0")
    fee_type: str = ""

    best_yes_bid: Decimal | None = None
    best_yes_ask: Decimal | None = None
    best_no_bid: Decimal | None = None
    best_no_ask: Decimal | None = None
    spread: Decimal | None = None
    orderbook_depth_usd: Decimal | None = None
    volume_24h: Decimal | None = None
    total_volume: Decimal | None = None
    open_interest: Decimal | None = None
    last_trade_price: Decimal | None = None
    liquidity_usd: Decimal | None = None
    quote_observed_at: datetime | None = None

    negative_risk: bool = False
    provenance: DataProvenance = DataProvenance.LIVE
    raw: dict[str, Any] | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.status == MarketStatus.OPEN and self.accepting_orders


class NormalizedEvent(StrictModel):
    platform: Platform
    platform_event_id: str
    series_ticker: str | None = None
    title: str = ""
    normalized_title: str = ""
    category: Category = Category.OTHER
    close_time: datetime | None = None
    negative_risk: bool = False
    mutually_exclusive: bool = False
    exhaustive: bool = False
    #: Outcome count as reported by the venue. 0 means unknown.
    outcome_count: int = 0
    market_ids: list[str] = Field(default_factory=list)
    provenance: DataProvenance = DataProvenance.LIVE
    raw: dict[str, Any] | None = None


class TradeTick(StrictModel):
    platform: Platform
    platform_trade_id: str
    platform_market_id: str
    traded_at: datetime
    price: Decimal
    size: Decimal
    taker_side: str = ""
    provenance: DataProvenance = DataProvenance.LIVE


class PricePoint(StrictModel):
    timestamp: datetime
    price: Decimal


class ResolutionInfo(StrictModel):
    platform: Platform
    platform_market_id: str
    resolved: bool = False
    result: str | None = None
    yes_payout: Decimal | None = None
    settled_at: datetime | None = None
    settlement_source: str = ""
    disputed: bool = False
    raw: dict[str, Any] | None = None


# ------------------------------------------------------------------- execution
class ExecutionQuote(StrictModel):
    """Result of walking the book for a target quantity.

    ``filled_size`` may be less than requested; every consumer must check it rather
    than assuming the requested size is achievable.
    """

    side: Side
    requested_size: Decimal
    filled_size: Decimal
    average_price: Decimal
    worst_price: Decimal
    notional: Decimal
    levels_consumed: int
    fully_filled: bool


class CostBreakdown(StrictModel):
    """Every component between a quoted ask and true all-in cost per contract."""

    entry_price: Decimal
    platform_fee: Decimal = Decimal("0")
    fee_rounding: Decimal = Decimal("0")
    estimated_slippage: Decimal = Decimal("0")
    transfer_cost: Decimal = Decimal("0")
    capital_cost: Decimal = Decimal("0")
    execution_risk_penalty: Decimal = Decimal("0")

    @property
    def total_cost(self) -> Decimal:
        return (
            self.entry_price
            + self.platform_fee
            + self.fee_rounding
            + self.estimated_slippage
            + self.transfer_cost
            + self.capital_cost
            + self.execution_risk_penalty
        )


class SizedQuote(StrictModel):
    """Executable economics at one specific size (10 / 50 / 100 contracts)."""

    size: Decimal
    filled_size: Decimal
    average_price: Decimal
    total_cost_per_contract: Decimal
    net_ev_per_contract: Decimal
    expected_profit: Decimal
    fully_filled: bool


# ----------------------------------------------------------------- probability
class ProbabilityComponent(StrictModel):
    """One ensemble member's opinion, with the weight it earned."""

    name: str
    probability: Decimal | None
    weight: Decimal
    confidence: Decimal
    detail: str = ""
    data: dict[str, Any] | None = None


class FairProbability(StrictModel):
    fair_probability_mean: Decimal
    fair_probability_low: Decimal
    fair_probability_high: Decimal
    model_confidence: Decimal
    data_freshness_seconds: int | None = None
    evidence_quality: Decimal = Decimal("0")
    model_version: str = ""
    probability_explanation: str = ""
    category: Category = Category.OTHER
    #: Critical honesty flag. False => the estimate is derived from the market's own
    #: price and therefore cannot demonstrate an edge against that same price.
    has_independent_prior: bool = False
    market_implied_probability: Decimal | None = None
    components: list[ProbabilityComponent] = Field(default_factory=list)


class EvidenceRecord(StrictModel):
    source_name: str
    source_url: str = ""
    title: str = ""
    summary: str = ""
    stance: EvidenceStance = EvidenceStance.NEUTRAL
    published_at: datetime | None = None
    event_time: datetime | None = None
    is_novel: bool = True
    source_quality: Decimal = Decimal("0.5")
    conflicts_with: list[str] = Field(default_factory=list)
    provider: str = ""


# -------------------------------------------------------------------- outputs
class ValueCandidate(StrictModel):
    market_id: int | None = None
    platform: Platform
    platform_market_id: str
    title: str
    side: Side
    horizon: str

    entry_price: Decimal
    executable_size: Decimal
    cost: CostBreakdown
    total_cost_per_contract: Decimal
    fair: FairProbability

    net_ev_per_contract: Decimal
    conservative_net_ev: Decimal
    net_roi: Decimal
    sized_quotes: list[SizedQuote] = Field(default_factory=list)
    expected_profit_per_100_usd: Decimal = Decimal("0")
    fractional_kelly: Decimal = Decimal("0")
    recommended_position_cap: Decimal = Decimal("0")
    composite_score: Decimal = Decimal("0")

    spread: Decimal | None = None
    liquidity_usd: Decimal | None = None
    expected_resolution_time: datetime | None = None
    risk_flags: list[str] = Field(default_factory=list)
    provenance: DataProvenance = DataProvenance.LIVE


class ArbLeg(StrictModel):
    platform: Platform
    platform_market_id: str
    market_id: int | None = None
    title: str = ""
    side: Side
    price: Decimal
    size_available: Decimal
    fee_per_contract: Decimal = Decimal("0")
    token_id: str | None = None


class ArbitrageResult(StrictModel):
    kind: ArbitrageKind
    label: ArbitrageLabel
    title: str
    legs: list[ArbLeg]
    gross_edge_per_set: Decimal
    total_cost_per_set: Decimal
    net_profit_per_set: Decimal
    max_executable_sets: Decimal
    max_net_profit: Decimal
    capital_required: Decimal
    net_roi: Decimal
    rule_compatibility: RuleCompatibility = RuleCompatibility.INCOMPATIBLE
    #: Public equivalence verdict. Distinct from ``rule_compatibility``: only
    #: VERIFIED_EQUIVALENT_STRICT licenses a guaranteed-arbitrage claim, whereas
    #: RuleCompatibility.IDENTICAL says nothing about cancellation handling.
    equivalence_verdict: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    quote_age_seconds: int | None = None
    expected_resolution_time: datetime | None = None
    cost_breakdown: dict[str, Any] = Field(default_factory=dict)
    match_id: int | None = None
    provenance: DataProvenance = DataProvenance.LIVE
