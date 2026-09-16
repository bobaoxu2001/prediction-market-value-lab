"""Venue-neutral order primitives, so a second venue is not modelled as the first.

:class:`~pmvl_markets.execution.models.OrderIntent` is Kalshi-shaped by
construction - integer cents, integer contracts, a ticker. Those are not general
facts about prediction markets; they are Kalshi's conventions. Polymarket prices in
USDC with six decimals against an ERC-1155 token id, and its sizes are not integers.
Reusing the Kalshi type for both would round a Polymarket price to the nearest cent
and a size to the nearest whole share, and the rounding error lands squarely in the
number that decides whether the trade has an edge at all.

So the venue-specific constraints live in data, on :class:`VenueContract`, read from
each venue's own API rather than assumed:

* the price grid and how far a price may be rounded before it is a different order;
* the size grid, minimum size and minimum notional;
* which time-in-force values the venue actually honours;
* the fee model, **including the case where the fee is not known**.

That last one is the one worth stating plainly. ``fee_rate=None`` means the venue did
not tell us, and :meth:`VenueContract.fee_for` raises rather than returning zero. An
unknown cost recorded as zero is an edge that does not exist, and it will be
discovered only after the money is committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Mapping

from pmvl_shared.enums import Platform
from pmvl_shared.money import ONE, ZERO, quantize_usd

from ..pricing.fees import taker_fee
from .models import Outcome, TimeInForce

__all__ = [
    "UnknownFeeError",
    "ContractConstraintError",
    "VenueContract",
    "VenueOrderSpec",
    "kalshi_contract",
    "polymarket_contract",
]


class UnknownFeeError(RuntimeError):
    """Raised when a fee must be charged but the venue never published its rate.

    Deliberately an exception rather than a sentinel. A sentinel propagates into
    arithmetic; an exception stops the sizing decision at the point where the missing
    input is still attributable to a specific contract.
    """


class ContractConstraintError(ValueError):
    """An order that the venue's own published constraints would reject."""


