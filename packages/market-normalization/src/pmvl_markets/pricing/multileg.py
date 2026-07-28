"""Multi-leg execution simulation.

A multi-leg trade is only worth what its *scarcest* leg can fill. A basket priced at
a 3% edge on top-of-book quotes is worth nothing if one leg has forty contracts
behind it and the others have four thousand: you either scale the whole basket down
to forty, or you fill three legs and hold a naked position on the fourth.

This module answers two questions the single-leg pricer cannot:

1. What is the largest size at which **every** leg still fills?
2. If the basket is only partly filled, what is actually left on the book?

The second matters because partial fill on an arbitrage is not a smaller arbitrage -
it is a directional position that was never intended, taken at a price chosen for a
hedged trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pmvl_shared.enums import Side
from pmvl_shared.money import D, ONE, ZERO, quantize_usd, safe_div
from pmvl_shared.schemas import ExecutionQuote, OrderBook

from .orderbook import available_size, executable_quote


@dataclass(frozen=True)
class LegRequest:
    """One leg of a proposed basket."""

    label: str
    book: OrderBook
    side: Side
    #: Contracts of this leg per one unit of the basket. A complete set buys one of
    #: each outcome, so every ratio is 1; a hedge ratio may differ.
    ratio: Decimal = ONE


@dataclass
class LegFill:
    """What one leg actually achieved.

    Three quantities, deliberately kept apart:

    ``requested``
        what the caller originally asked for on this leg
    ``planned``
        what the leg was asked for after the WHOLE basket was scaled to the size every
        leg could support
    ``filled``
        what the book actually delivered against ``planned``

    Conflating the first and third is what made a uniformly scaled-down basket look
    like a naked position. Scaling every leg from 100 to 40 leaves a fully hedged 40;
    only a leg that misses its *planned* size leaves exposure.
    """

    label: str
    side: Side
    requested: Decimal
    planned: Decimal
    filled: Decimal
    average_price: Decimal
    notional: Decimal
    available: Decimal

    @property
    def fully_filled(self) -> bool:
        """Whether the leg delivered the size the basket planned for it."""
        return self.filled >= self.planned

    @property
    def shortfall_vs_plan(self) -> Decimal:
        """Size missed against the plan. Non-zero means real exposure."""
        return max(ZERO, self.planned - self.filled)

    @property
    def shortfall_vs_requested(self) -> Decimal:
        """Size given up by scaling the basket down. Not exposure."""
        return max(ZERO, self.requested - self.filled)

    #: Retained: callers reading "unfilled" mean the shortfall against the plan.
    @property
    def unfilled(self) -> Decimal:
        return self.shortfall_vs_plan


@dataclass
class BasketExecution:
    """Simulated execution of a whole basket."""

    requested_units: Decimal
    executable_units: Decimal
    legs: list[LegFill] = field(default_factory=list)
    binding_leg: str | None = None

    @property
    def fully_executable(self) -> bool:
        """Whether the basket ran at the size the caller asked for."""
        return self.executable_units >= self.requested_units and self.requested_units > 0

    @property
    def scaled_down(self) -> bool:
        """The basket ran, but smaller than requested, because a leg was thin."""
        return 0 < self.executable_units < self.requested_units

    @property
    def fully_hedged(self) -> bool:
        """Every leg delivered its planned size, so no leg is exposed.

        True for a scaled-down basket. Reducing 100 sets to 40 across ALL legs is a
        smaller hedged position, not an unhedged one - the distinction the earlier
        version got wrong by comparing each fill against the original request.
        """
        return self.executable_units > 0 and all(leg.fully_filled for leg in self.legs)

    @property
    def naked_exposure(self) -> bool:
        """At least one leg missed its planned size while others filled."""
        return self.executable_units > 0 and not self.fully_hedged

    @property
    def shortfall_vs_requested(self) -> Decimal:
        return max(ZERO, self.requested_units - self.executable_units)

    @property
    def total_cost(self) -> Decimal:
        return quantize_usd(sum((leg.notional for leg in self.legs), ZERO))

    @property
    def cost_per_unit(self) -> Decimal:
        return quantize_usd(safe_div(self.total_cost, self.executable_units))

    @property
    def unfilled_legs(self) -> list[LegFill]:
        """Legs that missed their PLANNED size. Non-empty means real exposure."""
        return [leg for leg in self.legs if not leg.fully_filled]

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_units": str(self.requested_units),
            "executable_units": str(self.executable_units),
            "fully_executable": self.fully_executable,
            "scaled_down": self.scaled_down,
            "fully_hedged": self.fully_hedged,
            "naked_exposure": self.naked_exposure,
            "shortfall_vs_requested": str(self.shortfall_vs_requested),
            "binding_leg": self.binding_leg,
            "total_cost": str(self.total_cost),
            "cost_per_unit": str(self.cost_per_unit),
            "legs": [
                {
                    "label": leg.label,
                    "side": leg.side.value,
                    "requested": str(leg.requested),
                    "planned": str(leg.planned),
                    "filled": str(leg.filled),
                    "shortfall_vs_plan": str(leg.shortfall_vs_plan),
                    "shortfall_vs_requested": str(leg.shortfall_vs_requested),
                    "average_price": str(leg.average_price),
                    "notional": str(leg.notional),
                    "fully_filled": leg.fully_filled,
                    "available": str(leg.available),
                }
                for leg in self.legs
            ],
        }


def max_executable_units(legs: list[LegRequest]) -> tuple[Decimal, str | None]:
    """Largest unit count at which every leg still fills, and which leg binds.

    Returns ``(0, label)`` as soon as any leg is empty: a basket missing one leg is
    not a partially-available basket, it is unavailable.
    """
    if not legs:
        return ZERO, None

    best: Decimal | None = None
    binding: str | None = None
    for leg in legs:
        if leg.ratio <= 0:
            continue
        capacity = safe_div(available_size(leg.book, leg.side), leg.ratio)
        if capacity <= 0:
            return ZERO, leg.label
        if best is None or capacity < best:
            best, binding = capacity, leg.label
    return (best if best is not None else ZERO), binding


def simulate_basket(legs: list[LegRequest], units: Decimal) -> BasketExecution:
    """Walk every leg's book for ``units`` of the basket.

    Each leg is walked for the size it would actually need, so the reported average
    price includes the impact of climbing that leg's ladder. Legs are never assumed to
    fill at top of book.
    """
    execution = BasketExecution(requested_units=units, executable_units=ZERO)
    if not legs or units <= 0:
        return execution

    capacity, binding = max_executable_units(legs)
    execution.binding_leg = binding
    execution.executable_units = min(units, capacity)

    for leg in legs:
        requested = quantize_usd(units * leg.ratio)
        # The plan is the basket's achievable size, not the caller's original ask.
        planned = quantize_usd(execution.executable_units * leg.ratio)
        target = execution.executable_units * leg.ratio
        quote: ExecutionQuote | None = (
            executable_quote(leg.book, leg.side, target) if target > 0 else None
        )
        if quote is None:
            execution.legs.append(
                LegFill(
                    label=leg.label,
                    side=leg.side,
                    requested=requested,
                    planned=planned,
                    filled=ZERO,
                    average_price=ZERO,
                    notional=ZERO,
                    available=available_size(leg.book, leg.side),
                )
            )
            continue
        execution.legs.append(
            LegFill(
                label=leg.label,
                side=leg.side,
                requested=requested,
                planned=planned,
                filled=quote.filled_size,
                average_price=quote.average_price,
                notional=quantize_usd(quote.notional),
                available=available_size(leg.book, leg.side),
            )
        )
    return execution


def basket_edge(
    execution: BasketExecution,
    *,
    guaranteed_payout_per_unit: Decimal,
    fees_per_unit: Decimal = ZERO,
) -> dict[str, Decimal]:
    """Net edge of an executed basket, as a fraction of capital deployed.

    ``guaranteed_payout_per_unit`` is the terminal value of one basket - $1.00 for a
    complete set. Returns zeros for an empty execution rather than dividing by zero.
    """
    units = execution.executable_units
    if units <= 0:
        return {
            "gross_payout": ZERO,
            "total_cost": ZERO,
            "fees": ZERO,
            "net_profit": ZERO,
            "net_edge": ZERO,
        }

    gross = quantize_usd(guaranteed_payout_per_unit * units)
    fees = quantize_usd(fees_per_unit * units)
    cost = execution.total_cost
    net = quantize_usd(gross - cost - fees)
    # Edge is expressed against capital actually deployed, which is what a reader
    # needs to compare an opportunity against any other use of the same money.
    return {
        "gross_payout": gross,
        "total_cost": cost,
        "fees": fees,
        "net_profit": net,
        "net_edge": quantize_usd(safe_div(net, cost)),
    }
