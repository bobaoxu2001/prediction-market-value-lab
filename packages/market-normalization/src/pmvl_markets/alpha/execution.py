"""Model C: what it costs to get filled, and whether you get filled at all.

The rule this module exists to enforce
--------------------------------------

    **A maker order is not filled because the quote touched its price.**

This is the single most common way a prediction-market backtest becomes fiction.
A resting bid at 29c and a best bid that ticks down to 29c look, in candle data,
exactly like a fill. They are not one:

* the order is at the **back of the queue** at that price. Everything already
  resting there trades first, and on a venue where a level holds thousands of
  contracts a touch clears a fraction of one percent of it;
* a touch with **no traded volume** is a quote that moved, not a trade that
  happened. Nobody crossed;
* the touches that *do* fill are adversely selected. An order fills fastest
  exactly when the market is moving through it, which is when you would least
  like to own it.

So the default fill model here requires **traded volume through the level**, and
even then fills only the share of that volume a queue position could plausibly
have captured. Where the historical data cannot support that, it says so through
:class:`FillConfidence` rather than substituting a number.

Preferring to be wrong in one direction
---------------------------------------
Every choice below is made in the direction that *understates* performance. A
backtest that overstates is worse than useless - it is expensive - while one that
understates leaves real strategies on the table, which costs opportunity and not
money. When a modelling choice was genuinely ambiguous, the conservative branch
was taken and the docstring says which one it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Sequence

from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import ONE, ZERO, D, quantize_prob, quantize_usd, safe_div
from pmvl_shared.schemas import NormalizedMarket, OrderBook

from ..pricing.fees import fee_per_contract, kalshi_maker_fee
from ..pricing.orderbook import executable_quote
from .models import ExecutionAssessment, Liquidity, ProposedOrder


class FillConfidence(str, Enum):
    """How much the fill estimate can be trusted, given the data behind it.

    Reported on every simulated fill and carried onto the trade, so a run's
    overall confidence is the *worst* of its trades - the same rule the existing
    backtest applies to ``data_quality``, for the same reason. One
    queue-unmodelled fill must not hide inside an aggregate that reads as
    executable.
    """

    #: Traded volume through the level, with a queue position estimate.
    TRADE_CONFIRMED = "trade_confirmed"
    #: Traded volume through the level, no queue data - share assumed.
    TRADE_CONFIRMED_QUEUE_UNKNOWN = "trade_confirmed_queue_unknown"
    #: The book crossed the price outright. Stronger than a touch, still not a
    #: guarantee that this particular order was reached.
    CROSSED_NO_VOLUME = "crossed_no_volume"
    #: Price merely touched the level. Never a fill under the default model.
    TOUCH_ONLY = "touch_only"
    #: Taker order lifted from the observed ladder. The one certain case.
    TAKER_FROM_LADDER = "taker_from_ladder"


@dataclass(frozen=True)
class LevelTrade:
    """Public trade prints at or through a price, over a simulated interval.

    ``volume`` is contracts, not dollars: queue position is a contract count and
    converting through a price would lose the thing being modelled.
    """

    price: Decimal
    volume: Decimal
    #: Contracts resting ahead of our order when it was posted, if known.
    queue_ahead: Decimal | None = None


@dataclass(frozen=True)
class MakerQuote:
    """A resting order, priced but not yet filled.

    Separate type from :class:`~pmvl_markets.pricing.execution` outputs on
    purpose. Everything in that module is a *taker* cost: an entry price that
    exists because a ladder was walked. A maker quote has no entry price yet -
    it has a request, and whether it becomes an entry is what this module
    decides.
    """

    market_key: str
    side: Side
    limit_price: Decimal
    size: Decimal
    posted_at_best_bid: Decimal | None = None
    posted_at_best_ask: Decimal | None = None

    @property
    def improves_the_bid(self) -> bool:
        """Whether this order is the new best bid.

        An order that merely joins the existing best bid is behind the whole
        queue at that price. Improving it puts you in front, which is the only
        version of "resting" with a fill probability worth simulating.
        """
        if self.posted_at_best_bid is None:
            return False
        return self.limit_price > self.posted_at_best_bid

    @property
    def crosses_the_offer(self) -> bool:
        """A "maker" order priced at or above the ask is a taker order.

        It would execute immediately at a worse price than the maker path claims,
        so treating it as a maker fill would credit a rebate to a trade that paid
        the spread.
        """
        if self.posted_at_best_ask is None:
            return False
        return self.limit_price >= self.posted_at_best_ask


@dataclass(frozen=True)
class FillResult:
    """What actually happened to an order, in the simulation."""

    filled_size: Decimal
    fill_price: Decimal | None
    confidence: FillConfidence
    #: Fraction of the requested size that filled. Partial fills are normal.
    fill_ratio: Decimal
    reason: str = ""

    @property
    def filled(self) -> bool:
        return self.filled_size > ZERO

    @property
    def partial(self) -> bool:
        return self.filled and self.fill_ratio < ONE


@dataclass(frozen=True)
class ConservativeFillModel:
    """The default maker-fill model. Deliberately pessimistic.

    Rules, in order:

    1. **A cross without volume does not fill.** The book trading through a price
       with no print is a quote update. Configurable, because some historical
       feeds record crosses reliably and prints unreliably, but the default is
       off and the resulting fills are labelled ``CROSSED_NO_VOLUME`` so they can
       be excluded from any headline.
    2. **A touch never fills.** No exception, no flag.
    3. **With volume, fill the share a queue position could have captured.**
       Given ``queue_ahead``, the order fills only volume beyond it. Without it,
       ``blind_queue_share`` of the printed volume is assumed - a stated
       assumption, flagged as ``TRADE_CONFIRMED_QUEUE_UNKNOWN``.
    4. **Never fill more than was requested**, and never more than traded.

    ``blind_queue_share`` defaults to 0.25 rather than 1.0. Assuming the whole
    print would say that our order was alone at the price, which on a venue with
    real participants is the least likely arrangement rather than the neutral
    one.
    """

    name: str = "conservative_queue_v0"
    #: Whether an outright cross with no printed volume may fill.
    allow_cross_without_volume: bool = False
    #: Share of printed volume assumed captured when queue position is unknown.
    blind_queue_share: Decimal = Decimal("0.25")
    #: Charged against a maker fill for being selected against, per contract.
    adverse_selection_ticks: Decimal = Decimal("0.5")

    def simulate(
        self,
        quote: MakerQuote,
        trades: Sequence[LevelTrade],
        *,
        crossed: bool = False,
    ) -> FillResult:
        """Decide whether ``quote`` filled, and for how much."""
        if quote.crosses_the_offer:
            return FillResult(
                ZERO,
                None,
                FillConfidence.TOUCH_ONLY,
                ZERO,
                reason=(
                    "the order is priced at or through the offer; it is a taker "
                    "order and must be priced as one"
                ),
            )

        eligible = [t for t in trades if t.price <= quote.limit_price]
        traded = sum((t.volume for t in eligible), ZERO)

        if traded <= ZERO:
            if crossed and self.allow_cross_without_volume:
                size = min(quote.size, quote.size * self.blind_queue_share)
                return FillResult(
                    _floor_contracts(size),
                    quote.limit_price,
                    FillConfidence.CROSSED_NO_VOLUME,
                    quantize_prob(safe_div(size, quote.size)),
                    reason=(
                        "the book crossed the level with no printed volume; "
                        "filled under an explicit assumption, not evidence"
                    ),
                )
            return FillResult(
                ZERO,
                None,
                FillConfidence.TOUCH_ONLY,
                ZERO,
                reason=(
                    "no traded volume at or through the level. A quote touching "
                    "a resting order is not a trade against it."
                ),
            )

        known_queue = [t for t in eligible if t.queue_ahead is not None]
        if known_queue:
            ahead = min(t.queue_ahead for t in known_queue)  # type: ignore[type-var]
            available = max(ZERO, traded - ahead)
            confidence = FillConfidence.TRADE_CONFIRMED
            reason = f"{traded} traded through the level, {ahead} ahead in queue"
        else:
            available = traded * self.blind_queue_share
            confidence = FillConfidence.TRADE_CONFIRMED_QUEUE_UNKNOWN
            reason = (
                f"{traded} traded through the level; queue position unknown, so "
                f"{self.blind_queue_share} of the print is assumed captured"
            )

        filled = _floor_contracts(min(quote.size, available))
        if filled <= ZERO:
            return FillResult(
                ZERO,
                None,
                confidence,
                ZERO,
                reason=reason + " - the queue ahead absorbed the whole print",
            )
        return FillResult(
            filled,
            quote.limit_price,
            confidence,
            quantize_prob(safe_div(filled, quote.size)),
            reason=reason,
        )


def _floor_contracts(size: Decimal) -> Decimal:
    """Round a simulated fill DOWN to a whole contract.

    Down, always. Rounding a 0.8-contract fill up to one is a free contract at a
    price nobody offered, and across a long run those add up in exactly the
    direction that flatters the strategy.
    """
    return size.to_integral_value(rounding="ROUND_FLOOR")


@dataclass(frozen=True)
class TakerExecutionModel:
    """Model C for orders that lift the offer.

    The one certain case, and still not free: the price is the VWAP of the ladder
    at the size requested, not top of book, and a book that cannot fill the size
    produces a partial fill rather than an extrapolated one.
    """

    name: str = "taker_ladder_v0"

    def price_order(
        self, market: NormalizedMarket, book: OrderBook, order: ProposedOrder
    ) -> ExecutionAssessment:
        quote = executable_quote(book, order.side, order.size)
        if quote is None or quote.filled_size <= ZERO:
            return ExecutionAssessment(
                fill_probability=ZERO,
                expected_edge_conditional_on_fill=ZERO,
                expected_fill_size=ZERO,
                fill_model=self.name,
                fill_confidence=FillConfidence.TAKER_FROM_LADDER.value,
                notes=("the ask ladder cannot fill any size",),
            )
        fee = fee_per_contract(
            market.platform,
            quote.filled_size,
            quote.average_price,
            rate=market.fee_rate,
            fee_type=market.fee_type,
        )
        edge = order.conservative_probability - quote.average_price - fee
        return ExecutionAssessment(
            # Certain in the sense that matters here: the ladder was observed and
            # the size is available in it. Latency between observing and sending
            # is charged as slippage in the cost stack, not modelled as a chance
            # of not filling.
            fill_probability=ONE,
            expected_edge_conditional_on_fill=quantize_usd(edge),
            expected_fill_size=quote.filled_size,
            fill_model=self.name,
            fill_confidence=FillConfidence.TAKER_FROM_LADDER.value,
            notes=(
                f"VWAP {quote.average_price} over {quote.levels_consumed} levels",
                "fully filled" if quote.fully_filled else "PARTIAL: ladder ran out",
            ),
        )


@dataclass(frozen=True)
class MakerExecutionModel:
    """Model C for resting orders.

    Fill probability here is an **estimate from observed flow**, not a
    certainty and not a constant. Where there is no flow to estimate from it
    returns zero with a note, because a maker strategy whose fill probability was
    assumed is a maker strategy whose entire result is the assumption.
    """

    fill_model: ConservativeFillModel = ConservativeFillModel()
    name: str = "maker_queue_v0"

    def price_order(
        self, market: NormalizedMarket, book: OrderBook, order: ProposedOrder
    ) -> ExecutionAssessment:
        ask = book.best_ask(order.side)
        bid = book.best_bid(order.side)
        if ask is not None and order.limit_price >= ask:
            return ExecutionAssessment(
                fill_probability=ZERO,
                expected_edge_conditional_on_fill=ZERO,
                expected_fill_size=ZERO,
                fill_model=self.name,
                fill_confidence=FillConfidence.TOUCH_ONLY.value,
                notes=(
                    "priced at or through the offer: this is a taker order and "
                    "must be assessed by the taker model",
                ),
            )
        fee = _maker_fee_per_contract(market, order.size, order.limit_price)
        edge = order.conservative_probability - order.limit_price - fee
        tick = market.tick_size if market.tick_size > ZERO else Decimal("0.01")
        adverse = quantize_usd(tick * self.fill_model.adverse_selection_ticks)
        return ExecutionAssessment(
            # No flow is supplied at pricing time, so no fill probability is
            # claimed. The simulator estimates it from what actually traded; a
            # constant here would be an invented number in the one place it
            # matters most.
            fill_probability=ZERO,
            expected_edge_conditional_on_fill=quantize_usd(edge),
            expected_fill_size=ZERO,
            fill_model=self.name,
            fill_confidence=FillConfidence.TOUCH_ONLY.value,
            adverse_selection_penalty=adverse,
            notes=(
                f"resting at {order.limit_price} against bid {bid} / ask {ask}",
                "fill probability is not asserted here; it comes from observed "
                "flow in the simulator",
            ),
        )


def _maker_fee_per_contract(
    market: NormalizedMarket, size: Decimal, price: Decimal
) -> Decimal:
    """Maker fee from the venue's published schedule.

    Kalshi charges makers only on series whose ``fee_type`` names maker fees;
    Polymarket states that makers are never charged. A series this code has not
    seen pays nothing rather than a guessed rate - the direction that does not
    manufacture edge.
    """
    if market.platform != Platform.KALSHI:
        return ZERO
    if "maker" not in (market.fee_type or ""):
        return ZERO
    rate = market.maker_fee_rate
    if rate <= ZERO:
        return ZERO
    return safe_div(kalshi_maker_fee(size, price, rate=rate), size)


def maker_limit_price(
    market: NormalizedMarket, book: OrderBook, side: Side
) -> Decimal | None:
    """Where a maker order would rest: one tick inside the touch.

    Returns ``None`` when there is no room - a one-tick-wide book has nowhere to
    improve without crossing, and posting there would be a taker order under a
    maker label.
    """
    bid, ask = book.best_bid(side), book.best_ask(side)
    if bid is None or ask is None:
        return None
    tick = market.tick_size if market.tick_size > ZERO else Decimal("0.01")
    proposed = bid + tick
    return proposed if proposed < ask else None


def exit_proceeds_per_contract(
    market: NormalizedMarket, book: OrderBook, side: Side, size: Decimal
) -> Decimal | None:
    """What closing a position would actually realise, per contract.

    Exiting sells into the **bid** ladder, and the bid is not the ask. A
    convergence strategy that marks its exit at the ask has not modelled an exit
    at all; it has recorded the price it wishes it could sell at.

    Fees are charged on the exit too. Kalshi's quadratic fee applies to the
    closing trade the same way it applied to the opening one.
    """
    bids = book.bids(side)
    if not bids:
        return None
    remaining, proceeds, filled = size, ZERO, ZERO
    for level in bids:
        if remaining <= ZERO:
            break
        take = min(remaining, level.size)
        proceeds += take * level.price
        filled += take
        remaining -= take
    if filled <= ZERO:
        return None
    average = safe_div(proceeds, filled)
    fee = fee_per_contract(
        market.platform, filled, average, rate=market.fee_rate, fee_type=market.fee_type
    )
    return quantize_usd(average - fee)


def worst_fill_confidence(values: Sequence[FillConfidence]) -> FillConfidence:
    """The weakest confidence in a set.

    A run's confidence is the worst of its trades, not the average. Averaging
    would let a single queue-unmodelled fill disappear into an aggregate that
    reads as executable - the same reason the existing backtest takes the worst
    ``data_quality`` rather than the mean.
    """
    order = {
        FillConfidence.TOUCH_ONLY: 0,
        FillConfidence.CROSSED_NO_VOLUME: 1,
        FillConfidence.TRADE_CONFIRMED_QUEUE_UNKNOWN: 2,
        FillConfidence.TRADE_CONFIRMED: 3,
        FillConfidence.TAKER_FROM_LADDER: 4,
    }
    if not values:
        return FillConfidence.TOUCH_ONLY
    return min(values, key=lambda value: order[value])


def liquidity_of(order: ProposedOrder) -> Liquidity:
    """Read back the order's own declaration, for callers that only have one."""
    return order.liquidity


def implied_break_even(price: Decimal, fee: Decimal) -> Decimal:
    """The probability an entry at ``price`` plus ``fee`` needs to break even.

    A binary contract pays exactly $1, so the all-in cost per contract *is* the
    break-even probability. A contract quoted at 34c with an all-in cost of 37.2c
    needs the event 37.2% of the time, not 34%.
    """
    return quantize_prob(D(str(price + fee)))
