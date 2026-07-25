"""Demo dataset generator - **synthetic data, clearly labelled as such**.

Why this exists
---------------
The backtest, calibration and track-record surfaces can only show anything once
published recommendations have reached their resolution date. That takes real
calendar time. To make those surfaces reviewable and testable on day one, this module
generates a synthetic history.

The honesty contract
--------------------
Every row written here carries ``provenance = DataProvenance.DEMO``. That flag is:

* stored on markets, predictions, recommendations, snapshots, settlements, arbitrage
  rows and backtest runs;
* filtered out of every production API response by default - the API must be asked
  explicitly for demo rows;
* rendered as a prominent banner wherever demo data is shown.

Nothing here is presented as a real opportunity, a real price, or a real result. The
generated probabilities are drawn from a deliberately *imperfect* forecaster so the
calibration plots show a realistic, non-flattering picture rather than a fabricated
success story - the model is given a modest edge and genuine miscalibration, and
roughly a third of the demo recommendations lose.

``ALLOW_DEMO_DATA=false`` makes the write path refuse outright.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pmvl_shared.config import get_settings
from pmvl_shared.enums import (
    Category,
    DataProvenance,
    MarketStatus,
    Platform,
    RecommendationState,
    SettlementResult,
    Side,
)
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ONE, ZERO, clamp_prob, quantize_price, quantize_usd, safe_div
from pmvl_shared.timeutil import utcnow

from ..db_models import (
    Event,
    Market,
    ModelPrediction,
    Recommendation,
    RecommendationSnapshot,
    Settlement,
)
from ..pricing.execution import expected_profit_per_100_usd, fractional_kelly
from ..pricing.fees import taker_fee

log = get_logger(__name__)

#: Prefix on every synthetic market id, so demo rows are identifiable even if the
#: provenance column were ever ignored by a future query.
DEMO_ID_PREFIX = "DEMO-"

DEMO_MODEL_VERSION = "ensemble-v1.0.0-demo"

_TEMPLATES: list[tuple[str, Category, Platform]] = [
    ("Will BTC close above ${strike:,} on {date}?", Category.CRYPTO, Platform.KALSHI),
    ("Will ETH close above ${strike:,} on {date}?", Category.CRYPTO, Platform.POLYMARKET),
    ("Will the high temp in NYC exceed {strike}F on {date}?", Category.WEATHER, Platform.KALSHI),
    ("Will CPI year-over-year exceed {strike}% in the {date} print?", Category.ECONOMICS, Platform.KALSHI),
    ("Will the Fed hold rates at the {date} meeting?", Category.ECONOMICS, Platform.POLYMARKET),
    ("Will Team Alpha beat Team Beta on {date}?", Category.SPORTS, Platform.POLYMARKET),
    ("Will the incumbent lead the {date} national poll?", Category.POLITICS, Platform.POLYMARKET),
    ("Will the S&P 500 close above {strike:,} on {date}?", Category.FINANCE, Platform.KALSHI),
]


@dataclass
class DemoReport:
    markets: int = 0
    predictions: int = 0
    recommendations: int = 0
    snapshots: int = 0
    settlements: int = 0
    days: int = 0
    win_rate: float = 0.0
    total_pnl: str = "0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provenance": DataProvenance.DEMO.value,
            "warning": "SYNTHETIC DEMO DATA - not real markets, prices, or results",
            "markets": self.markets,
            "predictions": self.predictions,
            "recommendations": self.recommendations,
            "snapshots": self.snapshots,
            "settlements": self.settlements,
            "days": self.days,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": self.total_pnl,
        }


def purge_demo_data(session: Session) -> dict[str, int]:
    """Remove every demo row. Live data is untouched."""
    removed: dict[str, int] = {}
    for model in (
        RecommendationSnapshot, Recommendation, ModelPrediction, Settlement, Market, Event,
    ):
        result = session.execute(
            delete(model).where(model.provenance == DataProvenance.DEMO.value)
        )
        removed[model.__tablename__] = result.rowcount or 0

    from ..db_models import ArbitrageOpportunity, BacktestRun, BacktestTrade

    for model in (ArbitrageOpportunity, BacktestRun):
        result = session.execute(
            delete(model).where(model.provenance == DataProvenance.DEMO.value)
        )
        removed[model.__tablename__] = result.rowcount or 0
    session.execute(delete(BacktestTrade).where(BacktestTrade.run_id.like("demo-%")))
    session.flush()
    return removed


def seed_demo_history(
    session: Session,
    *,
    days: int = 45,
    per_day: int = 10,
    seed: int = 42,
    now: datetime | None = None,
) -> DemoReport:
    """Generate a synthetic, settled track record.

    The simulated forecaster is intentionally mediocre: it has a small genuine edge
    over the market price but is also miscalibrated (overconfident in the middle of
    the range). That produces a calibration curve that visibly deviates from the
    diagonal, which is what a real early-stage model looks like. Producing a
    perfectly calibrated, always-profitable demo would misrepresent what the platform
    can do.
    """
    settings = get_settings()
    if not settings.allow_demo_data:
        raise RuntimeError(
            "refusing to seed demo data: ALLOW_DEMO_DATA=false"
        )

    rng = random.Random(seed)
    now = now or utcnow()
    report = DemoReport(days=days)

    purge_demo_data(session)

    event = Event(
        platform=Platform.DEMO.value,
        platform_event_id=f"{DEMO_ID_PREFIX}EVENT",
        title="Demo event group (synthetic)",
        normalized_title="demo event group synthetic",
        category=Category.OTHER.value,
        provenance=DataProvenance.DEMO.value,
        created_at=now,
    )
    session.add(event)
    session.flush()

    total_pnl = ZERO
    wins = 0
    graded = 0

    for day_offset in range(days, 0, -1):
        # A fixed publication hour keeps one calendar day per offset. Randomising the
        # hour lets two different offsets land on the same date, which collides both
        # the market unique key and the daily snapshot grouping.
        published_at = (now - timedelta(days=day_offset)).replace(
            hour=13, minute=0, second=0, microsecond=0
        )
        snapshot_day: date = published_at.date()

        for rank in range(1, per_day + 1):
            template, category, platform = _TEMPLATES[rng.randrange(len(_TEMPLATES))]
            horizon = rng.choice(["24h", "24h", "7d", "7d", "30d"])
            hours_ahead = {"24h": rng.uniform(2, 24), "7d": rng.uniform(24, 168),
                           "30d": rng.uniform(168, 720)}[horizon]
            resolution_at = published_at + timedelta(hours=hours_ahead)
            # Only produce history that has actually resolved by "now".
            if resolution_at >= now:
                resolution_at = published_at + timedelta(hours=min(hours_ahead, 12))
                if resolution_at >= now:
                    continue

            strike = rng.choice([60000, 65000, 70000, 85, 3, 5200, 4800])
            title = template.format(
                strike=strike, date=(published_at + timedelta(days=1)).strftime("%b %d, %Y")
            )

            # ---- the "true" probability, which the demo world settles on --------
            true_p = D(str(round(rng.betavariate(2.2, 2.2), 4)))

            # ---- market price: true probability plus noise and a fee-shaped spread
            market_noise = D(str(round(rng.gauss(0, 0.06), 4)))
            market_p = clamp_prob(true_p + market_noise)
            tick = Decimal("0.01")
            half_spread = D(str(rng.choice([0.005, 0.01, 0.015])))
            yes_ask = quantize_price(clamp_prob(market_p + half_spread))
            yes_bid = quantize_price(clamp_prob(market_p - half_spread))

            # ---- the model's view: a small real edge, plus real miscalibration ---
            # The model sees a noisy signal of the truth. It is then made
            # overconfident by pushing its estimate away from 0.5, which is the most
            # common real failure mode and shows up clearly on a reliability diagram.
            signal = clamp_prob(true_p + D(str(round(rng.gauss(0, 0.075), 4))))
            overconfidence = D("1.18")
            logit = math.log(float(signal) / (1 - float(signal))) * float(overconfidence)
            model_p = clamp_prob(D(str(round(1 / (1 + math.exp(-logit)), 6))))

            side = Side.YES if model_p > market_p else Side.NO
            entry = yes_ask if side == Side.YES else quantize_price(ONE - yes_bid)
            win_p_model = model_p if side == Side.YES else ONE - model_p
            interval = D(str(round(rng.uniform(0.05, 0.16), 4)))
            low = clamp_prob(win_p_model - interval)
            high = clamp_prob(win_p_model + interval)

            contracts = D(100)
            fee_rate = D("0.07") if platform == Platform.KALSHI else D("0.05")
            fee = safe_div(
                taker_fee(platform, contracts, entry, rate=fee_rate), contracts
            )
            slippage = tick
            transfer = D("0.005") if platform == Platform.POLYMARKET else ZERO
            total_cost = quantize_usd(entry + fee + slippage + transfer)

            net_ev = quantize_usd(win_p_model - total_cost)
            conservative_ev = quantize_usd(low - total_cost)
            confidence = D(str(round(rng.uniform(0.32, 0.86), 4)))

            # Keyed on the day offset, which is unique by construction.
            market_id = f"{DEMO_ID_PREFIX}{platform.value[:3].upper()}-D{day_offset:03d}-{rank:02d}"
            market = Market(
                platform=platform.value,
                platform_market_id=market_id,
                event_id=event.id,
                title=f"[DEMO] {title}",
                subtitle="synthetic",
                normalized_title=title.lower(),
                description="Synthetic demo market. Not a real market on any venue.",
                category=category.value,
                outcomes=["Yes", "No"],
                open_time=published_at - timedelta(days=3),
                close_time=resolution_at,
                expected_resolution_time=resolution_at,
                actual_settlement_time=resolution_at,
                settlement_source="synthetic demo source",
                settlement_rules_raw="Synthetic demo market with no real settlement source.",
                settlement_rules_normalized="demo",
                status=MarketStatus.SETTLED.value,
                accepting_orders=False,
                tick_size=tick,
                fee_rate=fee_rate,
                fee_type="quadratic" if platform == Platform.KALSHI else "general_fees",
                best_yes_bid=yes_bid,
                best_yes_ask=yes_ask,
                best_no_bid=quantize_price(ONE - yes_ask),
                best_no_ask=quantize_price(ONE - yes_bid),
                spread=quantize_price(yes_ask - yes_bid),
                volume_24h=D(str(rng.randint(2000, 90000))),
                total_volume=D(str(rng.randint(20000, 900000))),
                orderbook_depth_usd=D(str(rng.randint(400, 20000))),
                last_trade_price=market_p,
                quote_observed_at=published_at,
                provenance=DataProvenance.DEMO.value,
                created_at=published_at,
            )
            session.add(market)
            session.flush()
            report.markets += 1

            prediction = ModelPrediction(
                market_id=market.id,
                model_version=DEMO_MODEL_VERSION,
                fair_probability_mean=model_p,
                fair_probability_low=clamp_prob(model_p - interval),
                fair_probability_high=clamp_prob(model_p + interval),
                model_confidence=confidence,
                data_freshness_seconds=rng.randint(60, 5400),
                evidence_quality=D(str(round(rng.uniform(0, 0.7), 3))),
                has_independent_prior=True,
                market_implied_probability=market_p,
                components=[{"name": "demo_synthetic", "probability": str(model_p)}],
                explanation="Synthetic demo prediction from a deliberately imperfect forecaster.",
                category=category.value,
                provenance=DataProvenance.DEMO.value,
                created_at=published_at,
            )
            session.add(prediction)
            session.flush()
            report.predictions += 1

            # ---- resolve against the TRUE probability, not the model's ---------
            outcome_yes = rng.random() < float(true_p)
            result = SettlementResult.YES if outcome_yes else SettlementResult.NO
            yes_payout = ONE if outcome_yes else ZERO
            payout = yes_payout if side == Side.YES else ONE - yes_payout
            realized = quantize_usd(payout - total_cost)

            batch_id = f"demo-{snapshot_day:%Y%m%d}"
            recommendation = Recommendation(
                batch_id=batch_id,
                market_id=market.id,
                prediction_id=prediction.id,
                horizon=horizon,
                rank=rank,
                side=side.value,
                entry_price=entry,
                executable_size=D(str(rng.randint(120, 4000))),
                total_cost_per_contract=total_cost,
                fair_probability=win_p_model,
                fair_probability_low=low,
                fair_probability_high=high,
                net_ev_per_contract=net_ev,
                conservative_net_ev=conservative_ev,
                net_roi=quantize_usd(safe_div(net_ev, total_cost)),
                expected_profit_10=quantize_usd(net_ev * D(10)),
                expected_profit_50=quantize_usd(net_ev * D(50)),
                expected_profit_100=quantize_usd(net_ev * D(100)),
                expected_profit_per_100_usd=expected_profit_per_100_usd(net_ev, total_cost),
                fractional_kelly=fractional_kelly(low, total_cost),
                recommended_position_cap=D(str(rng.randint(50, 1500))),
                composite_score=quantize_usd(conservative_ev * D(str(rng.uniform(1, 6)))),
                model_confidence=confidence,
                spread=quantize_price(yes_ask - yes_bid),
                liquidity_usd=market.orderbook_depth_usd,
                risk_flags=["demo_data"],
                cost_breakdown={
                    "entry_price": str(entry), "platform_fee": str(quantize_usd(fee)),
                    "estimated_slippage": str(slippage), "transfer_cost": str(transfer),
                },
                model_version=DEMO_MODEL_VERSION,
                expected_resolution_time=resolution_at,
                state=RecommendationState.SETTLED.value,
                current_price=entry,
                current_net_ev=net_ev,
                state_checked_at=now,
                settlement_result=result.value,
                realized_profit_per_contract=realized,
                settled_at=resolution_at,
                provenance=DataProvenance.DEMO.value,
                created_at=published_at,
            )
            session.add(recommendation)
            session.flush()
            report.recommendations += 1

            session.add(
                RecommendationSnapshot(
                    batch_id=batch_id,
                    snapshot_date=snapshot_day,
                    recommendation_id=recommendation.id,
                    recommendation_created_at=published_at,
                    market_id=market.id,
                    platform=platform.value,
                    platform_market_id=market_id,
                    market_title=market.title,
                    horizon=horizon,
                    rank=rank,
                    side=side.value,
                    entry_price_at_recommendation=entry,
                    total_cost_at_recommendation=total_cost,
                    executable_size=recommendation.executable_size,
                    fair_probability=win_p_model,
                    fair_probability_low=low,
                    fair_probability_high=high,
                    expected_value=net_ev,
                    conservative_net_ev=conservative_ev,
                    model_confidence=confidence,
                    model_version=DEMO_MODEL_VERSION,
                    expected_resolution_time=resolution_at,
                    evidence_snapshot={
                        "items": [],
                        "explanation": prediction.explanation,
                        "has_independent_prior": True,
                        "market_implied_probability": str(market_p),
                    },
                    orderbook_snapshot={
                        "observed_at": published_at.isoformat(),
                        "best_yes_ask": str(yes_ask),
                        "best_no_ask": str(quantize_price(ONE - yes_bid)),
                        "yes_asks": [
                            {"price": str(yes_ask), "size": str(recommendation.executable_size)}
                        ],
                        "no_asks": [
                            {"price": str(quantize_price(ONE - yes_bid)),
                             "size": str(recommendation.executable_size)}
                        ],
                    },
                    risk_flags=["demo_data"],
                    final_result=result.value,
                    realized_profit_per_contract=realized,
                    realized_profit_at_100_usd=quantize_usd(
                        realized * safe_div(D(100), total_cost)
                    ),
                    settled_at=resolution_at,
                    provenance=DataProvenance.DEMO.value,
                )
            )
            report.snapshots += 1

            session.add(
                Settlement(
                    market_id=market.id,
                    platform=platform.value,
                    platform_market_id=market_id,
                    result=result.value,
                    yes_payout=yes_payout,
                    settled_at=resolution_at,
                    settlement_source="synthetic demo source",
                    provenance=DataProvenance.DEMO.value,
                    created_at=resolution_at,
                )
            )
            report.settlements += 1

            total_pnl += realized
            graded += 1
            if realized > 0:
                wins += 1

    session.flush()
    report.win_rate = wins / graded if graded else 0.0
    report.total_pnl = str(quantize_usd(total_pnl))

    log.info(
        "seeded DEMO history: %d recommendations over %d days, win rate %.1f%%, "
        "synthetic P&L %s per contract",
        report.recommendations, days, report.win_rate * 100, report.total_pnl,
    )
    return report
