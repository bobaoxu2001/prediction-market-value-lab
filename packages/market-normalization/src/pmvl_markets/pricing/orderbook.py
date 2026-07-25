"""Order book walking: VWAP, depth, and the executable-price rule.

The central rule of this project: **an entry price is the price you can actually
lift, at the size you actually want.** Never the last trade, never the midpoint.

* Buying YES consumes the YES ask ladder.
* Buying NO consumes the NO ask ladder.
* Filling more than the top level costs strictly more than the top-of-book price.
"""

from __future__ import annotations

from decimal import Decimal

from pmvl_shared.enums import Side
from pmvl_shared.money import ONE, ZERO, quantize_price, safe_div
from pmvl_shared.schemas import BookLevel, ExecutionQuote, OrderBook


def walk_book(levels: list[BookLevel], target_size: Decimal) -> ExecutionQuote | None:
    """Consume ask levels cheapest-first up to ``target_size``.

    Returns a partial fill when depth runs out - ``fully_filled`` is False and
    ``filled_size`` reports what was actually achievable. Callers must never treat a
    partial fill as if the requested size were available.
    """
    if target_size <= 0 or not levels:
        return None

    remaining = target_size
    notional = ZERO
    filled = ZERO
    worst = ZERO
    consumed = 0

    for level in levels:
        if remaining <= 0:
            break
        take = min(remaining, level.size)
        if take <= 0:
            continue
        notional += take * level.price
        filled += take
        remaining -= take
        worst = level.price
        consumed += 1

    if filled <= 0:
        return None

    return ExecutionQuote(
        side=Side.YES,  # overwritten by the caller that knows the side
        requested_size=target_size,
        filled_size=filled,
        average_price=quantize_price(safe_div(notional, filled)),
        worst_price=worst,
        notional=notional,
        levels_consumed=consumed,
        fully_filled=remaining <= 0,
    )


def executable_quote(
    book: OrderBook, side: Side, size: Decimal
) -> ExecutionQuote | None:
    """VWAP for buying ``size`` contracts of ``side`` right now."""
    quote = walk_book(book.asks(side), size)
    if quote is None:
        return None
    return quote.model_copy(update={"side": side})


def best_executable_price(book: OrderBook, side: Side) -> Decimal | None:
    """Top-of-book ask - the cheapest price at which *any* size can be bought."""
    return book.best_ask(side)


def available_size(book: OrderBook, side: Side, *, max_price: Decimal | None = None) -> Decimal:
    """Total contracts buyable, optionally capped at a worst acceptable price."""
    total = ZERO
    for level in book.asks(side):
        if max_price is not None and level.price > max_price:
            break
        total += level.size
    return total


def depth_usd(book: OrderBook, side: Side, *, max_price: Decimal | None = None) -> Decimal:
    """Dollar notional resting on the ask side."""
    total = ZERO
    for level in book.asks(side):
        if max_price is not None and level.price > max_price:
            break
        total += level.price * level.size
    return total


def effective_spread(book: OrderBook, side: Side) -> Decimal | None:
    """Ask minus bid on one side, in dollars.

    A wide spread means the exit price is far below the entry price, so a position
    entered here cannot be unwound cheaply if the thesis changes.
    """
    ask = book.best_ask(side)
    bid = book.best_bid(side)
    if ask is None or bid is None:
        return None
    return ask - bid


def implied_probability(book: OrderBook, side: Side) -> Decimal | None:
    """Market-implied probability from the *mid*, for reference display only.

    This is what the market thinks, not what a trade would cost. Never use it as an
    entry price - that is what :func:`executable_quote` is for.
    """
    ask = book.best_ask(side)
    bid = book.best_bid(side)
    if ask is not None and bid is not None:
        return quantize_price((ask + bid) / Decimal("2"))
    return ask if ask is not None else bid


def book_imbalance(book: OrderBook, side: Side) -> Decimal | None:
    """(bid notional - ask notional) / total, in [-1, 1].

    A strongly positive value means buyers are stacked behind the current price - a
    weak signal that the quote is more likely to move up than down.
    """
    bid_notional = sum((l.price * l.size for l in book.bids(side)), ZERO)
    ask_notional = sum((l.price * l.size for l in book.asks(side)), ZERO)
    total = bid_notional + ask_notional
    if total <= 0:
        return None
    return safe_div(bid_notional - ask_notional, total)


def complete_set_cost(book: OrderBook) -> Decimal | None:
    """Cost of buying one YES and one NO at top of book.

    Below $1.00 (after costs) this is a risk-free pair, since exactly one side pays
    out $1. Returns ``None`` when either side has no offer, because a set that cannot
    be completed is not an opportunity.
    """
    yes_ask = book.best_ask(Side.YES)
    no_ask = book.best_ask(Side.NO)
    if yes_ask is None or no_ask is None:
        return None
    return yes_ask + no_ask


def max_complete_sets(book: OrderBook, *, max_cost: Decimal = ONE) -> tuple[Decimal, Decimal]:
    """Walk both ask ladders in parallel while a completed set stays under ``max_cost``.

    Returns ``(sets, total_cost)``. Each step takes the smaller of the two available
    level sizes so the two legs stay matched - an unmatched leg is a naked position,
    not an arbitrage.
    """
    yes_levels = list(book.yes_asks)
    no_levels = list(book.no_asks)
    if not yes_levels or not no_levels:
        return ZERO, ZERO

    i = j = 0
    yes_remaining = yes_levels[0].size
    no_remaining = no_levels[0].size
    sets = ZERO
    cost = ZERO

    while i < len(yes_levels) and j < len(no_levels):
        pair_price = yes_levels[i].price + no_levels[j].price
        if pair_price >= max_cost:
            break
        take = min(yes_remaining, no_remaining)
        if take <= 0:
            break
        sets += take
        cost += take * pair_price
        yes_remaining -= take
        no_remaining -= take
        if yes_remaining <= 0:
            i += 1
            if i < len(yes_levels):
                yes_remaining = yes_levels[i].size
        if no_remaining <= 0:
            j += 1
            if j < len(no_levels):
                no_remaining = no_levels[j].size

    return sets, cost


def slippage_estimate(
    book: OrderBook, side: Side, size: Decimal, tick_size: Decimal, extra_ticks: int
) -> Decimal:
    """Per-contract adverse move assumed between quoting and filling.

    Two components:

    1. **Realised book impact** - the VWAP at ``size`` minus the top-of-book price.
       This is measured, not assumed.
    2. **Latency padding** - ``extra_ticks`` of tick size, covering the gap between
       snapshotting the book and an order arriving.
    """
    padding = tick_size * Decimal(extra_ticks)
    top = book.best_ask(side)
    quote = executable_quote(book, side, size)
    if top is None or quote is None:
        return padding
    impact = max(ZERO, quote.average_price - top)
    return impact + padding
