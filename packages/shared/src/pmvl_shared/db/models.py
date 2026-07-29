"""ORM models.

Design notes that matter for correctness:

* All timestamps are UTC. The DB never stores local time.
* All monetary/price columns use :class:`Money` (Decimal-exact on both backends).
* Every table that can influence a displayed opportunity carries ``provenance``.
* Snapshot tables (``recommendation_snapshots``, ``orderbook_snapshots``) are
  append-only: nothing in the codebase updates or deletes them, which is what makes
  the public track record auditable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..enums import DataProvenance
from .base import Base, JSONColumn, Money


def _utc_col(**kw) -> Mapped[datetime]:  # noqa: ANN003
    return mapped_column(DateTime(timezone=True), **kw)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- venues
class PlatformRow(Base, TimestampMixin):
    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    api_base: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    #: e.g. "quadratic" (Kalshi) / "general_fees" (Polymarket)
    fee_model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Event(Base, TimestampMixin):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("platform", "platform_event_id", name="uq_event_platform_id"),
        Index("ix_events_platform_close", "platform", "close_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    series_ticker: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False, default="", index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other", index=True)
    close_time: Mapped[datetime | None] = _utc_col(nullable=True)
    #: Polymarket "negative risk" events share collateral across mutually exclusive
    #: outcomes; multi-outcome arbitrage must know this to price a complete set.
    negative_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mutually_exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exhaustive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Number of outcomes the *venue* says this event has. Counting the rows we
    #: happen to have ingested instead would be circular: a partially-ingested event
    #: would look complete to us and its cheap subset would be reported as a
    #: guaranteed complete set. 0 means "unknown", which blocks any completeness
    #: claim rather than assuming one.
    outcome_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provenance: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DataProvenance.LIVE, index=True
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)

    markets: Mapped[list["Market"]] = relationship(back_populates="event")


class Market(Base, TimestampMixin):
    __tablename__ = "markets"
    __table_args__ = (
        UniqueConstraint("platform", "platform_market_id", name="uq_market_platform_id"),
        Index("ix_markets_status_resolution", "status", "expected_resolution_time"),
        Index("ix_markets_platform_category", "platform", "category"),
        Index("ix_markets_normalized_title", "normalized_title"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform_market_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    platform_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    series_ticker: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subtitle: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other", index=True)

    #: JSON list of outcome labels; binary markets are ["Yes", "No"].
    outcomes: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    yes_token_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    no_token_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    condition_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # The four distinct times the spec insists on keeping separate.
    open_time: Mapped[datetime | None] = _utc_col(nullable=True)
    close_time: Mapped[datetime | None] = _utc_col(nullable=True)
    event_occurrence_time: Mapped[datetime | None] = _utc_col(nullable=True)
    expected_resolution_time: Mapped[datetime | None] = _utc_col(nullable=True, index=True)
    actual_settlement_time: Mapped[datetime | None] = _utc_col(nullable=True)
    source_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    settlement_source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    settlement_rules_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    settlement_rules_normalized: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Deterministic digest of the normalized rule terms; equal hashes are a
    #: necessary (not sufficient) condition for "identical" rule compatibility.
    resolution_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", index=True)
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    accepting_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    market_type: Mapped[str] = mapped_column(String(32), nullable=False, default="binary")
    strike_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    floor_strike: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    cap_strike: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    tick_size: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0.01"))
    price_level_structure: Mapped[str] = mapped_column(String(32), nullable=False, default="linear_cent")
    min_order_size: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    #: Per-market taker fee rate; sourced from the venue API where exposed.
    fee_rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    maker_fee_rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    fee_type: Mapped[str] = mapped_column(String(48), nullable=False, default="")

    # Latest top-of-book, denormalised for fast list rendering.
    best_yes_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    best_yes_ask: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    best_no_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    best_no_ask: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    spread: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    orderbook_depth_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    volume_24h: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    total_volume: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    open_interest: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    last_trade_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    liquidity_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    quote_observed_at: Mapped[datetime | None] = _utc_col(nullable=True)

    provenance: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DataProvenance.LIVE, index=True
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)

    event: Mapped["Event | None"] = relationship(back_populates="markets")


class Outcome(Base, TimestampMixin):
    """One tradeable leg. Binary markets get two rows; multi-outcome events get N."""

    __tablename__ = "outcomes"
    __table_args__ = (
        UniqueConstraint("market_id", "label", name="uq_outcome_market_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    index_in_market: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_yes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    best_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    best_ask: Mapped[Decimal | None] = mapped_column(Money, nullable=True)


class MarketRule(Base, TimestampMixin):
    """Structured settlement terms extracted from free-text rules."""

    __tablename__ = "market_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    settlement_source_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    settlement_source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: "reach" | "exceed" | "close_above" | "touch_intraday" | "at_or_above" | ...
    threshold_semantics: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    threshold_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    comparator: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    cutoff_time: Mapped[datetime | None] = _utc_col(nullable=True)
    cutoff_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    includes_overtime: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    uses_revised_data: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    entities: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    normalized_terms: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    resolution_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)


class MarketMatch(Base, TimestampMixin):
    """A candidate cross-platform pairing plus its compatibility audit."""

    __tablename__ = "market_matches"
    __table_args__ = (
        UniqueConstraint("market_a_id", "market_b_id", name="uq_match_pair"),
        Index("ix_match_confidence", "match_confidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_a_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    market_b_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    match_confidence: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    rule_compatibility: Mapped[str] = mapped_column(String(24), nullable=False, default="incompatible")
    time_compatibility: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    source_compatibility: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    #: {"a_yes": "b_yes"} or {"a_yes": "b_no"} when polarity is inverted.
    outcome_mapping: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    polarity_inverted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_hash_a: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    resolution_hash_b: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    mismatch_reasons: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    verified_by: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    llm_assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ------------------------------------------------------------------ market data
class OrderbookSnapshot(Base):
    """Append-only. One row per book capture; levels live in a child table."""

    __tablename__ = "orderbook_snapshots"
    __table_args__ = (
        Index("ix_ob_market_time", "market_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = _utc_col(nullable=False)
    #: Venue-reported book timestamp, which can lag ``observed_at``.
    source_timestamp: Mapped[datetime | None] = _utc_col(nullable=True)
    best_yes_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    best_yes_ask: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    best_no_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    best_no_ask: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    yes_depth_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    no_depth_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    provenance: Mapped[str] = mapped_column(String(16), nullable=False, default=DataProvenance.LIVE)
    raw_payload: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)

    levels: Mapped[list["OrderbookLevel"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class OrderbookLevel(Base):
    __tablename__ = "orderbook_levels"
    __table_args__ = (
        Index("ix_oblevel_snapshot_side", "snapshot_id", "side", "is_ask"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("orderbook_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # yes | no
    is_ask: Mapped[bool] = mapped_column(Boolean, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    size: Mapped[Decimal] = mapped_column(Money, nullable=False)
    level_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    snapshot: Mapped["OrderbookSnapshot"] = relationship(back_populates="levels")


class PriceSnapshot(Base):
    """Compact time series for charts; cheaper than replaying full books."""

    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_price_market_time", "market_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = _utc_col(nullable=False)
    yes_bid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    yes_ask: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    mid: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    last_trade_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    volume_24h: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="poll")
    provenance: Mapped[str] = mapped_column(String(16), nullable=False, default=DataProvenance.LIVE)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("platform", "platform_trade_id", name="uq_trade_platform_id"),
        Index("ix_trades_market_time", "market_id", "traded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_trade_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market_id: Mapped[int | None] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=True
    )
    traded_at: Mapped[datetime] = _utc_col(nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    size: Mapped[Decimal] = mapped_column(Money, nullable=False)
    taker_side: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    provenance: Mapped[str] = mapped_column(String(16), nullable=False, default=DataProvenance.LIVE)


# ------------------------------------------------------------------- research
class EvidenceItem(Base, TimestampMixin):
    __tablename__ = "evidence_items"
    __table_args__ = (Index("ix_evidence_market_time", "market_id", "published_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    source_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stance: Mapped[str] = mapped_column(String(24), nullable=False, default="neutral")
    #: When the source published it vs. when the underlying event happened - a
    #: republished wire story is not new information.
    published_at: Mapped[datetime | None] = _utc_col(nullable=True)
    event_time: Mapped[datetime | None] = _utc_col(nullable=True)
    is_novel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_quality: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0.5"))
    conflicts_with: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    provider: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    provenance: Mapped[str] = mapped_column(String(16), nullable=False, default=DataProvenance.LIVE)
    raw_payload: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)


class ModelVersion(Base, TimestampMixin):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_model_name_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    config: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    #: Fitted calibration parameters, if any, plus the walk-forward window they came
    #: from. Kept so a historical prediction can be reproduced exactly.
    calibration: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)


class ModelPrediction(Base, TimestampMixin):
    __tablename__ = "model_predictions"
    __table_args__ = (Index("ix_pred_market_time", "market_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    fair_probability_mean: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fair_probability_low: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fair_probability_high: Mapped[Decimal] = mapped_column(Money, nullable=False)
    model_confidence: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    data_freshness_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_quality: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    #: False when the only information available was the market's own price. Such a
    #: prediction can never produce a value recommendation.
    has_independent_prior: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    market_implied_probability: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    # The three probability classes. `fair_probability_mean` above pools every
    # component including the target market's own price, so it is the
    # market-informed figure and is stored again under that name; publishing it as
    # "fair probability" alone let the model partly agree with itself and called
    # the residual an edge. Nullable because historical rows predate the split and
    # must not be backfilled with a guess.
    market_informed_probability: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    independent_probability: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    independent_probability_low: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    independent_probability_high: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    #: What eligibility is decided on. NULL means there is no independent estimate,
    #: which is not a probability of zero - the question has no answer for this row.
    conservative_decision_probability: Mapped[Decimal | None] = mapped_column(
        Money, nullable=True
    )
    #: Which components backed each class, and the distinct correlation groups.
    independence: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    components: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    provenance: Mapped[str] = mapped_column(String(16), nullable=False, default=DataProvenance.LIVE)


# ------------------------------------------------------- recommendations & arb
class Recommendation(Base, TimestampMixin):
    """Mutable *current* view of a recommendation (state, latest price)."""

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_rec_horizon_rank", "horizon", "rank"),
        Index("ix_rec_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prediction_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_predictions.id", ondelete="SET NULL"), nullable=True
    )
    horizon: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    entry_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    executable_size: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    total_cost_per_contract: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fair_probability: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fair_probability_low: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fair_probability_high: Mapped[Decimal] = mapped_column(Money, nullable=False)
    net_ev_per_contract: Mapped[Decimal] = mapped_column(Money, nullable=False)
    conservative_net_ev: Mapped[Decimal] = mapped_column(Money, nullable=False)
    net_roi: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    expected_profit_10: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    expected_profit_50: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    expected_profit_100: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    expected_profit_per_100_usd: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    fractional_kelly: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    recommended_position_cap: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    composite_score: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    model_confidence: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    spread: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    liquidity_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    risk_flags: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    cost_breakdown: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence_updated_at: Mapped[datetime | None] = _utc_col(nullable=True)
    expected_resolution_time: Mapped[datetime | None] = _utc_col(nullable=True)

    state: Mapped[str] = mapped_column(String(32), nullable=False, default="still_actionable")
    current_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    current_net_ev: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    state_checked_at: Mapped[datetime | None] = _utc_col(nullable=True)

    settlement_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    realized_profit_per_contract: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    settled_at: Mapped[datetime | None] = _utc_col(nullable=True)

    provenance: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DataProvenance.LIVE, index=True
    )


class RecommendationSnapshot(Base):
    """Immutable record of what was published, exactly as published.

    Nothing in the codebase updates or deletes rows in this table. The public track
    record reads from here so that a later price move cannot rewrite history.
    """

    __tablename__ = "recommendation_snapshots"
    __table_args__ = (
        Index("ix_snapshot_batch_horizon", "batch_id", "horizon"),
        Index("ix_snapshot_created", "recommendation_created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_date: Mapped[datetime] = mapped_column(Date, nullable=False, index=True)
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), nullable=True
    )
    recommendation_created_at: Mapped[datetime] = _utc_col(nullable=False)
    market_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_market_id: Mapped[str] = mapped_column(String(256), nullable=False)
    market_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    horizon: Mapped[str] = mapped_column(String(8), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    entry_price_at_recommendation: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total_cost_at_recommendation: Mapped[Decimal] = mapped_column(Money, nullable=False)
    executable_size: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    fair_probability: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fair_probability_low: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fair_probability_high: Mapped[Decimal] = mapped_column(Money, nullable=False)
    expected_value: Mapped[Decimal] = mapped_column(Money, nullable=False)
    conservative_net_ev: Mapped[Decimal] = mapped_column(Money, nullable=False)
    model_confidence: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    expected_resolution_time: Mapped[datetime | None] = _utc_col(nullable=True)
    #: Frozen copy of the evidence and orderbook that justified the call.
    evidence_snapshot: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    orderbook_snapshot: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    risk_flags: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)

    final_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    realized_profit_per_contract: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    realized_profit_at_100_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    settled_at: Mapped[datetime | None] = _utc_col(nullable=True)
    provenance: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DataProvenance.LIVE, index=True
    )


class ArbitrageOpportunity(Base, TimestampMixin):
    __tablename__ = "arbitrage_opportunities"
    __table_args__ = (
        Index("ix_arb_kind_label", "kind", "label"),
        Index("ix_arb_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: [{market_id, platform, side, price, size, fee, ...}] - one entry per leg.
    legs: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    gross_edge_per_set: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    total_cost_per_set: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    net_profit_per_set: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    max_executable_sets: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    max_net_profit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    capital_required: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    net_roi: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    rule_compatibility: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    #: Public equivalence verdict; only VERIFIED_EQUIVALENT_STRICT licenses a
    #: guaranteed-arbitrage claim.
    equivalence_verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_matches.id", ondelete="SET NULL"), nullable=True
    )
    risk_flags: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    quote_age_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_resolution_time: Mapped[datetime | None] = _utc_col(nullable=True)
    cost_breakdown: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    provenance: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DataProvenance.LIVE, index=True
    )


class Settlement(Base, TimestampMixin):
    __tablename__ = "settlements"
    __table_args__ = (
        UniqueConstraint("platform", "platform_market_id", name="uq_settlement_market"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int | None] = mapped_column(
        ForeignKey("markets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_market_id: Mapped[str] = mapped_column(String(256), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Terminal YES payout: 1, 0, or 0.5 for a 50-50 resolution.
    yes_payout: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    settled_at: Mapped[datetime | None] = _utc_col(nullable=True)
    settlement_source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    disputed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provenance: Mapped[str] = mapped_column(String(16), nullable=False, default=DataProvenance.LIVE)
    raw_payload: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)


# ---------------------------------------------------------------- backtesting
class BacktestRun(Base, TimestampMixin):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    window_start: Mapped[datetime | None] = _utc_col(nullable=True)
    window_end: Mapped[datetime | None] = _utc_col(nullable=True)
    walk_forward: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    n_recommendations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_settled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    calibration: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provenance: Mapped[str] = mapped_column(String(16), nullable=False, default=DataProvenance.LIVE)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (Index("ix_bt_run_time", "run_id", "entered_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_id: Mapped[int] = mapped_column(Integer, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    market_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entered_at: Mapped[datetime] = _utc_col(nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    fees: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    contracts: Mapped[Decimal] = mapped_column(Money, nullable=False)
    stake: Mapped[Decimal] = mapped_column(Money, nullable=False)
    predicted_probability: Mapped[Decimal] = mapped_column(Money, nullable=False)
    market_probability: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payout: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    settled_at: Mapped[datetime | None] = _utc_col(nullable=True)
    data_quality: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_jobrun_name_started", "job_name", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = _utc_col(nullable=False)
    finished_at: Mapped[datetime | None] = _utc_col(nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    records_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