@dataclass(frozen=True, slots=True)
class VenueContract:
    """One tradeable side of one contract, plus the venue's rules for trading it.

    ``contract_key`` is the identifier actually sent to the venue: a Kalshi ticker,
    or a Polymarket ERC-1155 token id. ``market_ref`` groups the two sides of the
    same question - a Kalshi ticker again, a Polymarket condition id - and is what
    settlement reasoning keys on. They differ on Polymarket, where YES and NO are
    separate tokens of one condition, and coincide on Kalshi, where one ticker has
    two sides. Keeping both means neither venue has to be described in the other's
    vocabulary.
    """

    venue: Platform
    contract_key: str
    market_ref: str
    outcome: Outcome

    #: Smallest price increment the venue accepts. Kalshi: $0.01. Polymarket: varies
    #: by market and is read from the API, not assumed.
    tick_size: Decimal = Decimal("0.01")
    #: Smallest size increment. Whole contracts on Kalshi; fractional shares on
    #: Polymarket. Not hardcoded as 1 anywhere.
    quantity_step: Decimal = ONE
    min_quantity: Decimal = ONE
    min_notional_usd: Decimal = ZERO

    #: Per-market taker fee rate as the venue publishes it. ``None`` means unknown,
    #: which is not the same as zero and is not allowed to behave like it.
    fee_rate: Decimal | None = None
    fee_type: str = ""

    supported_tif: frozenset[TimeInForce] = field(
        default_factory=lambda: frozenset({TimeInForce.GOOD_TILL_CANCELLED})
    )
    #: Where the constraints above came from, so a wrong one can be traced.
    constraints_source: str = ""

    def __post_init__(self) -> None:
        if self.tick_size <= ZERO:
            raise ValueError(f"{self.contract_key}: tick_size must be positive")
        if self.quantity_step <= ZERO:
            raise ValueError(f"{self.contract_key}: quantity_step must be positive")

    # ------------------------------------------------------------------ pricing
    def round_price_for_buy(self, price: Decimal) -> Decimal:
        """Snap a limit price up to the venue's grid.

        Up, for a buy: rounding a buy limit *down* to the grid produces an order the
        venue accepts and the book never touches, which looks like "no liquidity"
        rather than like a bug in the price.
        """
        steps = (price / self.tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        snapped = steps * self.tick_size
        if snapped < price:
            snapped += self.tick_size
        return snapped

    def round_quantity_down(self, quantity: Decimal) -> Decimal:
        """Snap a size down to the venue's grid. Never up: up overspends."""
        steps = (quantity / self.quantity_step).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        )
        return steps * self.quantity_step

    def fee_for(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Taker fee for this size at this price, using the venue's own model.

        Raises :class:`UnknownFeeError` when the rate was never published. The
        alternative - returning zero - is how a trade with no edge acquires one.
        """
        if self.fee_rate is None:
            raise UnknownFeeError(
                f"{self.venue.value}:{self.contract_key} has no published fee rate; "
                "refusing to price it as free"
            )
        return taker_fee(
            self.venue, quantity, price, rate=self.fee_rate, fee_type=self.fee_type
        )

    # -------------------------------------------------------------- validation
    def violations(self, *, price: Decimal, quantity: Decimal) -> list[str]:
        """Every published constraint this order breaks. Empty means acceptable."""
        problems: list[str] = []
        if price <= ZERO or price >= ONE:
            problems.append(f"price {price} is outside (0, 1)")
        if price % self.tick_size != ZERO:
            problems.append(f"price {price} is off the {self.tick_size} tick grid")
        if quantity <= ZERO:
            problems.append(f"quantity {quantity} is not positive")
        elif quantity % self.quantity_step != ZERO:
            problems.append(
                f"quantity {quantity} is off the {self.quantity_step} size grid"
            )
        if quantity < self.min_quantity:
            problems.append(
                f"quantity {quantity} is below the venue minimum {self.min_quantity}"
            )
        notional = price * quantity
        if notional < self.min_notional_usd:
            problems.append(
                f"notional ${quantize_usd(notional)} is below the venue minimum "
                f"${quantize_usd(self.min_notional_usd)}"
            )
        if self.fee_rate is None:
            problems.append("fee rate is unknown; cost cannot be bounded")
        return problems


@dataclass(frozen=True, slots=True)
class VenueOrderSpec:
    """A buy order expressed in the venue's own units, before any gate has run.

    This round only constructs buys. Selling a contract short is a different risk
    object - it needs borrow, margin and a liquidation model that none of this code
    has - and the complementary-outcome structure does not require it.
    """

    contract: VenueContract
    limit_price: Decimal
    quantity: Decimal
    time_in_force: TimeInForce = TimeInForce.GOOD_TILL_CANCELLED

    def __post_init__(self) -> None:
        if self.time_in_force not in self.contract.supported_tif:
            raise ContractConstraintError(
                f"{self.contract.venue.value} does not support "
                f"{self.time_in_force.value} on {self.contract.contract_key}"
            )

    @property
    def notional_usd(self) -> Decimal:
        return self.limit_price * self.quantity

    @property
    def max_fee_usd(self) -> Decimal:
        return self.contract.fee_for(self.quantity, self.limit_price)

    @property
    def max_cash_out_usd(self) -> Decimal:
        """Most this order can cost if it fills completely at the limit.

        This is the number funding reserves against. A buy cannot cost more than
        limit x size plus the fee on that, so the reservation is exact rather than a
        guess with a margin bolted on.
        """
        return self.notional_usd + self.max_fee_usd

    def violations(self) -> list[str]:
        return self.contract.violations(
            price=self.limit_price, quantity=self.quantity
        )


# ------------------------------------------------------------------- builders
def kalshi_contract(
    ticker: str,
    outcome: Outcome,
    *,
    fee_rate: Decimal | None,
    fee_type: str = "",
    min_quantity: Decimal = ONE,
    source: str = "",
) -> VenueContract:
    """A Kalshi contract side.

    Kalshi's grid is a whole cent and a whole contract. Those two *are* venue-wide on
    Kalshi, so they are defaults here rather than in :class:`VenueContract` itself -
    the generic type must not learn them.
    """
    return VenueContract(
        venue=Platform.KALSHI,
        contract_key=ticker,
        market_ref=ticker,
        outcome=outcome,
        tick_size=Decimal("0.01"),
        quantity_step=ONE,
        min_quantity=min_quantity,
        fee_rate=fee_rate,
        fee_type=fee_type,
        supported_tif=frozenset(
            {
                TimeInForce.GOOD_TILL_CANCELLED,
                TimeInForce.IMMEDIATE_OR_CANCEL,
                TimeInForce.FILL_OR_KILL,
            }
        ),
        constraints_source=source or "kalshi:defaults",
    )


def polymarket_contract(
    token_id: str,
    condition_id: str,
    outcome: Outcome,
    *,
    fee_rate: Decimal | None,
    tick_size: Decimal,
    min_quantity: Decimal,
    min_notional_usd: Decimal,
    quantity_step: Decimal = Decimal("0.01"),
    source: str = "",
) -> VenueContract:
    """A Polymarket token side.

    Every constraint is a required argument. Polymarket publishes ``minimum_tick_size``
    and ``minimum_order_size`` per market and they genuinely differ between markets,
    so there is no honest default to fall back on - a caller that does not know them
    has not read the market yet.
    """
    return VenueContract(
        venue=Platform.POLYMARKET,
        contract_key=token_id,
        market_ref=condition_id,
        outcome=outcome,
        tick_size=tick_size,
        quantity_step=quantity_step,
        min_quantity=min_quantity,
        min_notional_usd=min_notional_usd,
        fee_rate=fee_rate,
        fee_type="quadratic",
        supported_tif=frozenset(
            {
                TimeInForce.GOOD_TILL_CANCELLED,
                TimeInForce.IMMEDIATE_OR_CANCEL,
                TimeInForce.FILL_OR_KILL,
            }
        ),
        constraints_source=source or "polymarket:clob",
    )
