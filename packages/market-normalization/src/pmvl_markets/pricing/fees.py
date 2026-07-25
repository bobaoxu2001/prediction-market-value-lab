"""Venue fee models.

Both venues charge a *quadratic* taker fee that peaks at 50c and decays toward the
extremes, but they differ in rounding, which matters at small size.

Kalshi
    ``fee = ceil_to_cent(rate * multiplier * C * P * (1 - P))``

    Rounding is **up to the whole cent, on the whole order**. Published schedule:
    "$0.07 - $1.75" per 100 contracts at multiplier 1, which pins ``rate = 0.07``
    (0.07 x 100 x 0.25 = 1.75 at P=0.50, and 0.07 x 100 x 0.01 x 0.99 = $0.0693
    -> $0.07 at P=0.01). Maker-fee series use 0.0175 ("$0.02 - $0.44" per 100).

    The ceiling makes the *per-contract* fee size-dependent: one contract at 50c
    costs a full cent of fee (0.0175 -> 0.01... rounded up), i.e. 1.75c/contract,
    while 100 contracts cost 1.75c/contract exactly. Small orders are
    disproportionately expensive and the model must reflect that.

Polymarket
    ``fee = C * rate * P * (1 - P)``, rounded to 5 decimal places, minimum 0.00001
    USDC. Makers are never charged. The rate is per-market and read from the API.

Sources: https://kalshi.com/fee-schedule, https://docs.kalshi.com/getting_started/fee_rounding,
         https://docs.polymarket.com/trading/fees
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pmvl_shared.enums import Platform
from pmvl_shared.money import ONE, POLY_FEE_PRECISION, ZERO, ceil_cent, safe_div

#: Kalshi published rates (multiplier 1). Per-series multipliers come from the API.
KALSHI_TAKER_RATE = Decimal("0.07")
KALSHI_MAKER_RATE = Decimal("0.0175")


def kalshi_taker_fee(
    contracts: Decimal, price: Decimal, *, rate: Decimal = KALSHI_TAKER_RATE
) -> Decimal:
    """Total taker fee for an order, ceiled to the whole cent.

    ``rate`` should already include the series ``fee_multiplier``.
    """
    if contracts <= 0 or price <= 0 or price >= ONE or rate <= 0:
        return ZERO
    raw = rate * contracts * price * (ONE - price)
    return ceil_cent(raw)


def kalshi_maker_fee(
    contracts: Decimal, price: Decimal, *, rate: Decimal = KALSHI_MAKER_RATE
) -> Decimal:
    """Maker fee, charged only on series with ``fee_type='quadratic_with_maker_fees'``."""
    if contracts <= 0 or price <= 0 or price >= ONE or rate <= 0:
        return ZERO
    return ceil_cent(rate * contracts * price * (ONE - price))


def polymarket_taker_fee(
    contracts: Decimal, price: Decimal, *, rate: Decimal
) -> Decimal:
    """Total taker fee in USDC, rounded to 5dp with a 0.00001 floor.

    A computed fee below the minimum rounds to zero rather than up to the floor:
    the docs state "anything smaller rounds to zero, so very small trades near the
    extremes may incur no fee at all."
    """
    if contracts <= 0 or price <= 0 or price >= ONE or rate <= 0:
        return ZERO
    raw = contracts * rate * price * (ONE - price)
    rounded = raw.quantize(POLY_FEE_PRECISION, rounding=ROUND_HALF_UP)
    return rounded if rounded >= POLY_FEE_PRECISION else ZERO


def taker_fee(
    platform: Platform,
    contracts: Decimal,
    price: Decimal,
    *,
    rate: Decimal,
    fee_type: str = "",
) -> Decimal:
    """Total taker fee for an order on either venue."""
    if platform == Platform.KALSHI:
        if fee_type == "flat":
            # Flat-fee series charge a fixed amount per contract; `rate` carries it.
            return ceil_cent(rate * contracts)
        return kalshi_taker_fee(contracts, price, rate=rate)
    if platform == Platform.POLYMARKET:
        return polymarket_taker_fee(contracts, price, rate=rate)
    return ZERO


def fee_per_contract(
    platform: Platform,
    contracts: Decimal,
    price: Decimal,
    *,
    rate: Decimal,
    fee_type: str = "",
) -> Decimal:
    """Amortised fee per contract at a given order size.

    Explicitly size-dependent. Kalshi's cent-ceiling means a 1-contract order can pay
    several times the per-contract fee of a 100-contract order, and quoting the
    large-order rate on a small recommendation would understate true cost.
    """
    if contracts <= 0:
        return ZERO
    total = taker_fee(platform, contracts, price, rate=rate, fee_type=fee_type)
    return safe_div(total, contracts)


def fee_rounding_cost(
    platform: Platform,
    contracts: Decimal,
    price: Decimal,
    *,
    rate: Decimal,
    fee_type: str = "",
) -> Decimal:
    """The portion of the fee that exists purely because of rounding, per contract.

    Reported separately so a recommendation card can show *why* a small order is
    uneconomic, rather than burying it inside a single fee number.
    """
    if contracts <= 0:
        return ZERO
    if platform == Platform.KALSHI:
        if fee_type == "flat":
            return ZERO
        exact = rate * contracts * price * (ONE - price)
        charged = kalshi_taker_fee(contracts, price, rate=rate)
        return safe_div(charged - exact, contracts)
    if platform == Platform.POLYMARKET:
        exact = contracts * rate * price * (ONE - price)
        charged = polymarket_taker_fee(contracts, price, rate=rate)
        return safe_div(charged - exact, contracts)
    return ZERO


def settlement_fee(platform: Platform, contracts: Decimal) -> Decimal:
    """Fee charged when a winning position pays out.

    Neither venue currently charges one: Kalshi's schedule covers trading only, and
    Polymarket takes no settlement cut. Kept as an explicit zero so that if either
    introduces one, there is a single place to add it rather than a hidden
    assumption spread across the EV code.
    """
    return ZERO
