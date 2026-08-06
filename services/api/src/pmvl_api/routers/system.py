"""System health, data sources, methodology, and the compliance surface."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pmvl_shared.cadence import DeploymentMode
from pmvl_shared.config import get_settings
from pmvl_shared.enums import DataProvenance
from pmvl_shared.timeutil import utcnow

from pmvl_markets.db_models import (
    ArbitrageOpportunity,
    BacktestRun,
    EvidenceItem,
    JobRun,
    Market,
    MarketMatch,
    ModelPrediction,
    OrderbookSnapshot,
    Recommendation,
    RecommendationSnapshot,
    Settlement,
    Trade,
)
from pmvl_markets.probability.ensemble import MODEL_VERSION

from ..deps import DataMode, DbDep, ModeDep, envelope
from ..pipeline_status import pipeline_status
from ..snapshot_timing import QUOTE_SPREAD_NOTE, snapshot_timing

router = APIRouter(tags=["system"])

#: Set by the serverless entrypoint when the bundled read-only database is in use.
SNAPSHOT_MODE = os.environ.get("PMVL_SNAPSHOT_MODE") == "1"


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": utcnow().isoformat().replace("+00:00", "Z")}


@router.get("/system")
def system(db: Session = DbDep, mode: DataMode = ModeDep) -> dict[str, Any]:
    settings = get_settings()

    counts: dict[str, Any] = {}
    for model in (
        Market, OrderbookSnapshot, Trade, EvidenceItem, ModelPrediction, MarketMatch,
        Recommendation, RecommendationSnapshot, ArbitrageOpportunity, Settlement,
        BacktestRun,
    ):
        counts[model.__tablename__] = db.scalar(
            select(func.count()).select_from(model)
        )

    # Live vs demo split, so the operator can see at a glance whether the database
    # contains synthetic rows at all.
    provenance_split = {}
    for table, model in (("markets", Market), ("recommendations", Recommendation)):
        provenance_split[table] = {
            value: db.scalar(
                select(func.count()).select_from(model).where(model.provenance == value)
            )
            for value in (DataProvenance.LIVE.value, DataProvenance.DEMO.value)
        }

    latest = (
        select(JobRun.job_name, func.max(JobRun.started_at).label("latest"))
        .group_by(JobRun.job_name)
        .subquery()
    )
    jobs = [
        {
            "job_name": j.job_name,
            "status": j.status,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
            "duration_seconds": j.duration_seconds,
            "records_written": j.records_written,
            "error": j.error[:500] if j.error else "",
            "details": j.details or {},
        }
        for j in db.scalars(
            select(JobRun).join(
                latest,
                (JobRun.job_name == latest.c.job_name)
                & (JobRun.started_at == latest.c.latest),
            ).order_by(JobRun.job_name)
        )
    ]

    # SNAPSHOT_MODE is read from the serverless entrypoint's env, and the settings
    # object may have been constructed before it was set, so the router's own view
    # of snapshot mode wins. Both must agree or the cadence caveat goes missing on
    # exactly the deployment that needs it.
    mode = DeploymentMode.READ_ONLY_SNAPSHOT if SNAPSHOT_MODE else settings.deployment_mode
    pipeline = pipeline_status(db, mode)
    timing = snapshot_timing(db)
    # Kept at the top level because clients read it, but it is the single most
    # recent observation in the database - not a capture time for the dataset.
    # `snapshot_timing` carries the spread that makes that unambiguous.
    freshest_quote = timing["freshest_quote_observed_at"]

    return envelope(
        {
            "environment": settings.runtime_environment,
            "runtime_mode": mode.value,
            "deployment_mode": mode.value,
            "deployment": settings.deployment_metadata,
            "worker_status": settings.worker_status,
            "model_version": MODEL_VERSION,
            "row_counts": counts,
            "provenance_split": provenance_split,
            "jobs": jobs,
            "freshest_quote_observed_at": freshest_quote,
            "freshest_quote_observed_at_note": QUOTE_SPREAD_NOTE,
            "snapshot_timing": timing,
            "data_sources": [
                {
                    "name": "Kalshi Trade API v2",
                    "base_url": settings.kalshi_api_base,
                    "auth_required": False,
                    "used_for": "markets, events, series, orderbooks, trades, candlesticks, settlement",
                    "docs": "https://docs.kalshi.com",
                },
                {
                    "name": "Polymarket Gamma API",
                    "base_url": settings.polymarket_gamma_base,
                    "auth_required": False,
                    "used_for": "market and event discovery, rules, fee schedule, negative risk",
                    "docs": "https://docs.polymarket.com",
                },
                {
                    "name": "Polymarket CLOB API",
                    "base_url": settings.polymarket_clob_base,
                    "auth_required": False,
                    "used_for": "per-token orderbooks, midpoint, spread, price history",
                    "docs": "https://docs.polymarket.com",
                },
                {
                    "name": "Polymarket Data API",
                    "base_url": settings.polymarket_data_base,
                    "auth_required": False,
                    "used_for": "public trade prints",
                    "docs": "https://docs.polymarket.com",
                },
                {
                    "name": "Coinbase Exchange",
                    "base_url": settings.coinbase_api_base,
                    "auth_required": False,
                    "used_for": "spot price and realised volatility for the crypto threshold model",
                    "docs": "https://docs.cdp.coinbase.com/exchange/docs/welcome",
                },
                {
                    "name": "Yahoo Finance (chart API)",
                    "base_url": settings.yahoo_finance_base,
                    "auth_required": False,
                    "used_for": "index level and realised volatility for the equity index model",
                    "docs": "https://query1.finance.yahoo.com/v8/finance/chart/",
                },
                {
                    "name": "National Weather Service",
                    "base_url": settings.nws_api_base,
                    "auth_required": False,
                    "used_for": "gridpoint temperature forecasts for the weather model",
                    "docs": "https://www.weather.gov/documentation/services-web-api",
                },
                {
                    "name": "Anthropic (research agent)",
                    "base_url": "https://api.anthropic.com",
                    "auth_required": True,
                    "configured": bool(settings.anthropic_api_key),
                    "enabled": settings.research_enabled,
                    "used_for": "structured evidence extraction; disabled by default",
                    "docs": "https://docs.anthropic.com",
                },
            ],
            # Configured cadence AND whether anything is executing it. These were a
            # bare table of intervals, which described scheduler.py accurately and
            # the running deployment not at all: a reader saw "arbitrage scan: 1
            # minute" on an artefact where no scan had run since it was built.
            "pipeline": pipeline,
            # Retained for existing clients, but every value now carries the
            # deployment's cadence notice rather than standing alone.
            "update_frequencies": {
                job["job_name"]: job["configured_cadence"] for job in pipeline["jobs"]
            },
            "update_frequencies_notice": pipeline["cadence_notice"],
            "trading_execution_enabled": settings.trading_execution_enabled,
            # A serverless deployment ships a pre-built database inside the bundle,
            # so it is frozen at build time. Say so plainly: a stale snapshot
            # presented as a live scan would misrepresent the whole platform.
            "snapshot_mode": SNAPSHOT_MODE,
            "snapshot_notice": (
                "This deployment serves a READ-ONLY SNAPSHOT. Prices, orderbooks "
                "and model estimates are frozen and are NOT live. Run the pipeline "
                "locally (`make ingest && make rank`) for current data. There is no "
                "single capture time: see 'snapshot_timing' for the ingest window, "
                "the freshest, median and oldest quote observations, and the "
                "arbitrage scan time, each named separately."
            ) if SNAPSHOT_MODE else None,
        },
        mode,
    )


@router.get("/system/config")
def config(mode: DataMode = ModeDep) -> dict[str, Any]:
    """Effective configuration with every secret reduced to a presence boolean."""
    return envelope(get_settings().redacted(), mode)


@router.get("/system/eligibility")
def eligibility(
    region: str | None = Query(
        None, description="ISO country or subdivision code, e.g. US-NY, GB, ON"
    ),
) -> dict[str, Any]:
    """Geographic eligibility check for the *future* trading surface.

    Research access is read-only and unrestricted - reading published market data is
    not a regulated activity. This endpoint exists so that when an execution service
    is added it has a gate to consult, and so restricted regions can be told up front
    that trading will not be enabled for them.
    """
    settings = get_settings()
    restricted = settings.restricted_region_list
    normalized = (region or "").strip().upper()
    is_restricted = bool(normalized) and any(
        normalized == r or normalized.startswith(f"{r}-") or r.startswith(f"{normalized}-")
        for r in restricted
    )

    return {
        "region": normalized or None,
        "research_access": "allowed",
        "trading_execution_available": False,
        "trading_execution_reason": (
            "This release has no execution service. It holds no funds, stores no "
            "wallet keys, and places no orders."
        ),
        "would_be_restricted_for_trading": is_restricted,
        "restricted_regions_configured": restricted,
        "note": (
            "Polymarket restricts trading access by jurisdiction and Kalshi is a "
            "CFTC-regulated US exchange. Confirm your own eligibility with each venue "
            "before trading anywhere."
        ),
    }


@router.get("/methodology")
def methodology(mode: DataMode = ModeDep) -> dict[str, Any]:
    """The formulas and decision rules the platform actually uses."""
    return envelope(
        {
            "model_version": MODEL_VERSION,
            "executable_price": {
                "rule": (
                    "Entry price is the volume-weighted average of the ask ladder at "
                    "the requested size. Last-trade prices and midpoints are never "
                    "used as entry prices."
                ),
                "yes_side": "Buying YES consumes the YES ask ladder.",
                "no_side": "Buying NO consumes the NO ask ladder.",
                "kalshi_note": (
                    "Kalshi publishes bids only. Asks are derived: "
                    "YES ask = $1.00 - best NO bid, NO ask = $1.00 - best YES bid."
                ),
                "polymarket_note": (
                    "YES and NO are separate ERC-1155 tokens with independent order "
                    "books; both are fetched. A missing side is reported as empty "
                    "rather than synthesised."
                ),
            },
            "fees": {
                "kalshi_taker": "ceil_to_cent(0.07 x multiplier x C x P x (1-P))",
                "kalshi_maker": "ceil_to_cent(0.0175 x multiplier x C x P x (1-P))",
                "polymarket_taker": "round_5dp(C x rate x P x (1-P)), rate per market from the API",
                "polymarket_maker": "zero - makers are never charged",
                "note": (
                    "Kalshi ceils the fee to the whole cent on the whole order, which "
                    "makes the per-contract fee size-dependent. Small orders are "
                    "disproportionately expensive and the model reflects that."
                ),
            },
            "cost_stack": [
                "executable entry price (VWAP at size)",
                "platform fee",
                "fee rounding",
                "observed book-depth impact",
                "configured transfer allowance (Polygon bridge/gas, amortised; zero on Kalshi)",
                "configured annual capital-cost assumption until expected resolution",
                "modelled latency/slippage pad (reported separately from the headline estimate)",
                "execution risk penalty (cross-venue legs only)",
            ],
            "value": {
                "gross_expected_profit_per_contract": "P(win) - executable entry price",
                "net_ev_per_contract": "P(win) - total executable cost",
                "conservative_net_ev": "fair_probability_low - total executable cost",
                "admission_rule": (
                    "conservative_net_ev must exceed the configured threshold AND the "
                    "fair probability must rest on an independent prior."
                ),
                "no_side_bound": (
                    "For a NO recommendation the conservative bound is 1 - "
                    "fair_probability_high, not 1 - low. Using the latter would be the "
                    "optimistic bound and would systematically overstate NO-side edge."
                ),
                "ranking": (
                    "Multiplicative composite of conservative edge scaled by realisable "
                    "capacity, then discounted for liquidity, spread, model confidence, "
                    "data freshness and time to resolution. Ranking on ROI alone would "
                    "let a 1-cent contract with no depth dominate permanently."
                ),
            },
            "probability": {
                "combination": "log-odds pooling weighted by component confidence",
                "independence_rule": (
                    "A model may not use the target market's own price to justify "
                    "trading against that same price. When every contributing "
                    "component derives from that price, has_independent_prior is false "
                    "and no recommendation can be produced."
                ),
                "independent_components": [
                    "cross-platform consensus (verified equivalent matches only)",
                    "sibling coherence on mutually exclusive exhaustive events",
                    "crypto driftless-GBM threshold model on Coinbase spot + realised vol",
                    "equity-index driftless-GBM model on Yahoo spot + realised vol, "
                    "measured in NYSE trading time rather than calendar time",
                    "weather model on NWS gridpoint forecasts",
                    "research agent (capped, evidence-weighted)",
                ],
                "non_independent_components": [
                    "target market reference prior",
                    "extreme price sanity anchor",
                ],
                "unmodelled_categories": (
                    "Sports, macro and politics return NO OPINION rather than a guess, "
                    "and name the credentialed data feed that would be required."
                ),
                "volatility_time": (
                    "Index variance accrues at very different rates through the day. "
                    "The clock counts NYSE cash-session hours at full weight and "
                    "overnight CME futures hours at 0.10, calibrated so overnight is "
                    "~20% of close-to-close daily variance; time when neither market "
                    "is open (weekends, the 17:00-18:00 ET halt) counts for nothing. "
                    "Calendar time would overstate sigma*sqrt(tau) by ~5x on an "
                    "overnight market and manufacture edge on correctly-priced "
                    "out-of-the-money strikes. Holidays and 13:00 ET early closes are "
                    "honoured, and the clock is asserted to round-trip: one "
                    "close-to-close day equals exactly one trading day."
                ),
                "overnight_anchor": (
                    "Outside cash hours the index print is stale while the underlying "
                    "keeps moving through futures. Spot is re-anchored using the "
                    "front-month future's RETURN since the cash close "
                    "(implied = cash_close * futures_now / futures_at_close), which "
                    "cancels basis, carry and contract rolls without needing to model "
                    "them. Rejected if the reference bar is far from the close or the "
                    "implied move exceeds 8%."
                ),
                "interval": (
                    "Explicitly a conservative uncertainty band, not a formal confidence "
                    "interval: components are not independent draws from a common "
                    "distribution. Built from component uncertainty, inter-component "
                    "disagreement, a staleness penalty and a low-confidence floor."
                ),
            },
            "arbitrage": {
                "executable_definition": [
                    "every leg has sufficient depth right now",
                    "all fees, slippage and capital costs deducted",
                    "settlement conditions substantively identical (rule compatibility = identical)",
                    "cutoff, timezone, measurement basis and settlement source agree",
                    "net profit still strictly greater than zero",
                ],
                "safety_margins": (
                    "Minimum net edge before an arbitrage is published, held in "
                    "configuration rather than scattered through the scanners: 1.5% "
                    "same-venue liquid, 3% same-venue thin, 4% cross-venue liquid, 5% "
                    "cross-venue thin. Cross-venue legs clear a higher bar because "
                    "they add settlement-source divergence, two close times, split "
                    "capital and withdrawal cost, none of which is recoverable if one "
                    "leg fills and the other does not. Unknown depth is treated as "
                    "thin - absent evidence of depth is not evidence of depth."
                ),
                "other_labels": (
                    "Everything failing any precondition is labelled Theoretical, Rule "
                    "Mismatch Risk, Execution Risk, Stale Quote, Insufficient "
                    "Liquidity, Not Guaranteed, or Logical Mispricing."
                ),
                "multi_outcome_guard": (
                    "A complete set is only priced when the number of legs equals the "
                    "venue's own reported outcome count. Partial baskets are refused."
                ),
                "logical_constraints": (
                    "Monotonicity and probability-sum violations are reported as "
                    "mispricings. They are only upgraded when a complete executable "
                    "hedge clears its costs."
                ),
            },
            "backtest": {
                "look_ahead_prevention": (
                    "The engine reads only immutable snapshots frozen at publication. "
                    "It never queries live markets, never re-runs the model, and never "
                    "re-prices an entry. Selection is applied within each publication "
                    "day."
                ),
                "data_quality": DATA_QUALITY_NOTE,
                "benchmark": (
                    "Brier improvement versus the market's own implied probability. A "
                    "model that cannot beat the market price adds no information."
                ),
            },
            "venue_availability": {
                "observed_directly": ["kalshi", "polymarket"],
                "unverified": ["moomoo", "robinhood", "ibkr"],
                "rule": (
                    "Availability is reported only for venues this platform reads "
                    "directly. Brokers that resell exchange event contracts list a "
                    "subset that changes without notice and is gated by jurisdiction "
                    "and account type, and no discovery source for them is wired up "
                    "here. A contract existing on Kalshi is therefore NOT reported as "
                    "available on Moomoo - that status stays 'Unverified'. Inferring "
                    "it would be a confident claim the reader cannot act on."
                ),
            },
            "limitations": [
                "Sports, macro and politics have no independent model in this release.",
                "The equity index model uses REALISED volatility (EWMA of daily "
                "closes), not implied. No keyless forward vol surface exists. When the "
                "short-dated vol term structure is steep the model prices a WIDER "
                "distribution than the market: it reads high in both tails and low "
                "around the money, symmetrically. That is a volatility disagreement, "
                "not an edge, and the market's short-dated view is usually better "
                "informed than a backward-looking estimate. It is left visible rather "
                "than tuned away, and the conservative bound is what stops it becoming "
                "a recommendation - importantly, acting on it would mean systematically "
                "buying tails, which is the historically losing side of that trade.",
                "Index markets worded as touch/barrier events are declined rather than "
                "priced, because no barrier model is fitted for indices.",
                "Cross-platform matching requires an exact rule match before any "
                "arbitrage claim; genuinely identical pairs across these two venues "
                "are rare.",
                "Polymarket expected resolution adds a fixed oracle-latency estimate to "
                "endDate; actual UMA settlement time varies.",
                "Slippage beyond measured book impact is a fixed tick pad, not a "
                "fitted market-impact model.",
                "The backtest cannot model queue position or partial fills.",
            ],
        },
        mode,
    )


DATA_QUALITY_NOTE = (
    "Each simulated trade records how its fill price was derived, and a run's overall "
    "data quality is the worst quality of any trade in it. Candlestick-derived fills "
    "are never presented as orderbook-derived."
)
