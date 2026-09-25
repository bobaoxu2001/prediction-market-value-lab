"""The incentive rule, as amended (pre-registration 1 and 5, Amendment 1).

On each side, bids qualify level by level from the best bid, whole levels at a time,
until cumulative size reaches the Target Size; a side that never reaches it has no
qualifying bids. The reference price is the level at which the qualifying total first
reaches a fifth of the target; bids at or above it earn full credit, deeper ones the
Discount Factor per cent below it. A user's share of a side is their credit over all
credit on it. A snapshot counts only with both sides at target.

Two envelopes, never mixed:

  LOW   resolves every open point against us - the only envelope that may support a
        positive verdict. Our credited size is what fits under the target *behind*
        everyone ahead of us in price-time; others' depth is the counterfactual book
        (what our fills absorbed is added back); others' credit is the larger of two
        constructions; the target test must pass in both the recorded and the
        counterfactual book.
  HIGH  resolves them for us - the only envelope that may support a negative verdict.

Pivotality, in both envelopes: the target test reads *others'* orders only. An order
of ours that would be what makes a starved side count earns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

from .model import HIGHEST_CENT

__all__ = [
    "Levels",
    "OwnOrder",
    "accrual",
    "daily_payout",
    "multiplier",
    "qualifying",
    "share_high",
    "share_low",
    "side_ok",
]

#: a side's resting bids, best first: ((price_cents, size), ...)
Levels = tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class OwnOrder:
    price: int
    #: size credited: LOW passes q_eff, HIGH the full remaining size
    size: float


def side_ok(levels: Levels, target: float) -> bool:
    """The target test: a best bid below 99 and at least T resting on the side."""
    return bool(levels) and levels[0][0] < HIGHEST_CENT and sum(q for _, q in levels) >= target


@lru_cache(maxsize=262_144)
def qualifying(levels: Levels, target: float) -> tuple[Levels, int | None]:
    """(qualifying levels, reference price). Empty when the side never reaches T."""
    out: list[tuple[int, float]] = []
    cum, ref = 0.0, None
    for px, q in levels:
        out.append((px, q))
        cum += q
        if ref is None and cum >= target / 5:
            ref = px
        if cum >= target:
            return tuple(out), ref
    return (), None


def multiplier(price: int, ref: int, discount: float) -> float:
    return discount ** max(0, ref - price)


def _merge(others: Levels, own: Sequence[OwnOrder]) -> tuple[Levels, dict[int, float]]:
    book: dict[int, float] = {}
    for px, q in others:
        book[px] = book.get(px, 0.0) + q
    mine: dict[int, float] = {}
    for o in own:
        if o.size > 0:
            book[o.price] = book.get(o.price, 0.0) + o.size
            mine[o.price] = mine.get(o.price, 0.0) + o.size
    return tuple(sorted(book.items(), key=lambda x: -x[0])), mine


def _others_score(others: Levels, target: float, discount: float) -> float:
    lv, ref = qualifying(others, target)
    return sum(q * multiplier(px, ref, discount) for px, q in lv) if lv else 0.0


@lru_cache(maxsize=262_144)
def share_high(others: Levels, own: tuple[OwnOrder, ...], target: float, discount: float) -> float:
    """HIGH: our full size at our price in the recorded book, whole-level reading."""
    merged, mine = _merge(others, own)
    lv, ref = qualifying(merged, target)
    if not lv:
        return 0.0
    prices = {px for px, _ in lv}
    ours = sum(q * multiplier(px, ref, discount) for px, q in mine.items() if px in prices)
    total = sum(q * multiplier(px, ref, discount) for px, q in lv)
    return ours / total if total > 0 and ours > 0 else 0.0


@lru_cache(maxsize=262_144)
def share_low(others_cf: Levels, own: tuple[OwnOrder, ...], target: float, discount: float) -> float:
    """LOW: M_min / (M_min + O_max) on the counterfactual book.

    M_min: our credited sizes (already truncated to what fits under T behind everyone
    ahead of us) at the smaller of the multipliers the two books give them - with our
    orders in the book, and without.
    O_max: the larger of others' credit (i) on their own and (ii) with ours inserted,
    where any of their depth our insertion pushes past the target rejoins at full
    credit instead of dropping out.
    """
    own = tuple(o for o in own if o.size > 0)
    if not own:
        return 0.0
    merged, mine = _merge(others_cf, own)
    lv_m, ref_m = qualifying(merged, target)
    lv_o, ref_o = qualifying(others_cf, target)
    refs = [r for r in (ref_m, ref_o) if r is not None]
    if not refs:
        return 0.0
    m_min = sum(q * min(multiplier(px, r, discount) for r in refs) for px, q in mine.items())
    o_i = _others_score(others_cf, target, discount)
    # (ii): others' credit inside the merged qualifying set, plus displaced depth at m = 1
    if lv_m:
        prices = {px for px, _ in lv_m}
        others_in = sum(q * multiplier(px, ref_m, discount) for px, q in others_cf if px in prices)
        qual_o = {px for px, _ in lv_o}
        displaced = sum(q for px, q in others_cf if px in qual_o and px not in prices)
        o_ii = others_in + displaced
    else:
        o_ii = o_i
    o_max = max(o_i, o_ii)
    return m_min / (m_min + o_max) if m_min > 0 else 0.0


def accrual(reward: float, v: float, n_total: int) -> float:
    """One second's share of the period pool: R * v / (2 * N_total)."""
    return reward * v / (2 * n_total) if n_total > 0 else 0.0


def daily_payout(accrued: float, cap_left: float | None) -> float:
    """Per (market, program period, study day): cap, then $1 minimum, floored to the cent."""
    g = accrued if cap_left is None else min(accrued, max(0.0, cap_left))
    if g < 1.0:
        return 0.0
    return int(g * 100 + 1e-9) / 100
