"""An auditable entry-cost estimate, independent of any opinion about the outcome.

Every other analytical surface in this codebase needs a probability before it can
say anything. The independence gate then refuses to supply one for most markets,
which is correct and which is also why those surfaces are usually empty.

This module deliberately needs no probability at all. Given a market and a book it
answers one question -- *what does it cost to get in* -- and that question has an
answer for every market that has a price, on every day, whether or not a model has
an opinion about the outcome.

Three quantities, kept distinct because conflating them is the entire problem:

``nominal_price``
    The number on the venue's screen: best ask, top of book, size one. It is what a
    reader believes they are paying.

``entry_price``
    The volume-weighted average of the ask ladder at the size actually wanted. The
    difference from ``nominal_price`` is *depth impact* -- already a cost, and one
    that a top-of-book quote hides completely.

``measured_cost``
    ``entry_price`` plus the venue fee and documented fee-rounding rule, followed by
    two disclosed scenario inputs: transfer amortisation and the configured annual
    capital rate. The observed/rule-derived inputs and configured assumptions stay
    itemised even though the compatibility field retains its historical name.

That last quantity is the one worth printing in large type, because for a binary
contract paying exactly $1 it maps to the break-even probability under the same
assumptions. A contract quoted at
34c whose measured cost is 37.2c does not need the event to happen 34% of the time
to break even. It needs 37.2%. That gap is not a rounding detail; on small Kalshi
orders it routinely exceeds the entire edge a model would need to find.

Kalshi's fee rounding is why size matters more than anyone expects. The fee is
ceiled to the whole cent *on the whole order*, so one contract at 50c pays the same
fee as several, and the per-contract cost of a 1-lot can be multiples of the same
contract bought 100 at a time. On a 1c contract the ceiled fee is a full cent --
the fee alone doubles the cost of the trade. Reporting the large-order rate to
someone about to buy five would understate their cost by more than the spread.

Slippage is kept **out** of that headline and reported beside it. The pad this
codebase applies is ``tick_size x SLIPPAGE_TICKS``, a flat assumption standing in
for market impact, and the README already lists it as a known limitation rather
than a measurement. At a 1-cent tick it is a whole cent, which on a cheap contract
exceeds every observed or rule-derived component combined -- so a single blended number would make the
product's central claim mostly an artefact of a config default. Measured and
modelled costs are therefore separate fields everywhere, and the measured one
leads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pmvl_shared.config import get_settings
from pmvl_shared.enums import Platform, Side
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import CostBreakdown, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import age_seconds, humanize_seconds

from .execution import (
    build_cost_breakdown,
    capital_cost_per_contract,
    transfer_cost_per_contract,
)
from .fees import fee_per_contract, fee_rounding_cost
from .orderbook import executable_quote

#: Sizes the cost curve is reported at.
#:
#: Starts at 1 and spans three orders of magnitude on purpose. The interesting
#: behaviour is at both ends and in opposite directions: fee rounding punishes the
#: 1-lot, and book depth punishes the 1000-lot. A ladder that started at 10 would
#: hide the first effect, and one that stopped at 100 would hide the second.
DEFAULT_LADDER: tuple[Decimal, ...] = (
    D(1), D(5), D(10), D(25), D(50), D(100), D(250), D(500), D(1000),
)

#: Quotes older than this are labelled stale. Matches the pilot gate's book limit:
#: a book half an hour old is evidence about the past, not a price.
STALE_QUOTE_SECONDS = 1800


@dataclass(frozen=True)
class CostAtSize:
    """The full cost stack for one specific order size."""

    size: Decimal
    filled_size: Decimal
    fully_filled: bool
    #: None on the degraded path, where no ladder was observed.
    levels_consumed: int | None
    nominal_price: Decimal
    entry_price: Decimal
    breakdown: CostBreakdown
    #: Depth impact: the part of the premium caused by walking the ladder rather
    #: than by any fee. None when no ladder was observed, because an unknown depth
    #: impact must not be reported as a zero one.
    depth_impact: Decimal | None
    #: True when this size is below the venue's minimum order.
    #:
    #: Such a size is not merely expensive, it is *unplaceable*, and the two must
    #: not look alike. Polymarket's minimum is 5 contracts, and amortising the
    #: fixed bridge cost over 1 contract produced a premium of 50,000% -- an
    #: arithmetically correct answer to a question about an order nobody can send.
    below_min_order_size: bool = False

    # ---------------------------------------------------------------------
    # Measured cost and modelled cost are kept apart, and the headline figure is
    # the measured one.
    #
    # Depth and venue fees are read or rule-derived. Transfer and capital cost are
    # disclosed configuration assumptions. Slippage is a third, separate modelling
    # assumption and remains outside the headline for compatibility.
    #
    # The slippage pad cannot be verified that way. It is `tick_size x
    # SLIPPAGE_TICKS`, a flat assumption standing in for the market impact between
    # observing a book and reaching it, and this codebase already names it as a
    # known limitation rather than a measurement. At a 1-cent tick it is a whole
    # cent, which on a 2-cent contract is larger than every observed or rule-derived
    # component combined --
    # so folding it into one headline number would mean the product's central
    # claim was mostly a constant from a config file.
    #
    # Reporting them separately keeps the strong claim strong: the measured
    # premium on a 1-lot at 1 cent is a doubling of cost, and that part is real.
    # ---------------------------------------------------------------------

    @property
    def measured_cost(self) -> Decimal:
        """Headline cost per contract under the disclosed configuration.

        Excludes the slippage pad, but includes configured transfer and capital-cost
        assumptions. When depth is unknown it is a floor with respect to book impact,
        not a claim that every other component was directly observed.
        """
        return quantize_usd(
            self.entry_price
            + self.breakdown.platform_fee
            + self.breakdown.fee_rounding
            + self.breakdown.transfer_cost
            + self.breakdown.capital_cost
        )

    @property
    def modelled_slippage(self) -> Decimal:
        """The latency pad. An assumption, not an observation."""
        return quantize_usd(self.breakdown.estimated_slippage)

    @property
    def all_in_cost(self) -> Decimal:
        """Measured cost plus the modelled slippage pad."""
        return quantize_usd(self.measured_cost + self.modelled_slippage)

    @property
    def measured_premium(self) -> Decimal:
        """Headline premium above the screen price under disclosed assumptions."""
        return quantize_usd(self.measured_cost - self.nominal_price)

    @property
    def measured_premium_ratio(self) -> Decimal | None:
        if self.nominal_price <= ZERO:
            return None
        return safe_div(self.measured_premium, self.nominal_price)

    @property
    def premium(self) -> Decimal:
        """All-in cost minus the price on the screen, per contract."""
        return quantize_usd(self.all_in_cost - self.nominal_price)

    @property
    def premium_ratio(self) -> Decimal | None:
        """Premium as a fraction of the nominal price."""
        if self.nominal_price <= ZERO:
            return None
        return safe_div(self.premium, self.nominal_price)

    def _as_probability(self, cost: Decimal) -> Decimal | None:
        """A cost read as a break-even probability, or None if it cannot be one.

        A binary contract pays exactly $1, so cost per contract *is* the
        probability at which the purchase breaks even. Above $1 no probability
        breaks even, and returning 1.03 would be reporting a number that cannot
        exist.
        """
        return cost if ZERO < cost < ONE else None

    @property
    def breakeven_probability(self) -> Decimal | None:
        """Break-even probability on measured cost alone. The headline figure."""
        return self._as_probability(self.measured_cost)

    @property
    def breakeven_probability_with_slippage(self) -> Decimal | None:
        return self._as_probability(self.all_in_cost)

    @property
    def total_outlay(self) -> Decimal:
        """Cash required for the whole order, at the size that actually fills."""
        return quantize_usd(self.measured_cost * self.filled_size)

    def as_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "filled_size": self.filled_size,
            "fully_filled": self.fully_filled,
            "below_min_order_size": self.below_min_order_size,
            "levels_consumed": self.levels_consumed,
            "nominal_price": self.nominal_price,
            "entry_price": self.entry_price,
            # Measured first, because it is the claim the product stands behind.
            "measured_cost": self.measured_cost,
            "measured_premium": self.measured_premium,
            "measured_premium_ratio": self.measured_premium_ratio,
            "breakeven_probability": self.breakeven_probability,
            "modelled_slippage": self.modelled_slippage,
            "all_in_cost": self.all_in_cost,
            "premium": self.premium,
            "premium_ratio": self.premium_ratio,
            "breakeven_probability_with_slippage": (
                self.breakeven_probability_with_slippage
            ),
            "total_outlay": self.total_outlay,
            "measured_components": {
                "depth_impact": self.depth_impact,
                "platform_fee": self.breakdown.platform_fee,
                "fee_rounding": self.breakdown.fee_rounding,
                "transfer_cost": self.breakdown.transfer_cost,
                "capital_cost": self.breakdown.capital_cost,
            },
            "modelled_components": {
                "estimated_slippage": self.modelled_slippage,
            },
        }


@dataclass(frozen=True)
class CostTruth:
    """Cost analysis for one market and side, across a ladder of sizes."""

    side: Side
    #: "orderbook" when a real ask ladder was walked, "venue_summary" when only a
    #: top-of-book price was available and depth is therefore unknown.
    quote_source: str
    quote_observed_at: datetime | None
    quote_age_seconds: int | None
    nominal_price: Decimal
    #: Total ask-side depth in dollars. None when no ladder was observed.
    available_depth_usd: Decimal | None
    #: Largest size the observed ladder can fill. None when depth is unknown.
    max_fillable_size: Decimal | None
    requested: CostAtSize | None
    ladder: list[CostAtSize]
    caveats: list[str]

    @property
    def depth_known(self) -> bool:
        return self.quote_source == "orderbook"

    @property
    def is_stale(self) -> bool:
        return (
            self.quote_age_seconds is not None
            and self.quote_age_seconds > STALE_QUOTE_SECONDS
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side.value if isinstance(self.side, Side) else self.side,
            "quote_source": self.quote_source,
            "quote_observed_at": self.quote_observed_at,
            "quote_age_seconds": self.quote_age_seconds,
            "is_stale": self.is_stale,
            "depth_known": self.depth_known,
            "nominal_price": self.nominal_price,
            "available_depth_usd": self.available_depth_usd,
            "max_fillable_size": self.max_fillable_size,
            "requested": self.requested.as_dict() if self.requested else None,
            "ladder": [entry.as_dict() for entry in self.ladder],
            "caveats": self.caveats,
        }


def _summary_cost_at_size(
    market: NormalizedMarket, nominal: Decimal, size: Decimal
) -> CostAtSize:
    """Cost stack when only a top-of-book price is known.

    Everything that does not require depth is computed exactly: the venue fee at
    this size, its rounding rule, the transfer amortisation and the capital cost.
    What *cannot* be computed is depth impact, because no ladder was observed.

    It is reported as ``None`` rather than zero. Zero would be a claim that the
    whole order fills at the top price, which is the specific optimism this module
    exists to remove; None says the figure is unknown and leaves the reader to
    treat the total as a floor.
    """
    settings = get_settings()
    fee = fee_per_contract(
        market.platform, size, nominal, rate=market.fee_rate, fee_type=market.fee_type
    )
    rounding = fee_rounding_cost(
        market.platform, size, nominal, rate=market.fee_rate, fee_type=market.fee_type
    )
    breakdown = CostBreakdown(
        entry_price=quantize_usd(nominal),
        platform_fee=quantize_usd(max(ZERO, fee - rounding)),
        fee_rounding=quantize_usd(rounding),
        estimated_slippage=quantize_usd(
            market.tick_size * Decimal(settings.slippage_ticks)
        ),
        transfer_cost=transfer_cost_per_contract(market.platform, size),
        capital_cost=capital_cost_per_contract(
            nominal, market.expected_resolution_time
        ),
    )
    return CostAtSize(
        size=size,
        filled_size=size,
        fully_filled=False,  # unknowable without depth; never claimed as true
        levels_consumed=None,
        nominal_price=quantize_usd(nominal),
        entry_price=quantize_usd(nominal),
        breakdown=breakdown,
        depth_impact=None,
        below_min_order_size=size < market.min_order_size,
    )


def _book_cost_at_size(
    market: NormalizedMarket, book: OrderBook, side: Side, size: Decimal
) -> CostAtSize | None:
    quote = executable_quote(book, side, size)
    if quote is None or quote.filled_size <= ZERO:
        return None
    breakdown = build_cost_breakdown(market, book, side, size, quote=quote)
    if breakdown is None:
        return None
    nominal = book.best_ask(side)
    if nominal is None:
        return None
    return CostAtSize(
        size=size,
        filled_size=quote.filled_size,
        fully_filled=quote.fully_filled,
        levels_consumed=quote.levels_consumed,
        nominal_price=quantize_usd(nominal),
        entry_price=quantize_usd(quote.average_price),
        breakdown=breakdown,
        depth_impact=quantize_usd(quote.average_price - nominal),
        below_min_order_size=size < market.min_order_size,
    )


def _caveats(
    market: NormalizedMarket, source: str, observed_age: int | None, stale: bool
) -> list[str]:
    out: list[str] = []
    if source != "orderbook":
        out.append(
            "No order book was captured for this contract, so only the venue's "
            "top-of-book summary price is known. Depth impact cannot be computed "
            "and is omitted rather than assumed to be zero. The published venue "
            "rules still apply and configured transfer/capital assumptions remain "
            "visible. The result is an incomplete floor with respect to depth "
            "impact, not a measured all-in cost."
        )
    if stale and observed_age is not None:
        # `humanize_seconds`, not minutes: a 117-day-old snapshot rendered as
        # "169393 minutes ago", which is a number a reader has to do arithmetic on
        # before it means anything, and staleness is exactly the thing that must
        # land immediately.
        out.append(
            f"This quote was observed {humanize_seconds(observed_age)} ago. It "
            "describes the book at that instant, not now."
        )
    if market.platform == Platform.KALSHI:
        out.append(
            "Kalshi ceils its fee to the whole cent on the whole order, so the "
            "per-contract fee falls as size rises. The cost of a 1-lot is not the "
            "cost of a 100-lot divided by 100."
        )
    if market.platform == Platform.POLYMARKET:
        out.append(
            "Polymarket settles in USDC on Polygon. The bridge and gas allowance is "
            "amortised over the position, which makes small orders structurally "
            "expensive per contract."
        )
    if market.min_order_size > ONE:
        out.append(
            f"This venue will not accept an order below {market.min_order_size} "
            "contracts on this contract, so smaller sizes are excluded from the "
            "ladder rather than priced."
        )
    if market.expected_resolution_time is not None:
        out.append(
            "Capital cost charges for the time the stake is locked up until "
            "resolution. A contract that resolves in nine months costs more to hold "
            "than the same price resolving tomorrow."
        )
    out.append(
        "The break-even figure uses measured cost only: observed depth plus the "
        f"venues' published fee rules. The slippage pad ({market.tick_size} x "
        f"{get_settings().slippage_ticks} tick) is an assumption about market "
        "impact, not an observation, so it is reported separately and excluded "
        "from the headline."
    )
    return out


def analyse_cost(
    market: NormalizedMarket,
    side: Side,
    *,
    book: OrderBook | None = None,
    requested_size: Decimal | None = None,
    summary_ask: Decimal | None = None,
    quote_observed_at: datetime | None = None,
    now: datetime | None = None,
    ladder: tuple[Decimal, ...] = DEFAULT_LADDER,
) -> CostTruth | None:
    """Cost of entering ``side`` of ``market``, at one size and across a ladder.

    Prefers a real ask ladder. Falls back to the venue's top-of-book summary when no
    book was captured, because a partial answer that names what is missing is more
    useful to someone deciding whether to trade than no answer at all -- and the
    fee, transfer and capital components, which are exact either way, are usually
    the larger part of the premium at small size.

    Returns None only when there is no ask price from any source, which is the one
    case where nothing can honestly be said.
    """
    # `age_seconds` coerces the naive datetimes SQLite hands back to UTC. Doing the
    # subtraction here instead would raise on every row read from the database.
    raw_age = age_seconds(quote_observed_at, now=now)
    age = max(0, int(raw_age)) if raw_age is not None else None

    has_book = book is not None and book.best_ask(side) is not None
    source = "orderbook" if has_book else "venue_summary"

    if has_book:
        assert book is not None
        nominal = book.best_ask(side)
        assert nominal is not None
        depth_usd = book.depth_notional(side)
        max_fillable = sum((lvl.size for lvl in book.asks(side)), ZERO)
    elif summary_ask is not None and summary_ask > ZERO:
        nominal = summary_ask
        depth_usd = None
        max_fillable = None
    else:
        return None

    sizes = sorted(set(ladder) | ({requested_size} if requested_size else set()))
    entries: list[CostAtSize] = []
    for size in sizes:
        if size <= ZERO:
            continue
        entry = (
            _book_cost_at_size(market, book, side, size)  # type: ignore[arg-type]
            if has_book
            else _summary_cost_at_size(market, nominal, size)
        )
        if entry is not None:
            entries.append(entry)

    if not entries:
        return None

    requested_entry = None
    if requested_size is not None:
        requested_entry = next(
            (e for e in entries if e.size == requested_size), None
        )

    stale = age is not None and age > STALE_QUOTE_SECONDS
    return CostTruth(
        side=side,
        quote_source=source,
        quote_observed_at=quote_observed_at,
        quote_age_seconds=age,
        nominal_price=quantize_usd(nominal),
        available_depth_usd=quantize_usd(depth_usd) if depth_usd is not None else None,
        max_fillable_size=max_fillable,
        requested=requested_entry,
        # Unplaceable sizes are dropped from the comparison ladder but kept
        # available as an explicit `requested` answer. Showing Polymarket's 1-lot
        # beside its 5-lot invites a reader to compare a source-backed estimate against the
        # cost of an order the venue would reject, and the unplaceable one always
        # looks worse -- so the ladder would appear to teach a lesson about fees
        # that is really a lesson about a minimum. Asking for that size directly
        # still gets an answer, flagged.
        ladder=[
            e for e in entries if e.size in ladder and not e.below_min_order_size
        ],
        caveats=_caveats(market, source, age, stale),
    )
