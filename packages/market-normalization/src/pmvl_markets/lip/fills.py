"""How a resting paper order would have filled, under three bracketing scenarios (4).

In this strategy resting time is paid and fills probably lose, so *under*-counting
fills is the error that flatters. Three scenarios bracket the truth:

  THROUGH  only prints at a worse price than ours fill us (never the queue)
  FIFO     at-level prints consume the queue ahead first; unexplained shrinkage of
           our level is assumed to have been behind us
  HEAVY    unexplained shrinkage is assumed to have been ahead of us; a book whose
           opposite side crosses our price fills us; a best bid that moves away and
           leaves us alone at the top is assumed picked off; shrinkage inside a tape
           gap is read as prints

On the same inputs THROUGH <= FIFO <= HEAVY, by construction and by test. A print
merely *touching* our price is never a fill.

Prices here are cents on the order's own side. A NO bid at n is a YES ask at 100 - n,
so it is filled by prints whose aggressor *bought* YES at a YES price >= 100 - n.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .model import LipSide

__all__ = ["Fill", "FillScenario", "OrderState", "print_relation"]


class FillScenario(StrEnum):
    THROUGH = "THROUGH"
    FIFO = "FIFO"
    HEAVY = "HEAVY"


@dataclass(frozen=True)
class Fill:
    order_id: int
    side: LipSide
    at: float
    price: int
    qty: float
    #: at_level | through | shrink | gap | cross | orphan
    kind: str


@dataclass
class OrderState:
    """One paper order. ``queue`` is the size resting ahead of it at its price."""

    oid: int
    side: LipSide
    price: int
    size: float
    remaining: float
    queue: float
    decided_at: float
    live_from: float
    #: fillable until the cancel lands; credit may stop earlier (at the decision) in LOW
    fillable_until: float = float("inf")
    credit_end_low: float = float("inf")
    credit_end_high: float = float("inf")
    parent: int | None = None
    improved: bool = False
    fills: list[Fill] = field(default_factory=list)

    def live(self, t: float) -> bool:
        return self.live_from <= t < self.fillable_until and self.remaining > 0

    def take(self, qty: float, at: float, kind: str) -> float:
        qty = min(qty, self.remaining)
        if qty <= 0:
            return 0.0
        self.remaining -= qty
        self.fills.append(Fill(self.oid, self.side, at, self.price, qty, kind))
        return qty


def print_relation(side: LipSide, price: int, yes_px: int, taker_yes: bool) -> str | None:
    """'at' | 'through' | None: whether a print could fill a resting bid at price."""
    if side is LipSide.YES:
        if taker_yes:  # aggressor bought YES: it lifted asks, not our YES bid
            return None
        if yes_px == price:
            return "at"
        return "through" if yes_px < price else None
    equivalent_ask = 100 - price  # our NO bid, in YES coordinates
    if not taker_yes:
        return None
    if yes_px == equivalent_ask:
        return "at"
    return "through" if yes_px > equivalent_ask else None


def apply_print(order: OrderState, scenario: FillScenario, rel: str, size: float, at: float) -> float:
    """One relevant print against one live order. Returns the quantity filled."""
    if rel == "through":
        filled = order.take(size, at, "through")
        if scenario is not FillScenario.THROUGH:
            order.queue = 0.0
        return filled
    if scenario is FillScenario.THROUGH:
        return 0.0
    ahead = min(order.queue, size)
    order.queue -= ahead
    return order.take(size - ahead, at, "at_level")


def apply_shrink(order: OrderState, scenario: FillScenario, level_now: float, unexplained: float,
                 covered: bool, at: float) -> float:
    """Others' size at our price fell by more than the at-level prints explain.

    FIFO: the cancels were behind us - the queue ahead can only fall to what is left.
    HEAVY: they were ahead of us; inside a tape gap the excess is read as prints.
    """
    if scenario is FillScenario.THROUGH or unexplained <= 0:
        return 0.0
    if scenario is FillScenario.FIFO:
        order.queue = min(order.queue, level_now)
        return 0.0
    if covered:
        order.queue = max(0.0, order.queue - unexplained)
        return 0.0
    ahead = min(order.queue, unexplained)
    order.queue -= ahead
    return order.take(unexplained - ahead, at, "gap")


def apply_cross(order: OrderState, scenario: FillScenario, crossing_size: float, at: float) -> float:
    """HEAVY only: others' opposite side at or through our price fills us."""
    if scenario is not FillScenario.HEAVY or crossing_size <= 0:
        return 0.0
    return order.take(crossing_size, at, "cross")


def apply_orphan(order: OrderState, scenario: FillScenario, others_best: int | None, at: float) -> float:
    """HEAVY only: others' best bid left below us (beyond our improve offset) - picked off."""
    if scenario is not FillScenario.HEAVY:
        return 0.0
    offset = 1 if order.improved else 0
    if others_best is None or others_best < order.price - offset:
        return order.take(order.remaining, at, "orphan")
    return 0.0
