"""The four declared quoting policies and where each would rest its orders (section 3).

Every policy quotes both sides and prices off *other traders'* recorded book - which
never contains our paper orders, so a policy can never price off itself. Nothing here
reacts to trades, to the opposite side approaching, or to depth: the target moves only
when others' best bid or spread moves (3.6 bans the rest).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .model import HIGHEST_CENT, BookSnapshot, LipSide
from .sim_params import SIM, SimParams

__all__ = ["LipPolicy", "Placement", "POLICIES", "allowed_sizes", "gate_passes", "targets"]


class Placement(StrEnum):
    JOIN = "join"
    IMPROVE = "improve"


@dataclass(frozen=True)
class LipPolicy:
    name: str
    placement: Placement
    #: size per side as a fraction of the program's target size T
    size_frac: Decimal

    def size(self, target: float) -> float:
        return float(Decimal(str(target)) * self.size_frac)


POLICIES = (
    LipPolicy("J1", Placement.JOIN, Decimal(1)),
    LipPolicy("I1", Placement.IMPROVE, Decimal(1)),
    LipPolicy("J5", Placement.JOIN, Decimal("0.2")),
    LipPolicy("I5", Placement.IMPROVE, Decimal("0.2")),
)


def _yes_spread(book: BookSnapshot) -> int | None:
    """Others' YES ask minus YES bid, in cents. The YES ask is 100 - best NO bid."""
    if book.yes.empty or book.no.empty:
        return None
    return (100 - book.no.best) - book.yes.best


def targets(book: BookSnapshot, policy: LipPolicy, resting: dict[LipSide, int | None],
            params: SimParams = SIM) -> dict[LipSide, int | None]:
    """Target price per side, or None where that side is not quotable (3.2, 3.3)."""
    spread = _yes_spread(book)
    out: dict[LipSide, int | None] = {}
    for side in (LipSide.YES, LipSide.NO):
        bids = book.side(side)
        if bids.empty or bids.best >= HIGHEST_CENT:
            out[side] = None
            continue
        best = bids.best
        target = best
        if policy.placement is Placement.IMPROVE and spread is not None and spread >= params.improve_min_spread:
            target = best + 1
        # hold: others joined the price we improved to - stay rather than chase
        if policy.placement is Placement.IMPROVE and resting.get(side) is not None and resting[side] == best:
            target = best
        # never cross others' ask on this side: our bid must stay below it
        other = book.side(side.other)
        ask = 100 - other.best if not other.empty else None
        if ask is not None and target >= ask:
            target = best if best < ask else None
        out[side] = target
    y, n = out[LipSide.YES], out[LipSide.NO]
    # never self-cross: our YES bid must stay below the YES ask our NO bid implies
    if y is not None and n is not None and y + n > HIGHEST_CENT:
        for side in (LipSide.YES, LipSide.NO):
            out[side] = book.side(side).best
        if out[LipSide.YES] + out[LipSide.NO] > HIGHEST_CENT:
            out = {LipSide.YES: None, LipSide.NO: None}
    return out


def allowed_sizes(inventory: float, size: float) -> dict[LipSide, float]:
    """|N| <= S: YES held - NO held nets, so the heavy side quotes less (3.5)."""
    return {LipSide.YES: min(max(size - inventory, 0.0), size),
            LipSide.NO: min(max(size + inventory, 0.0), size)}


def gate_passes(book: BookSnapshot, target: float) -> bool:
    """One gate book: both sides of others' book at T or more, best bids below 99."""
    for side in (LipSide.YES, LipSide.NO):
        bids = book.side(side)
        if bids.empty or bids.best >= HIGHEST_CENT or bids.total() < target:
            return False
    return True
