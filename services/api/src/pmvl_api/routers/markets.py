"""Market browser and market detail."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from pmvl_shared.enums import (
    DISCOVERABLE_VENUES,
    UNDISCOVERABLE_VENUES,
    availability_for,
)
from pmvl_shared.money import ZERO
from pmvl_shared.timeutil import HORIZON_DELTAS, horizons_for, utcnow

from pmvl_markets.db_models import (
    EvidenceItem,
    Market,
    MarketMatch,
    MarketRule,
    ModelPrediction,
    OrderbookLevel,
    OrderbookSnapshot,
    PriceSnapshot,
    Recommendation,
    Settlement,
)

from ..deps import DataMode, DbDep, ModeDep, apply_provenance, envelope
from ..quotes import bulk_coherent_quotes, coherent_quote

#: Candidate window when ordering by a quote-derived value.
QUOTE_SORT_WINDOW = 400

#: A horizon filter selects markets whose TIGHTEST bucket is the requested one
#: (a 24h market is also within 7d, but ?horizon=7d has always meant "resolves
#: after 24h and within 7d"). The lower bound per bucket mirrors
#: ``horizon_for``'s bucketing exactly, so the SQL filter and the displayed
#: horizon label can never disagree.
_HORIZON_LOWER_BOUNDS: dict[str, timedelta] = {
    "24h": timedelta(0),
    "7d": HORIZON_DELTAS["24h"],
    "30d": HORIZON_DELTAS["7d"],
}

router = APIRouter(prefix="/markets", tags=["markets"])


def _venue_availability(market: Market) -> list[dict[str, Any]]:
    """Where this contract can actually be traded, per venue, with provenance.

    The platform observes Kalshi and Polymarket directly. Brokers that resell Kalshi
    event contracts (Moomoo among them) list a subset that changes without notice and
    is gated by jurisdiction and account type, and there is no public endpoint here
    that enumerates it. Reporting "available on Moomoo" because a contract exists on
    Kalshi would be a claim the user cannot act on, so those venues stay UNVERIFIED.
    """
    observed = {market.platform}
    venues = sorted(DISCOVERABLE_VENUES | UNDISCOVERABLE_VENUES)
    rows = []
    for venue in venues:
        status = availability_for(venue, observed_platforms=observed)
        rows.append(
            {
                "venue": venue,
                "status": status.value,
                "label": status.display_label,
                "is_actionable_claim": status.is_actionable_claim,
                "note": (
                    "No contract-discovery source is wired up for this venue, so "
                    "availability is not inferred from the exchange listing."
                    if venue in UNDISCOVERABLE_VENUES
                    else "Read directly from the venue's public API during ingest."
                ),
            }
        )
    return rows


def _market_row(market: Market, quote=None) -> dict[str, Any]:  # noqa: ANN001
    horizons = horizons_for(market.expected_resolution_time)
    return {
        "id": market.id,
        "platform": market.platform,
        "platform_market_id": market.platform_market_id,
        "title": market.title,
        "subtitle": market.subtitle,
        "category": market.category,
        "status": market.status,
        "accepting_orders": market.accepting_orders,
        # Every price on a row comes from ONE observation. Rendering the venue
        # summary next to a book that was captured two days later showed three
        # different prices for one contract with no way to tell which to believe.
        "best_yes_bid": quote.best_yes_bid if quote else market.best_yes_bid,
        "best_yes_ask": quote.best_yes_ask if quote else market.best_yes_ask,
        "best_no_bid": quote.best_no_bid if quote else market.best_no_bid,
        "best_no_ask": quote.best_no_ask if quote else market.best_no_ask,
        "spread": quote.spread if quote else market.spread,
        # Named explicitly. "orderbook_depth_usd" was ambiguous: the quote resolver
        # sums the ASK side (what a buyer can lift), while OrderbookSnapshot's stored
        # figure is a different total. A single generic label for two definitions is
        # how a liquidity ranking ends up disagreeing with the number beside it.
        "yes_ask_depth_usd": quote.yes_depth_usd if quote else market.orderbook_depth_usd,
        "no_ask_depth_usd": quote.no_depth_usd if quote else None,
        "total_displayed_depth_usd": (
            (quote.yes_depth_usd or ZERO) + (quote.no_depth_usd or ZERO)
            if quote and quote.source == "orderbook"
            else market.orderbook_depth_usd
        ),
        # Retained for existing consumers; equal to yes_ask_depth_usd.
        "orderbook_depth_usd": (
            quote.yes_depth_usd if quote else market.orderbook_depth_usd
        ),
        "quote_source": quote.source if quote else "venue_summary",
        "quote_is_stale_summary": bool(quote and quote.summary_disagrees),
        "volume_24h": market.volume_24h,
        "total_volume": market.total_volume,
        "open_interest": market.open_interest,
        "last_trade_price": market.last_trade_price,
        "tick_size": market.tick_size,
        "fee_rate": market.fee_rate,
        "close_time": market.close_time,
        "expected_resolution_time": market.expected_resolution_time,
        "horizon": horizons[0] if horizons else None,
        "quote_observed_at": (
            quote.observed_at if quote else market.quote_observed_at
        ),
        "result": market.result,
        "provenance": market.provenance,
        # Availability travels with the row so a list can show it without an N+1
        # fetch. Broker availability is never inferred from exchange availability -
        # a Kalshi listing says nothing about what a broker resells.
        "venue_availability": [
            {
                "venue": venue,
                "status": availability_for(
                    venue, observed_platforms={market.platform}
                ).value,
                "label": availability_for(
                    venue, observed_platforms={market.platform}
                ).display_label,
            }
            for venue in ("kalshi", "polymarket", "moomoo")
        ],
    }


def _apply_market_filters(  # noqa: ANN001, ANN201
    stmt,
    *,
    mode: DataMode,
    q: str | None,
    platform: str | None,
    category: str | None,
    status: str | None,
    horizon: str | None,
    min_volume: Decimal | None,
    has_orderbook: bool,
):
    """Apply every SQL-level market filter to data and count queries alike.

    Horizon is expressed in SQL so ``total``, ``offset`` and ``limit`` all refer
    to the same filtered set - filtering in Python after paginating made the
    count overstate and the pages skip. The bounds replicate
    ``horizon_for``'s tightest-bucket semantics exactly.
    """
    stmt = apply_provenance(stmt, Market.provenance, mode)
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Market.title).like(pattern),
                func.lower(Market.normalized_title).like(pattern),
            )
        )
    if platform:
        stmt = stmt.where(Market.platform == platform)
    if category:
        stmt = stmt.where(Market.category == category)
    if status:
        stmt = stmt.where(Market.status == status)
    if horizon:
        now = utcnow()
        stmt = stmt.where(
            Market.expected_resolution_time > now + _HORIZON_LOWER_BOUNDS[horizon],
            Market.expected_resolution_time <= now + HORIZON_DELTAS[horizon],
        )
    if min_volume is not None:
        stmt = stmt.where(Market.volume_24h >= min_volume)
    if has_orderbook:
        stmt = stmt.where(Market.orderbook_depth_usd.is_not(None))
    return stmt


@router.get("")
def list_markets(
    q: str | None = Query(None, description="Substring search on title"),
    platform: str | None = Query(None),
    category: str | None = Query(None),
    horizon: str | None = Query(None, pattern="^(24h|7d|30d)$"),
    status: str | None = Query(None),
    min_volume: Decimal | None = Query(None),
    has_orderbook: bool = Query(False),
    sort: str = Query("volume", pattern="^(volume|spread|resolution|liquidity)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = DbDep,
    mode: DataMode = ModeDep,
) -> dict[str, Any]:
    filter_args = {
        "mode": mode,
        "q": q,
        "platform": platform,
        "category": category,
        "status": status,
        "horizon": horizon,
        "min_volume": min_volume,
        "has_orderbook": has_orderbook,
    }
    stmt = _apply_market_filters(select(Market), **filter_args)

    # Volume and resolution time are market attributes, so they can be ordered in
    # SQL. Spread and liquidity are QUOTE attributes, and the quote a row displays
    # comes from the order book - so ordering them by the stale Market summary would
    # rank rows by numbers the page does not show. Those two are resolved and sorted
    # in Python below.
    quote_sorted = sort in ("spread", "liquidity")
    if not quote_sorted:
        stmt = stmt.order_by(
            {
                "volume": Market.volume_24h.desc(),
                "resolution": Market.expected_resolution_time.asc(),
            }[sort]
        )
    else:
        # Order by volume first so the candidate window is the most-traded markets
        # rather than an arbitrary slice; the honest cost of sorting in Python is
        # stated in the response.
        stmt = stmt.order_by(Market.volume_24h.desc())

    total = db.scalar(
        _apply_market_filters(
            select(func.count()).select_from(Market), **filter_args
        )
    ) or 0

    # Every filter - horizon included - is applied in SQL, so offset and limit
    # paginate exactly the set ``total`` counts. A quote-sorted view still needs
    # its wider candidate window because its ordering is decided in Python after
    # the quotes are resolved; its honest limits are stated in the response.
    window = QUOTE_SORT_WINDOW if quote_sorted else limit
    rows = list(db.scalars(stmt.offset(0 if quote_sorted else offset).limit(window)))
    quotes = bulk_coherent_quotes(db, rows, mode.provenances)
    out = [_market_row(market, quotes.get(market.id)) for market in rows]

    sort_note = None
    ranked_total = None
    if quote_sorted:
        # Sort on the SAME values the row displays.
        def key(row: dict[str, Any]):  # noqa: ANN202
            if sort == "spread":
                value = row.get("spread")
                # Rows with no spread sort last rather than pretending to be tightest.
                return (value is None, Decimal(str(value)) if value is not None else ZERO)
            value = row.get("yes_ask_depth_usd")
            return (value is None, -(Decimal(str(value)) if value is not None else ZERO))

        out.sort(key=key)
        # `total` counts the table; this counts what was actually ranked. Without
        # it a client shows "351-400 of 1,388" under a ranking that only ever
        # considered 400 rows, and an offset past the window returns an empty page
        # while still claiming more exist.
        ranked_total = len(out)
        sort_note = (
            f"Ordered by displayed {sort}. Spread and liquidity come from each "
            f"market's order book, so they are resolved before sorting; the ranking "
            f"covers the {ranked_total} highest-volume markets matching the filters, "
            f"not all {total}. Paging beyond that window returns nothing rather than "
            f"continuing the ranking."
        )
        out = out[offset : offset + limit]

    return envelope(
        out, mode, total=total, count=len(out), offset=offset, limit=limit,
        sort=sort, sort_note=sort_note, ranked_total=ranked_total,
    )


@router.get("/categories")
def categories(db: Session = DbDep, mode: DataMode = ModeDep) -> dict[str, Any]:
    stmt = apply_provenance(
        select(Market.category, func.count(Market.id)).group_by(Market.category),
        Market.provenance,
        mode,
    )
    return envelope(
        [{"category": c, "count": n} for c, n in db.execute(stmt)], mode
    )


def _counterpart_quote_fields(
    db: Session, other: Market | None, mode: DataMode = DataMode.LIVE
) -> dict[str, Any]:
    """Coherent quote fields for a cross-platform counterpart, flattened."""
    if other is None:
        return {
            "other_best_yes_bid": None, "other_best_yes_ask": None,
            "other_best_no_bid": None, "other_best_no_ask": None,
            "other_spread": None, "other_quote_observed_at": None,
            "other_quote_source": "none", "other_quote_is_stale_summary": False,
        }
    quote = coherent_quote(db, other, mode.provenances)
    return {
        "other_best_yes_bid": quote.best_yes_bid,
        "other_best_yes_ask": quote.best_yes_ask,
        "other_best_no_bid": quote.best_no_bid,
        "other_best_no_ask": quote.best_no_ask,
        "other_spread": quote.spread,
        "other_quote_observed_at": quote.observed_at,
        "other_quote_source": quote.source,
        "other_quote_is_stale_summary": quote.summary_disagrees,
    }


@router.get("/{market_id}")
def market_detail(
    market_id: int, db: Session = DbDep, mode: DataMode = ModeDep
) -> dict[str, Any]:
    market = db.scalar(
        apply_provenance(
            select(Market).where(Market.id == market_id), Market.provenance, mode
        )
    )
    if market is None:
        raise HTTPException(
            status_code=404, detail="market not available in this data mode"
        )

    quote = coherent_quote(db, market, mode.provenances)
    rule = db.scalar(select(MarketRule).where(MarketRule.market_id == market_id))

    snapshot_stmt = apply_provenance(
        select(OrderbookSnapshot).where(OrderbookSnapshot.market_id == market_id),
        OrderbookSnapshot.provenance,
        mode,
    )
    snapshot = db.scalar(
        snapshot_stmt.order_by(OrderbookSnapshot.observed_at.desc()).limit(1)
    )
    book: dict[str, Any] = {}
    if snapshot is not None:
        levels = list(
            db.scalars(
                select(OrderbookLevel)
                .where(OrderbookLevel.snapshot_id == snapshot.id)
                .order_by(OrderbookLevel.level_index)
            )
        )
        book = {
            "observed_at": snapshot.observed_at,
            "source_timestamp": snapshot.source_timestamp,
            "provenance": snapshot.provenance,
            "yes_depth_usd": snapshot.yes_depth_usd,
            "no_depth_usd": snapshot.no_depth_usd,
            "yes_asks": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "yes" and l.is_ask
            ],
            "yes_bids": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "yes" and not l.is_ask
            ],
            "no_asks": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "no" and l.is_ask
            ],
            "no_bids": [
                {"price": l.price, "size": l.size}
                for l in levels if l.side == "no" and not l.is_ask
            ],
        }

    price_history = [
        {
            "observed_at": p.observed_at,
            "yes_bid": p.yes_bid,
            "yes_ask": p.yes_ask,
            "mid": p.mid,
            "last_trade_price": p.last_trade_price,
            "provenance": p.provenance,
        }
        for p in db.scalars(
            apply_provenance(
                select(PriceSnapshot).where(PriceSnapshot.market_id == market_id),
                PriceSnapshot.provenance,
                mode,
            ).order_by(PriceSnapshot.observed_at).limit(500)
        )
    ]

    predictions = [
        {
            "created_at": p.created_at,
            # Retained under its original name for existing clients, and published
            # alongside `market_informed_probability`, which is what it has always
            # been: a pool that includes the target market's own price.
            "fair_probability_mean": p.fair_probability_mean,
            "market_informed_probability": p.market_informed_probability,
            "independent_probability": p.independent_probability,
            "independent_probability_low": p.independent_probability_low,
            "independent_probability_high": p.independent_probability_high,
            "conservative_decision_probability": p.conservative_decision_probability,
            "independence": p.independence,
            "fair_probability_low": p.fair_probability_low,
            "fair_probability_high": p.fair_probability_high,
            "model_confidence": p.model_confidence,
            "market_implied_probability": p.market_implied_probability,
            "has_independent_prior": p.has_independent_prior,
            "explanation": p.explanation,
            "components": p.components or [],
            "model_version": p.model_version,
            "evidence_quality": p.evidence_quality,
            "data_freshness_seconds": p.data_freshness_seconds,
            "provenance": p.provenance,
        }
        for p in db.scalars(
            apply_provenance(
                select(ModelPrediction).where(ModelPrediction.market_id == market_id),
                ModelPrediction.provenance,
                mode,
            ).order_by(ModelPrediction.created_at.desc()).limit(100)
        )
    ]

    evidence = [
        {
            "source_name": e.source_name,
            "source_url": e.source_url,
            "title": e.title,
            "summary": e.summary,
            "stance": e.stance,
            "published_at": e.published_at,
            "event_time": e.event_time,
            "is_novel": e.is_novel,
            "source_quality": e.source_quality,
            "provenance": e.provenance,
        }
        for e in db.scalars(
            apply_provenance(
                select(EvidenceItem).where(EvidenceItem.market_id == market_id),
                EvidenceItem.provenance,
                mode,
            ).order_by(EvidenceItem.published_at.desc()).limit(50)
        )
    ]

    matches = []
    for match in db.scalars(
        select(MarketMatch).where(
            or_(MarketMatch.market_a_id == market_id, MarketMatch.market_b_id == market_id)
        )
    ):
        other_id = (
            match.market_b_id if match.market_a_id == market_id else match.market_a_id
        )
        other = db.scalar(
            apply_provenance(
                select(Market).where(Market.id == other_id), Market.provenance, mode
            )
        )
        if other is None:
            continue
        matches.append(
            {
                "other_market_id": other_id,
                "other_platform": other.platform if other else None,
                "other_title": other.title if other else None,
                # The counterpart gets the SAME treatment as the primary market.
                # Reading other.best_yes_ask here would put a stale venue summary
                # next to an order-book price for the market it is compared against,
                # which is the same incoherence one level down.
                **_counterpart_quote_fields(db, other, mode),
                "match_confidence": match.match_confidence,
                "rule_compatibility": match.rule_compatibility,
                "time_compatibility": match.time_compatibility,
                "source_compatibility": match.source_compatibility,
                "polarity_inverted": match.polarity_inverted,
                "mismatch_reasons": match.mismatch_reasons or [],
            }
        )

    recommendations = [
        {
            "created_at": r.created_at,
            "horizon": r.horizon,
            "rank": r.rank,
            "side": r.side,
            "entry_price": r.entry_price,
            "current_price": r.current_price,
            "total_cost_per_contract": r.total_cost_per_contract,
            "net_ev_per_contract": r.net_ev_per_contract,
            "conservative_net_ev": r.conservative_net_ev,
            "state": r.state,
            "settlement_result": r.settlement_result,
            "realized_profit_per_contract": r.realized_profit_per_contract,
            "provenance": r.provenance,
        }
        for r in db.scalars(
            apply_provenance(
                select(Recommendation).where(Recommendation.market_id == market_id),
                Recommendation.provenance,
                mode,
            ).order_by(Recommendation.created_at.desc()).limit(50)
        )
    ]

    settlement = db.scalar(
        apply_provenance(
            select(Settlement).where(Settlement.market_id == market_id),
            Settlement.provenance,
            mode,
        )
    )

    return envelope(
        {
            "market": {
                **_market_row(market, quote),
                "description": market.description,
                "settlement_source": market.settlement_source,
                "settlement_rules_raw": market.settlement_rules_raw,
                "settlement_rules_normalized": market.settlement_rules_normalized,
                "resolution_hash": market.resolution_hash,
                "open_time": market.open_time,
                "event_occurrence_time": market.event_occurrence_time,
                "actual_settlement_time": market.actual_settlement_time,
                "strike_type": market.strike_type,
                "floor_strike": market.floor_strike,
                "cap_strike": market.cap_strike,
                "min_order_size": market.min_order_size,
                "fee_type": market.fee_type,
                "yes_token_id": market.yes_token_id,
                "no_token_id": market.no_token_id,
                "condition_id": market.condition_id,
                "venue_availability": _venue_availability(market),
            },
            "quote": quote.as_dict(),
            "rule": {
                "threshold_semantics": rule.threshold_semantics,
                "threshold_value": rule.threshold_value,
                "comparator": rule.comparator,
                "cutoff_time": rule.cutoff_time,
                "includes_overtime": rule.includes_overtime,
                "uses_revised_data": rule.uses_revised_data,
                "entities": rule.entities or [],
                "normalized_terms": rule.normalized_terms or {},
            } if rule else None,
            "orderbook": book,
            "price_history": price_history,
            "predictions": predictions,
            "evidence": evidence,
            "cross_platform_matches": matches,
            "recommendations": recommendations,
            "settlement": {
                "result": settlement.result,
                "yes_payout": settlement.yes_payout,
                "settled_at": settlement.settled_at,
                "settlement_source": settlement.settlement_source,
                "disputed": settlement.disputed,
                "provenance": settlement.provenance,
            } if settlement else None,
        },
        mode,
    )
