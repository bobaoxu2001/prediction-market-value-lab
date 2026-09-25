"""The price grid the study admits: linear whole cents, 0.01 to 0.99.

Admitting only this grid removes a whole class of ambiguity from the rule - "N is the
number of ticks" means cents, and nothing has to guess what a tick is on a tapered
market (pre-registration 2.2).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

__all__ = ["CENT", "HIGHEST", "LOWEST", "is_linear_cent", "to_cents"]

CENT = Decimal("0.01")
LOWEST = 1
HIGHEST = 99


def is_linear_cent(meta: dict[str, Any]) -> bool:
    """Every ``price_ranges`` step is a cent and together they cover 0.01..0.99.

    A market with no ranges qualifies only if it declares ``linear_cent``. Anything
    else - a deci-cent tail, a coarser step, a gap - is out.
    """
    ranges = meta.get("price_ranges") or []
    if not ranges:
        return meta.get("price_level_structure") == "linear_cent"
    try:
        steps = [Decimal(str(r["step"])) for r in ranges]
        starts = [Decimal(str(r["start"])) for r in ranges]
        ends = [Decimal(str(r["end"])) for r in ranges]
    except (KeyError, TypeError, ArithmeticError, ValueError):
        return False
    if any(s != CENT for s in steps):
        return False
    return min(starts) <= CENT and max(ends) >= Decimal("0.99")


def to_cents(price: Decimal) -> int:
    """Whole cents. Raises on a price that is not on the cent grid."""
    cents = price * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"{price} is not on the cent grid")
    return int(cents)
