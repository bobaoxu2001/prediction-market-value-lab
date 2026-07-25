"""All-in executable cost, and expected value computed against it.

The value calculation is deliberately pessimistic at every step:

* Entry uses the **VWAP of the ask ladder** at the requested size, not top of book.
* Fees are the venue's real formula at that size, including rounding effects.
* Slippage combines measured book impact with a latency pad.
* Capital cost charges for the time the position is locked up until resolution.
* Ranking uses the **lower bound** of the fair-probability interval, so a wide,
  uncertain estimate cannot manufacture an edge.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import (
    CostBreakdown,
    ExecutionQuote,
    NormalizedMarket,
    OrderBook,
    SizedQuote,
)
from pmvl_shared.timeutil import years_until

from .fees import fee_per_contract, fee_rounding_cost
from .orderbook import executable_quote, slippage_estimate

#: Sizes every candidate is priced at, per the spec.
STANDARD_SIZES = (Decimal("10"), Decimal("50"), Decimal("100"))


def capital_cost_per_contract(
    price: Decimal, resolution_time: datetime | None, *, annual_rate: Decimal | None = None
) -> Decimal:
    """Opportunity cost of capital locked until resolution.

    A 3% edge that takes 30 days to realise is worth materially less than the same
    edge over 24 hours. Charged as simple interest on the entry price.
    """
    settings = get_settings()
    rate = annual_rate if annual_rate is not None else settings.capital_cost_annual_rate
    years = D(str(years_until(resolution_time)))
    if years <= 0 or rate <= 0:
        return ZERO
    return quantize_usd(price * rate * years)


def transfer_cost_per_contract(
    platform: Platform, contracts: Decimal, *, per_leg_cost: Decimal | None = None
) -> Decimal:
    """Amortised cost of getting value onto the venue.

    Kalshi is a US brokerage with free ACH, so this is zero. Polymarket settles in
    USDC on Polygon, so a bridge/gas allowance is amortised over the position - which
    makes small Polymarket positions structurally unattractive, correctly.
    """
    if platform != Platform.POLYMARKET or contracts <= 0:
        return ZERO
    settings = get_settings()
    cost = per_leg_cost if per_leg_cost is not None else settings.polymarket_transfer_cost_usd
    return quantize_usd(safe_div(cost, contracts))


def build_cost_breakdown(
    market: NormalizedMarket,
    book: OrderBook,
    side: Side,
    contracts: Decimal,
    *,
    quote: ExecutionQuote | None = None,
    execution_risk_penalty: Decimal = ZERO,
    include_transfer_cost: bool = True,
) -> CostBreakdown | None:
    """Full per-contract cost of entering ``contracts`` of ``side``.

    Returns ``None`` when the book cannot fill any size - an unfillable market has no
    executable price and therefore no opportunity.
    """
    settings = get_settings()
    quote = quote or executable_quote(book, side, contracts)
    if quote is None or quote.filled_size <= 0:
        return None

    filled = quote.filled_size
    entry = quote.average_price

    fee = fee_per_contract(
        market.platform, filled, entry, rate=market.fee_rate, fee_type=market.fee_type
    )
    rounding = fee_rounding_cost(
        market.platform, filled, entry, rate=market.fee_rate, fee_type=market.fee_type
    )
    # `fee` already contains the rounding component; report rounding separately for
    # display and subtract it here so the total is not double-counted.
    fee_ex_rounding = max(ZERO, fee - rounding)

    slippage = slippage_estimate(
        book, side, filled, market.tick_size, settings.slippage_ticks
    )
    # Book impact is already inside the VWAP; only the latency pad is additional.
    top = book.best_ask(side)
    latency_pad = market.tick_size * Decimal(settings.slippage_ticks)
    slippage_extra = latency_pad if top is not None else slippage

    return CostBreakdown(
        entry_price=quantize_usd(entry),
        platform_fee=quantize_usd(fee_ex_rounding),
        fee_rounding=quantize_usd(rounding),
        estimated_slippage=quantize_usd(slippage_extra),
        transfer_cost=(
            transfer_cost_per_contract(market.platform, filled)
            if include_transfer_cost
            else ZERO
        ),
        capital_cost=capital_cost_per_contract(entry, market.expected_resolution_time),
        execution_risk_penalty=quantize_usd(execution_risk_penalty),
    )


def gross_expected_profit(win_probability: Decimal, entry_price: Decimal) -> Decimal:
    """``P(win) - entry`` per contract.

    A binary contract pays exactly $1 on a win and $0 otherwise, so expected payout
    is ``P`` and the gross edge is ``P - entry``.
    """
    return quantize_usd(win_probability - entry_price)


def net_ev(win_probability: Decimal, total_cost: Decimal) -> Decimal:
    """Expected value per contract net of every cost component."""
    return quantize_usd(win_probability - total_cost)


def net_roi(win_probability: Decimal, total_cost: Decimal) -> Decimal:
    """Return on the capital actually deployed (the all-in cost, not the ask)."""
    if total_cost <= 0:
        return ZERO
    return quantize_usd(safe_div(win_probability - total_cost, total_cost))


def fractional_kelly(
    win_probability: Decimal, total_cost: Decimal, *, fraction: Decimal | None = None
) -> Decimal:
    """Kelly stake as a fraction of bankroll, scaled down.

    For a binary contract bought at cost ``c`` paying $1:
    ``b = (1 - c) / c``, ``f* = (p*b - (1-p)) / b = (p - c) / (1 - c)``.

    Returns 0 for a non-positive edge. Full Kelly is far too aggressive against an
    estimated - not known - probability, so the configured fraction is applied.
    """
    settings = get_settings()
    fraction = fraction if fraction is not None else settings.kelly_fraction
    if total_cost <= 0 or total_cost >= ONE:
        return ZERO
    edge = win_probability - total_cost
    if edge <= 0:
        return ZERO
    full = safe_div(edge, ONE - total_cost)
    return quantize_usd(max(ZERO, full * fraction))


def price_at_sizes(
    market: NormalizedMarket,
    book: OrderBook,
    side: Side,
    win_probability: Decimal,
    *,
    sizes: tuple[Decimal, ...] = STANDARD_SIZES,
    execution_risk_penalty: Decimal = ZERO,
) -> list[SizedQuote]:
    """Economics at each standard size.

    Costs rise with size (deeper into the book, more slippage) while some fixed costs
    amortise down. Showing all three makes the capacity of an edge visible instead of
    letting a top-of-book number imply unlimited size.
    """
    out: list[SizedQuote] = []
    for size in sizes:
        quote = executable_quote(book, side, size)
        if quote is None:
            continue
        cost = build_cost_breakdown(
            market, book, side, size,
            quote=quote, execution_risk_penalty=execution_risk_penalty,
        )
        if cost is None:
            continue
        total = cost.total_cost
        ev = net_ev(win_probability, total)
        out.append(
            SizedQuote(
                size=size,
                filled_size=quote.filled_size,
                average_price=quote.average_price,
                total_cost_per_contract=quantize_usd(total),
                net_ev_per_contract=ev,
                expected_profit=quantize_usd(ev * quote.filled_size),
                fully_filled=quote.fully_filled,
            )
        )
    return out


def max_profitable_size(
    market: NormalizedMarket,
    book: OrderBook,
    side: Side,
    win_probability: Decimal,
    *,
    max_size: Decimal = Decimal("5000"),
    execution_risk_penalty: Decimal = ZERO,
) -> tuple[Decimal, Decimal]:
    """Largest size whose *marginal* contract still has positive EV.

    Binary-searches the ask ladder. Because average cost rises monotonically as depth
    is consumed, the point where average EV turns negative bounds the profitable
    capacity. Returns ``(size, total_expected_profit)``.
    """
    available = sum((l.size for l in book.asks(side)), ZERO)
    if available <= 0:
        return ZERO, ZERO
    hi = min(available, max_size)

    def ev_at(size: Decimal) -> tuple[Decimal, Decimal]:
        quote = executable_quote(book, side, size)
        if quote is None:
            return ZERO, ZERO
        cost = build_cost_breakdown(
            market, book, side, size,
            quote=quote, execution_risk_penalty=execution_risk_penalty,
        )
        if cost is None:
            return ZERO, ZERO
        per_contract = net_ev(win_probability, cost.total_cost)
        return per_contract, quantize_usd(per_contract * quote.filled_size)

    ev_hi, profit_hi = ev_at(hi)
    if ev_hi > 0:
        return hi, profit_hi

    lo = market.min_order_size if market.min_order_size > 0 else Decimal("1")
    ev_lo, profit_lo = ev_at(lo)
    if ev_lo <= 0:
        return ZERO, ZERO

    best_size, best_profit = lo, profit_lo
    for _ in range(24):  # ~1e-7 relative precision on the bracket
        mid = (lo + hi) / Decimal("2")
        ev_mid, profit_mid = ev_at(mid)
        if ev_mid > 0:
            lo = mid
            if profit_mid > best_profit:
                best_size, best_profit = mid, profit_mid
        else:
            hi = mid
    return best_size.quantize(Decimal("0.01")), best_profit


def expected_profit_per_100_usd(net_ev_per_contract: Decimal, total_cost: Decimal) -> Decimal:
    """Expected profit from deploying $100 of capital at this cost.

    Normalises across price levels: 100 contracts at 5c and 5 contracts at $1 both
    deploy roughly the same capital, and this makes them directly comparable.
    """
    if total_cost <= 0:
        return ZERO
    contracts = safe_div(D(100), total_cost)
    return quantize_usd(net_ev_per_contract * contracts)
