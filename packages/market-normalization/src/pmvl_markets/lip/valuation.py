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
        return _fallback(md, side, md.close_time, low)
    for b in md.books.received_between(t, t + params.mark_search_s):
        m = side_mid(b, side)
        if m is not None:
            return m
    return _fallback(md, side, t + params.mark_search_s, low)


def _fallback(md: MarketDay, side: LipSide, at: float, low: bool) -> float:
    """No two-sided mid where one was needed. LOW: the held side's best bid (0 if none) -
    the lowest a holder could get. HIGH: the higher of the last two-sided mid and that
    same bid, so HIGH's mark is never below LOW's (Amendment 3, F2)."""
    book = md.books.before(at)
    bid = book.side(side).best if book is not None else None
    held = float(bid) if bid is not None else 0.0
    if low:
        return held
    m = _last_mid(md, side, at)
    return held if m is None else max(m, held)


def mark_1h(path: SimPath, md: MarketDay, *, low: bool, params: SimParams = SIM) -> float:
    """$: fills marked an hour later, minus maker fees, minus the flatten's cost vs the mid."""
    value = 0.0
    marked = {LipSide.YES: [0.0, 0.0], LipSide.NO: [0.0, 0.0]}  # contracts, sum of marks
    for f in path.fills:
        m = _mark(md, f.side, f.at + params.mark_horizon_s, low=low, params=params)
        value += f.qty * (m - f.price) / 100
        marked[f.side][0] += f.qty
        marked[f.side][1] += f.qty * m
    value -= fill_fees(path.fills, md, low=low, params=params)
    value += _exit_charge(path, md, marked, low=low, params=params)
    return value


def _observed(md: MarketDay, k: float, params: SimParams) -> bool:
    """Section 5.1: second k is OBSERVED when the last book received by k and the first
    book sent at or after k + 1 both exist and span at most the span limit (2c + 1.5 s).
    The same test the credit computation applies (simulator._BookIndex.bracket)."""
    first, last = md.books.before(k), md.books.after(k + 1)
    return first is not None and last is not None and last.sent_at - first.received_at <= params.span_limit_s


def _last_mid(md: MarketDay, side: LipSide, at: float) -> float | None:
    """The most recent two-sided mid at or before ``at``."""
    for b in reversed(md.books.received_between(-math.inf, at + 1e-9)):
        m = side_mid(b, side)
        if m is not None:
            return m
    return None


def _exit_charge(path: SimPath, md: MarketDay, marked: dict[LipSide, list[float]], *, low: bool,
                 params: SimParams = SIM) -> float:
    """Flattening the residual, charged against what it was worth.

    charge = proceeds - qty x reference - fee (section 6), against mid_held at the exit. That
    mid exists only where the exit's second is OBSERVED (section 5.1) and the reference book
    is two-sided: then it is the reference in both envelopes (Amendment 3.2). Across a gap
    the book before it is not the mid at the exit, so the flatten has no two-sided mid to be
    measured against, and (Amendment 2, item 11) the reference is chosen from candidates - the
    reference book's mid (its held-side bid where it has no mid), the walk book's held-side
    bid and the last two-sided mid - and for a charge a *higher* reference is the pessimistic one: LOW takes the
    highest of them, never less than the sale's own average price, so a charge is never
    a gain; HIGH the lowest. A remainder no bid could take is worth nothing in LOW (the
    value its fills' marks gave it is taken back) and nets to zero in HIGH.
    """
    charge = 0.0
    for at, side, qty, proceeds, fee in path.exits:
        if math.isnan(proceeds):
            if low:
                n, total = marked[side]
                charge -= qty * ((total / n) if n > 0 else 0.0) / 100
            continue
        candidates = []
        ref_book = md.books.before(at)
        mid = side_mid(ref_book, side) if ref_book is not None else None
        if mid is not None and _observed(md, math.floor(at), params):
            charge += proceeds - qty * mid / 100 - fee
            continue
        if mid is not None:  # item 11: the reference book's mid, or its bid where it has none
            candidates.append(mid)
        elif ref_book is not None and ref_book.side(side).best is not None:
            candidates.append(float(ref_book.side(side).best))
        walk_book = next((b for b in md.books.snapshots if b.received_at >= at), None)
        if walk_book is not None and walk_book.side(side).best is not None:
            candidates.append(float(walk_book.side(side).best))
        last = _last_mid(md, side, at)
        if last is not None:
            candidates.append(last)
        sale = proceeds * 100 / qty if qty > 0 else 0.0
        if low:
            ref = max(candidates + [sale])
        else:
            ref = min(candidates) if candidates else sale
        charge += proceeds - qty * ref / 100 - fee
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
