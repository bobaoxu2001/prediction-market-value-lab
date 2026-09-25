"""What the fills were worth, two ways, plus the fees (section 6).

MARK_1H estimates adverse selection: each fill is marked at other traders' side mid an
hour later (never at a mid our own orders would have moved), and the day-end flatten is
charged its real cost against the mid. DAY_EXIT is the literal cash of the daily-flat
strategy: every price paid, a dollar for every YES/NO pair that nets, and the proceeds
of selling the residual through others' bids. Both are required for a positive verdict;
SETTLE (fills held to settlement) is descriptive only and never reaches the decision.
"""

from __future__ import annotations

import math
from decimal import Decimal

from pmvl_shared.money import ceil_cent

from .fills import Fill
from .model import BookSide, BookSnapshot, LipSide, MarketDay
from .sim_params import SIM, SimParams
from .simulator import SimPath

__all__ = ["day_exit", "fill_fees", "mark_1h", "side_mid", "taker_fee", "walk_bids"]

TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")


def taker_fee(qty: float, price_cents: int, multiplier: Decimal) -> float:
    p = Decimal(price_cents) / 100
    return float(ceil_cent(TAKER_RATE * multiplier * Decimal(str(qty)) * p * (1 - p)))


def walk_bids(side: BookSide, qty: float, multiplier: Decimal) -> tuple[float, float, float]:
    """Sell qty into others' bids, best first. (proceeds $, filled, taker fee $)."""
    proceeds, filled, fee = 0.0, 0.0, 0.0
    for px, sz in zip(side.px, side.sz):
        take = min(float(sz), qty - filled)
        if take <= 0:
            break
        proceeds += take * int(px) / 100
        fee += taker_fee(take, int(px), multiplier)
        filled += take
    return proceeds, filled, fee


def side_mid(book: BookSnapshot, side: LipSide) -> float | None:
    """Others' mid for one side, in cents: (best bid + best ask) / 2, ask = 100 - other bid."""
    bids, other = book.side(side), book.side(side.other)
    if bids.empty or other.empty:
        return None
    return (bids.best + (100 - other.best)) / 2


def fill_fees(fills: list[Fill], md: MarketDay, *, low: bool, params: SimParams = SIM) -> float:
    """Maker fees on series that charge them; an unknown fee type is charged in LOW only."""
    if md.fee_type == "quadratic_with_maker_fees":
        rate = MAKER_RATE * md.fee_multiplier
    elif md.fee_type in ("quadratic", "flat"):
        return 0.0
    elif low:
        rate = Decimal(str(params.maker_rate_unknown_fee_low))
    else:
        return 0.0
    total = 0.0
    for f in fills:
        p = Decimal(f.price) / 100
        total += float(ceil_cent(rate * Decimal(str(f.qty)) * p * (1 - p)))
    return total


def _payout(md: MarketDay, side: LipSide) -> float | None:
    if md.settlement_yes is None:
        return None
    yes = float(md.settlement_yes) * 100
    return yes if side is LipSide.YES else 100 - yes


def _mark(md: MarketDay, side: LipSide, t: float, *, low: bool, params: SimParams) -> float:
    """Cents: others' side mid in [t, t + search]; closes and gaps resolved per envelope."""
    if t >= md.close_time:
        paid = _payout(md, side)
        if paid is not None:
            return paid
        last = md.books.before(md.close_time)
        return _fallback(last, side, low)
    for b in md.books.received_between(t, t + params.mark_search_s):
        m = side_mid(b, side)
        if m is not None:
            return m
    return _fallback(md.books.before(t + params.mark_search_s), side, low)


def _fallback(book: BookSnapshot | None, side: LipSide, low: bool) -> float:
    if book is None:
        return 0.0
    if low:
        bid = book.side(side).best
        return float(bid) if bid is not None else 0.0
    m = side_mid(book, side)
    if m is not None:
        return m
    bid = book.side(side).best
    return float(bid) if bid is not None else 0.0


def mark_1h(path: SimPath, md: MarketDay, *, low: bool, params: SimParams = SIM) -> float:
    """$: fills marked an hour later, minus maker fees, minus the flatten's cost vs the mid."""
    value = 0.0
    for f in path.fills:
        value += f.qty * (_mark(md, f.side, f.at + params.mark_horizon_s, low=low, params=params) - f.price) / 100
    value -= fill_fees(path.fills, md, low=low, params=params)
    value += _exit_charge(path, md, low=low)
    return value


def _exit_charge(path: SimPath, md: MarketDay, *, low: bool) -> float:
    charge = 0.0
    for at, side, qty, proceeds, fee in path.exits:
        book = md.books.before(at)
        mid = side_mid(book, side) if book is not None else None
        mid_c = mid if mid is not None else 0.0
        if math.isnan(proceeds):
            charge += 0.0 if not low else -qty * mid_c / 100
            continue
        charge += proceeds - qty * mid_c / 100 - fee
    return charge


def day_exit(path: SimPath, md: MarketDay, *, low: bool, params: SimParams = SIM) -> float:
    """$: the literal cash of quoting all day and ending flat."""
    held = {LipSide.YES: 0.0, LipSide.NO: 0.0}
    cash = 0.0
    for f in path.fills:
        cash -= f.qty * f.price / 100
        held[f.side] += f.qty
    cash += min(held[LipSide.YES], held[LipSide.NO])  # each netted pair pays $1
    for at, side, qty, proceeds, fee in path.exits:
        if math.isnan(proceeds):
            if not low:
                book = md.books.before(at)
                mid = side_mid(book, side) if book is not None else None
                cash += qty * (mid or 0.0) / 100
            continue
        cash += proceeds - fee
    cash -= fill_fees(path.fills, md, low=low, params=params)
    return cash
