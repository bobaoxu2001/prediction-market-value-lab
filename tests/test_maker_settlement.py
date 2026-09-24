"""Holding a maker fill to settlement: the secondary metric of the multi-day study.

Each test is a way the number could flatter the strategy: paying the wrong side, paying
a scalar settlement as a full dollar, scoring an unsettled market's quotes as zeros
that pull an unknown toward "nothing happened", or letting the second metric claim a
result at the confidence the first one needed.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pmvl_markets.arbitrage.maker_experiment import (
    FillEstimator,
    MakerQuote,
    QuotePolicy,
    settlement_pnl_per_contract,
)
from pmvl_markets.arbitrage.maker_study import (
    SETTLEMENT_FAMILY_SIZE,
    pool_settlement_holding,
)
from pmvl_shared.enums import Side

from tests.test_maker_study import T0, _session


def _quote(side: Side, price: str, size: str = "50") -> MakerQuote:
    return MakerQuote(
        market="M", event_ticker="EV", side=side, price=Decimal(price), size=Decimal(size),
        placed_at=T0, expires_at=T0 + timedelta(seconds=60), queue_ahead=Decimal("0"),
        spread_at_placement=Decimal("0.05"), snapshot_sequence=0,
    )


# ------------------------------------------------------------ one contract
def test_a_yes_fill_is_paid_what_yes_settled_for():
    assert settlement_pnl_per_contract(_quote(Side.YES, "0.40"), Decimal("1"), QuotePolicy()) == Decimal("0.60")
    assert settlement_pnl_per_contract(_quote(Side.YES, "0.40"), Decimal("0"), QuotePolicy()) == Decimal("-0.40")


def test_a_no_fill_is_paid_the_complement():
    """A NO bid bought NO; it wins when YES settles at zero."""
    assert settlement_pnl_per_contract(_quote(Side.NO, "0.55"), Decimal("0"), QuotePolicy()) == Decimal("0.45")
    assert settlement_pnl_per_contract(_quote(Side.NO, "0.55"), Decimal("1"), QuotePolicy()) == Decimal("-0.55")


def test_a_scalar_settlement_pays_its_partial_dollar_not_a_whole_one():
    assert settlement_pnl_per_contract(_quote(Side.YES, "0.40"), Decimal("0.3"), QuotePolicy()) == Decimal("-0.10")
    assert settlement_pnl_per_contract(_quote(Side.NO, "0.40"), Decimal("0.3"), QuotePolicy()) == Decimal("0.30")


def test_a_maker_fee_is_charged_where_the_series_charges_one():
    free = settlement_pnl_per_contract(_quote(Side.YES, "0.40"), Decimal("1"), QuotePolicy())
    charged = settlement_pnl_per_contract(
        _quote(Side.YES, "0.40"), Decimal("1"), QuotePolicy(maker_fee_rate=Decimal("0.0175"))
    )
    assert charged < free


# ------------------------------------------------------------ per quote
def test_unfilled_quotes_score_zero_and_still_count_as_placed():
    session = _session(1, "2026-09-19", base=T0, fill=True)
    values, sizes, unsettled = session.report.settlement_values(
        FillEstimator.QUEUE_MODEL, {"M": Decimal("1")}
    )
    assert unsettled == 0
    assert len(values) == session.report.quotes_placed
    filled = [v for v, z in zip(values, sizes) if z > 0]
    assert len(filled) == session.report.fills(FillEstimator.QUEUE_MODEL) >= 1
    assert all(v == 0 for v, z in zip(values, sizes) if z == 0)


def test_an_unsettled_market_is_left_out_and_counted_rather_than_scored_as_zero():
    session = _session(1, "2026-09-19", base=T0, fill=True)
    values, _, unsettled = session.report.settlement_values(FillEstimator.QUEUE_MODEL, {})
    assert values == []
    assert unsettled == session.report.quotes_placed


# ------------------------------------------------------------ pooled
def test_pooling_reports_unsettled_quotes_and_the_value_of_a_filled_contract():
    sessions = [_session(i, "2026-09-19", base=T0 + timedelta(hours=i), fill=True) for i in range(1, 4)]
    won = pool_settlement_holding(sessions, {"M": Decimal("1")})[0]
    lost = pool_settlement_holding(sessions, {"M": Decimal("0")})[0]
    assert won.quotes_unsettled == 0 and won.fills == 3
    assert won.pnl_per_filled_contract == Decimal("0.60")
    assert lost.pnl_per_filled_contract == Decimal("-0.40")
    assert won.mean_pnl_per_quote > 0 > lost.mean_pnl_per_quote
    nothing = pool_settlement_holding(sessions, {})[0]
    assert nothing.quotes_scored == 0 and nothing.quotes_unsettled == won.quotes_scored
    assert nothing.pnl_per_filled_contract is None


def test_one_date_gives_no_date_level_interval():
    sessions = [_session(i, "2026-09-19", base=T0 + timedelta(hours=i), fill=True) for i in range(1, 13)]
    result = pool_settlement_holding(sessions, {"M": Decimal("1")})[0]
    assert result.date_ci95 is None and result.date_ci_family is None
    assert not result.verdict_positive


def test_the_second_metric_needs_the_wider_family_wise_bound():
    assert SETTLEMENT_FAMILY_SIZE == 8
    sessions = [
        _session(i, f"2026-09-{i:02d}", base=T0 + timedelta(days=i), fill=i % 3 != 0)
        for i in range(1, 13)
    ]
    result = pool_settlement_holding(sessions, {"M": Decimal("1")})[0]
    assert result.dates == 12
    assert result.date_ci95 is not None and result.date_ci_family is not None
    assert result.date_ci_family[0] <= result.date_ci95[0]
    assert result.date_ci_family[1] >= result.date_ci95[1]
    assert result.as_dict()["family_wise_confidence"] == "99.375%"
