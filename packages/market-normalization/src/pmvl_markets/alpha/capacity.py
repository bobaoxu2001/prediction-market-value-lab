"""Executable capacity, measured from real stored ladders.

This module answers one question and refuses to answer any other:

    **If an informational edge of alpha cents existed, how much size could be
    put on before the venue took it back?**

It is not an alpha estimate, it cannot become one, and nothing here reads an
outcome, a settlement or a forecast. ``alpha`` is a **hypothetical input** the
caller supplies; every output is conditional on it. A capacity result that looks
good means the market is big enough to be worth researching, not that there is
anything to trade.

Why taker-only
--------------
Taker execution is the only execution this repository can *observe*. The ask
ladder was captured; walking it is arithmetic. A maker fill depends on queue
position, which Kalshi's public API does not publish and which therefore cannot
be validated at any sample size. Reporting a maker capacity would mean reporting
a number proportional to an unfalsifiable assumption, so this module does not
compute one.

The two economics, which are different
--------------------------------------
``HOLD_TO_SETTLEMENT`` crosses the spread once. You buy at the ask and the venue
pays $1 or $0; there is no exit trade and Kalshi charges no settlement fee. Net
edge is therefore ``alpha - half_spread - depth_slippage - entry_fee``.

``ROUND_TRIP`` crosses twice. A convergence strategy that exits before
resolution sells into the *bid*, pays a second fee, and eats the spread again.

These are not variations on one number. On a market whose entry costs a penny
and whose exit costs another penny, the round-trip needs roughly twice the alpha
to break even - and the bid side is where prediction-market books are thinnest.
Reporting only the first would overstate a convergence strategy by the width of
the book.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Sequence

from pmvl_shared.enums import Platform
from pmvl_shared.money import ONE, ZERO, D, quantize_price, quantize_usd, safe_div
from pmvl_shared.schemas import BookLevel

from ..pricing.fees import taker_fee
from ..pricing.orderbook import walk_book


class Economics(str, Enum):
    """Which of the two cost structures a result describes."""

    #: Cross once. Buy at the ask, hold, receive the venue's payout.
    HOLD_TO_SETTLEMENT = "hold_to_settlement"
    #: Cross twice. Buy at the ask, sell into the bid before resolution.
    ROUND_TRIP = "round_trip"


#: Sizes the preflight prices. Extended below the requested grid because the
#: measured books turned out to make the small end the interesting part.
DEFAULT_SIZES: tuple[Decimal, ...] = tuple(
    Decimal(n) for n in (10, 25, 50, 100, 250, 500)
)

#: Hypothetical gross alpha levels, in dollars per contract.
DEFAULT_ALPHAS: tuple[Decimal, ...] = tuple(
    Decimal(c) / Decimal(100) for c in (1, 2, 3, 4, 5)
)

#: Search grid for the maximum executable size. Geometric, so it resolves the
#: small end (where the cent-ceiling fee dominates) and still reaches real depth.
_SEARCH_GRID: tuple[Decimal, ...] = tuple(
    Decimal(n)
    for n in (
        1, 2, 5, 10, 15, 20, 25, 35, 50, 75, 100, 150, 200, 250, 350, 500,
        750, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 20000, 50000,
    )
)


@dataclass(frozen=True)
class BookState:
    """One side of one captured book, reduced to what capacity needs.

    Built only from stored ladder rows. There is deliberately no field that
    could carry an outcome, a model probability or anything dated after the
    capture: a capacity number must be computable at the instant the book was
    observed, or it is not capacity.
    """

    asks: tuple[BookLevel, ...]
    bids: tuple[BookLevel, ...]

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def two_sided(self) -> bool:
        return bool(self.asks and self.bids)

    @property
    def mid(self) -> Decimal | None:
        """Reference price for a hypothetical edge.

        ``None`` on a one-sided book rather than a substituted value. A
        one-sided book has no mid, and inventing one (say, by treating a missing
        bid as zero) would silently hand the strategy a spread it never paid.
        """
        if not self.two_sided:
            return None
        return quantize_price((self.best_ask + self.best_bid) / Decimal("2"))

    @property
    def spread(self) -> Decimal | None:
        if not self.two_sided:
            return None
        return self.best_ask - self.best_bid

    @property
    def ask_depth_contracts(self) -> Decimal:
        return sum((level.size for level in self.asks), ZERO)

    @property
    def ask_depth_usd(self) -> Decimal:
        return sum((level.price * level.size for level in self.asks), ZERO)

    @property
    def bid_depth_contracts(self) -> Decimal:
        return sum((level.size for level in self.bids), ZERO)


@dataclass(frozen=True)
class CapacityPoint:
    """Economics of one hypothetical order, at one size, for one alpha.

    Every cost is reported separately. A single "net edge" would hide which of
    spread, depth or fee is the binding constraint, and those have different
    remedies - a wide spread argues for resting, deep slippage argues for
    smaller clips, and a cent-ceiling fee argues for larger ones.
    """

    size_requested: Decimal
    size_filled: Decimal
    fully_filled: bool
    entry_vwap: Decimal
    reference_mid: Decimal
    #: ``best_ask - mid``: the cost of crossing, before any size.
    half_spread: Decimal
    #: ``entry_vwap - best_ask``: the additional cost of wanting size.
    depth_slippage: Decimal
    #: Venue fee per contract at this size. Size-dependent: Kalshi ceils the fee
    #: to the whole cent on the whole order.
    entry_fee: Decimal
    #: Second crossing plus second fee. Zero for HOLD_TO_SETTLEMENT.
    exit_cost: Decimal
    hypothetical_alpha: Decimal
    economics: Economics

    @property
    def total_cost(self) -> Decimal:
        return quantize_usd(
            self.half_spread + self.depth_slippage + self.entry_fee + self.exit_cost
        )

    @property
    def net_edge(self) -> Decimal:
        """Hypothetical alpha less every observable execution cost, per contract."""
        return quantize_usd(self.hypothetical_alpha - self.total_cost)

    @property
    def capital_deployed(self) -> Decimal:
        return quantize_usd(self.entry_vwap * self.size_filled)

    @property
    def pnl(self) -> Decimal:
        return quantize_usd(self.net_edge * self.size_filled)

    @property
    def return_on_deployed(self) -> Decimal:
        return quantize_usd(safe_div(self.pnl, self.capital_deployed))

    def as_dict(self) -> dict[str, Any]:
        return {
            "size_requested": str(self.size_requested),
            "size_filled": str(self.size_filled),
            "fully_filled": self.fully_filled,
            "entry_vwap": str(self.entry_vwap),
            "reference_mid": str(self.reference_mid),
            "half_spread": str(quantize_usd(self.half_spread)),
            "depth_slippage": str(quantize_usd(self.depth_slippage)),
            "entry_fee": str(quantize_usd(self.entry_fee)),
            "exit_cost": str(quantize_usd(self.exit_cost)),
            "total_cost": str(self.total_cost),
            "hypothetical_alpha": str(self.hypothetical_alpha),
            "net_edge": str(self.net_edge),
            "capital_deployed": str(self.capital_deployed),
            "pnl": str(self.pnl),
            "economics": self.economics.value,
        }


def price_order(
    book: BookState,
    *,
    size: Decimal,
    alpha: Decimal,
    platform: Platform,
    fee_rate: Decimal,
    fee_type: str = "",
    economics: Economics = Economics.HOLD_TO_SETTLEMENT,
) -> CapacityPoint | None:
    """Price one hypothetical taker order against a real ladder.

    Returns ``None`` when the book cannot support the calculation at all - no
    mid, or no ask depth. A refusal is not a zero: a market with no two-sided
    quote is not a market with zero capacity, it is one where capacity is
    undefined, and pooling those as zeros would understate the tradeable
    universe as surely as dropping them would overstate it.
    """
    mid = book.mid
    if mid is None or not book.asks:
        return None

    quote = walk_book(list(book.asks), size)
    if quote is None or quote.filled_size <= ZERO:
        return None

    filled = quote.filled_size
    entry_vwap = quote.average_price
    best_ask = book.best_ask

    entry_fee_total = taker_fee(
        platform, filled, entry_vwap, rate=fee_rate, fee_type=fee_type
    )
    entry_fee = safe_div(entry_fee_total, filled)

    exit_cost = ZERO
    if economics is Economics.ROUND_TRIP:
        # Exit sells into the BID ladder, and pays a second fee. A convergence
        # exit marked at the ask has not modelled an exit; it has recorded the
        # price somebody wishes they could sell at.
        exit_quote = walk_book(list(book.bids), filled)
        if exit_quote is None or exit_quote.filled_size <= ZERO:
            return None
        exit_vwap = exit_quote.average_price
        exit_fee_total = taker_fee(
            platform, exit_quote.filled_size, exit_vwap, rate=fee_rate, fee_type=fee_type
        )
        exit_fee = safe_div(exit_fee_total, exit_quote.filled_size)
        # Everything between the mid and what the exit actually realises.
        exit_cost = (mid - exit_vwap) + exit_fee

    return CapacityPoint(
        size_requested=size,
        size_filled=filled,
        fully_filled=quote.fully_filled,
        entry_vwap=entry_vwap,
        reference_mid=mid,
        half_spread=best_ask - mid,
        depth_slippage=entry_vwap - best_ask,
        entry_fee=entry_fee,
        exit_cost=exit_cost,
        hypothetical_alpha=alpha,
        economics=economics,
    )


def max_executable_size(
    book: BookState,
    *,
    alpha: Decimal,
    platform: Platform,
    fee_rate: Decimal,
    fee_type: str = "",
    economics: Economics = Economics.HOLD_TO_SETTLEMENT,
    min_net_edge: Decimal = ZERO,
    grid: Sequence[Decimal] = _SEARCH_GRID,
) -> tuple[Decimal, Decimal]:
    """Largest grid size whose net edge is still at least ``min_net_edge``.

    Returns ``(contracts, dollars_deployed)``.

    A grid scan rather than a bisection, and the reason is Kalshi's fee: it is
    ceiled to the whole cent **on the whole order**, so per-contract cost is a
    step function that *falls* as size rises across a step boundary. Net edge is
    therefore not monotonic in size at the small end, and a bisection assuming
    unimodality would return a wrong answer confidently. The scan takes the
    largest qualifying grid point, which is a lower bound on the true maximum -
    the conservative direction.
    """
    best_size, best_dollars = ZERO, ZERO
    for size in grid:
        point = price_order(
            book,
            size=size,
            alpha=alpha,
            platform=platform,
            fee_rate=fee_rate,
            fee_type=fee_type,
            economics=economics,
        )
        if point is None:
            continue
        # A partial fill means the ladder ran out; the size that matters is what
        # actually filled, not what was asked for.
        if point.net_edge >= min_net_edge and point.size_filled > best_size:
            best_size = point.size_filled
            best_dollars = point.capital_deployed
        if not point.fully_filled:
            break
    return best_size, best_dollars


@dataclass
class CapacityObservation:
    """One book, fully priced, with the segmentation keys attached.

    Segment keys are computed from the capture itself - price, spread, depth,
    time to settlement, hour - so a distribution can be cut without a second
    pass over the database and without any join that could introduce a value
    dated after the observation.
    """

    snapshot_id: int
    market_key: str
    series: str
    side: str
    observed_at: Any
    price_bucket: str
    spread_bucket: str
    ttl_bucket: str
    hour_utc: int
    liquidity_bucket: str
    best_ask: Decimal
    spread: Decimal
    ask_depth_contracts: Decimal
    ask_depth_usd: Decimal
    bid_depth_contracts: Decimal
    #: (alpha, size) -> point, for HOLD_TO_SETTLEMENT
    points: dict[tuple[str, str], CapacityPoint] = field(default_factory=dict)
    #: (alpha, size) -> point, for ROUND_TRIP
    round_trip_points: dict[tuple[str, str], CapacityPoint] = field(default_factory=dict)
    #: alpha -> (contracts, dollars) at net edge > 0
    max_positive: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    #: alpha -> (contracts, dollars) at net edge >= 1c
    max_one_cent: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)


def price_bucket(price: Decimal) -> str:
    """Ten-cent bands. Calibration and fee burden both vary strongly with price."""
    lower = int(price * 100) // 10 * 10
    return f"{lower:02d}-{lower + 10}c"


def spread_bucket(spread: Decimal) -> str:
    cents = spread * 100
    for edge in (1, 2, 3, 5, 10):
        if cents <= edge:
            return f"<={edge}c"
    return ">10c"


def liquidity_bucket(depth_usd: Decimal) -> str:
    for edge in (100, 500, 2_000, 10_000):
        if depth_usd < edge:
            return f"<${edge}"
    return ">=$10000"


def ttl_bucket(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 0:
        return "expired"
    for edge in (1, 3, 6, 12, 24, 48):
        if hours < edge:
            return f"<{edge}h"
    return ">=48h"


def summarise(values: Sequence[Decimal]) -> dict[str, str]:
    """P25 / median / P75 / P90, and the count they were computed from.

    The count travels with the percentiles because a P90 over eleven
    observations is not a percentile, it is the second-largest value, and a
    reader who cannot see ``n`` has no way to know which they are looking at.
    """
    if not values:
        return {"n": "0", "p25": "-", "median": "-", "p75": "-", "p90": "-"}
    ordered = sorted(values)
    n = len(ordered)

    def pick(q: float) -> Decimal:
        return ordered[min(n - 1, max(0, int(q * (n - 1) + 0.5)))]

    return {
        "n": str(n),
        "p25": str(quantize_usd(pick(0.25))),
        "median": str(quantize_usd(pick(0.50))),
        "p75": str(quantize_usd(pick(0.75))),
        "p90": str(quantize_usd(pick(0.90))),
    }


def segment_capacity(
    observations: Iterable[CapacityObservation],
    *,
    alpha: Decimal,
    key: str,
) -> dict[str, dict[str, Any]]:
    """Capacity distribution within one segmentation, for one alpha level."""
    groups: dict[str, list[CapacityObservation]] = {}
    for obs in observations:
        groups.setdefault(getattr(obs, key), []).append(obs)

    out: dict[str, dict[str, Any]] = {}
    alpha_key = str(alpha)
    for name, rows in sorted(groups.items()):
        contracts = [r.max_positive.get(alpha_key, (ZERO, ZERO))[0] for r in rows]
        dollars = [r.max_positive.get(alpha_key, (ZERO, ZERO))[1] for r in rows]
        tradeable = sum(1 for c in contracts if c > 0)
        out[name] = {
            "observations": len(rows),
            "tradeable": tradeable,
            "tradeable_share": f"{tradeable / len(rows):.1%}",
            "contracts": summarise(contracts),
            "dollars": summarise(dollars),
        }
    return out


def minimum_viable_alpha(
    book: BookState,
    *,
    platform: Platform,
    fee_rate: Decimal,
    fee_type: str = "",
    size: Decimal = Decimal("100"),
    economics: Economics = Economics.HOLD_TO_SETTLEMENT,
) -> Decimal | None:
    """The alpha at which this book breaks even at ``size``.

    The inverse of the capacity question and often the more useful number: it
    says what an edge would have to be *worth* before this market is worth
    trading at all, independent of whether such an edge exists.
    """
    point = price_order(
        book,
        size=size,
        alpha=ZERO,
        platform=platform,
        fee_rate=fee_rate,
        fee_type=fee_type,
        economics=economics,
    )
    return None if point is None else point.total_cost


def levels_from_rows(rows: Iterable[tuple[str, str]]) -> tuple[BookLevel, ...]:
    """Build a ladder from stored ``(price, size)`` text, in stored order.

    ``Money`` columns land in SQLite as TEXT; ``Decimal(str)`` is exact where
    ``float`` would not be. Levels arrive already ordered by ``level_index`` and
    that order is preserved rather than re-sorted - the venue's own ordering is
    the one the walk must follow.
    """
    out: list[BookLevel] = []
    for price, size in rows:
        p, s = D(str(price)), D(str(size))
        if p <= ZERO or p >= ONE or s <= ZERO:
            continue
        out.append(BookLevel(price=p, size=s))
    return tuple(out)
