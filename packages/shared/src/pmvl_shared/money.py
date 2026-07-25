"""Decimal money and price primitives.

Every financial quantity in this project is a ``Decimal``. ``float`` is banned from
core financial calculations because binary contract pricing lives on a $0.001-$0.01
lattice where float representation error changes arbitrage verdicts.

Two distinct quantisations are used:

``CENT`` (0.01)
    The unit balances settle in. Fees are *ceiled* here (the exchange rounds fees up).

``CENTICENT`` (0.0001)
    Kalshi's ``_dollars`` fixed-point wire format and its internal fee precision.
    Polymarket prices live on a $0.001 tick, which is representable here too.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from typing import Union

Numeric = Union[Decimal, int, str]

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")
MICRO = Decimal("0.000001")

#: Polymarket fee precision: "Fees are rounded to 5 decimal places."
POLY_FEE_PRECISION = Decimal("0.00001")


def D(value: Numeric | float | None, default: Decimal | None = None) -> Decimal:
    """Coerce to ``Decimal`` without ever going through binary float.

    ``float`` inputs are stringified first, so ``D(0.07)`` is exactly ``0.07`` and not
    ``0.070000000000000006...``. Providers hand us JSON floats (Polymarket) and
    fixed-point strings (Kalshi); both must land on the same lattice.
    """
    if value is None:
        if default is not None:
            return default
        raise ValueError("cannot coerce None to Decimal without a default")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def d_or_none(value: Numeric | float | None) -> Decimal | None:
    """``D`` that propagates ``None`` instead of raising."""
    if value is None or value == "":
        return None
    try:
        return D(value)
    except Exception:  # noqa: BLE001 - provider payloads are untrusted
        return None


def ceil_cent(value: Decimal) -> Decimal:
    """Round up to the next whole cent. Used for exchange fees."""
    return value.quantize(CENT, rounding=ROUND_CEILING)


def floor_cent(value: Decimal) -> Decimal:
    """Round down to a whole cent. Used for balance credits."""
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def round_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def quantize_price(value: Decimal) -> Decimal:
    """Snap a contract price to the centicent lattice both venues can express."""
    return value.quantize(CENTICENT, rounding=ROUND_HALF_UP)


def quantize_usd(value: Decimal) -> Decimal:
    """Money amounts carried through EV maths keep centicent resolution.

    We deliberately do *not* round to cents mid-calculation: a $0.004 per-contract
    edge is meaningful at 500 contracts, and rounding it away mid-pipeline would
    silently destroy small-but-real edges.
    """
    return value.quantize(CENTICENT, rounding=ROUND_HALF_UP)


def quantize_prob(value: Decimal) -> Decimal:
    return value.quantize(MICRO, rounding=ROUND_HALF_UP)


def clamp(value: Decimal, low: Decimal = ZERO, high: Decimal = ONE) -> Decimal:
    return max(low, min(high, value))


def clamp_prob(value: Decimal) -> Decimal:
    """Clamp into the open-ish unit interval used for log-loss safety."""
    return clamp(value, Decimal("0.000001"), Decimal("0.999999"))


def safe_div(numerator: Decimal, denominator: Decimal, default: Decimal = ZERO) -> Decimal:
    if denominator == 0:
        return default
    with localcontext() as ctx:
        ctx.prec = 28
        return numerator / denominator


def pct(value: Decimal) -> Decimal:
    """Decimal fraction -> percentage, 2dp, for display only."""
    return (value * Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)


def snap_to_tick(price: Decimal, tick: Decimal, *, direction: str = "up") -> Decimal:
    """Snap a price to a venue tick.

    Asks snap *up* and bids snap *down* so a snapped quote is never more optimistic
    than a real one that could actually rest on the book.
    """
    if tick <= 0:
        return price
    ratio = price / tick
    if direction == "up":
        steps = ratio.quantize(ONE, rounding=ROUND_CEILING)
    elif direction == "down":
        steps = ratio.quantize(ONE, rounding=ROUND_FLOOR)
    else:
        steps = ratio.quantize(ONE, rounding=ROUND_HALF_UP)
    return quantize_price(steps * tick)
