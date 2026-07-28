"""Multi-leg execution: a basket is worth what its scarcest leg can fill."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import Platform, Side
from pmvl_markets.pricing.multileg import (
    LegRequest,
    basket_edge,
    max_executable_units,
    simulate_basket,
)


def leg(book, side=Side.YES, label="leg", ratio="1") -> LegRequest:  # noqa: ANN001
    return LegRequest(label=label, book=book, side=side, ratio=Decimal(ratio))


class TestMaxExecutableUnits:
    def test_scarcest_leg_binds(self, book_factory) -> None:  # noqa: ANN001
        deep = book_factory(yes_asks=[("0.40", "5000")])
        thin = book_factory(yes_asks=[("0.55", "40")])
        units, binding = max_executable_units(
            [leg(deep, label="deep"), leg(thin, label="thin")]
        )
        assert units == Decimal("40")
        assert binding == "thin"

    def test_an_empty_leg_makes_the_basket_unavailable(self, book_factory) -> None:  # noqa: ANN001
        """Not a smaller basket - no basket."""
        units, binding = max_executable_units(
            [
                leg(book_factory(yes_asks=[("0.40", "5000")]), label="ok"),
                leg(book_factory(yes_asks=[]), label="empty"),
            ]
        )
        assert units == 0
        assert binding == "empty"

    def test_ratio_scales_capacity(self, book_factory) -> None:  # noqa: ANN001
        """A leg needing two contracts per unit halves the achievable unit count."""
        book = book_factory(yes_asks=[("0.40", "100")])
        assert max_executable_units([leg(book, ratio="2")])[0] == Decimal("50")

    def test_no_legs(self) -> None:
        assert max_executable_units([]) == (Decimal("0"), None)


class TestBasketSimulation:
    def test_full_fill(self, book_factory) -> None:  # noqa: ANN001
        a = book_factory(yes_asks=[("0.40", "500")])
        b = book_factory(yes_asks=[("0.55", "500")])
        ex = simulate_basket([leg(a, label="a"), leg(b, label="b")], Decimal("100"))
        assert ex.fully_executable
        assert ex.executable_units == Decimal("100")
        assert not ex.unfilled_legs
        assert ex.total_cost == Decimal("95")  # 100*0.40 + 100*0.55
        assert ex.cost_per_unit == Decimal("0.95")

    def test_basket_scales_down_to_the_binding_leg(self, book_factory) -> None:  # noqa: ANN001
        deep = book_factory(yes_asks=[("0.40", "5000")])
        thin = book_factory(yes_asks=[("0.55", "40")])
        ex = simulate_basket(
            [leg(deep, label="deep"), leg(thin, label="thin")], Decimal("1000")
        )
        assert not ex.fully_executable
        assert ex.executable_units == Decimal("40")
        assert ex.binding_leg == "thin"
        # Every leg is sized to the achievable unit count, so nothing is naked.
        assert all(l.filled == Decimal("40") for l in ex.legs)

    def test_average_price_includes_ladder_impact(self, book_factory) -> None:  # noqa: ANN001
        """Legs are never assumed to fill at top of book."""
        book = book_factory(yes_asks=[("0.40", "50"), ("0.50", "50")])
        ex = simulate_basket([leg(book)], Decimal("100"))
        assert ex.legs[0].average_price == Decimal("0.45")
        assert ex.legs[0].average_price > Decimal("0.40")

    def test_missing_leg_makes_the_basket_unavailable(self, book_factory) -> None:  # noqa: ANN001
        """No units at all, so there is nothing hedged and nothing exposed."""
        ex = simulate_basket(
            [
                leg(book_factory(yes_asks=[("0.40", "100")]), label="ok"),
                leg(book_factory(yes_asks=[]), label="gone"),
            ],
            Decimal("10"),
        )
        assert ex.executable_units == 0
        assert not ex.fully_executable
        assert not ex.fully_hedged
        # Zero units is not exposure - no leg was bought.
        assert not ex.naked_exposure
        assert ex.binding_leg == "gone"

    def test_scaled_down_basket_is_hedged_not_naked(self, book_factory) -> None:  # noqa: ANN001
        """Scaling every leg from 100 to 40 leaves a hedged 40, not a naked position.

        The earlier version compared each leg's fill against the ORIGINAL request, so
        a uniformly scaled basket reported every leg as unfilled and the whole thing
        as naked exposure. That is the opposite of what happened.
        """
        deep = book_factory(yes_asks=[("0.40", "5000")])
        thin = book_factory(yes_asks=[("0.55", "40")])
        ex = simulate_basket(
            [leg(deep, label="deep"), leg(thin, label="thin")], Decimal("100")
        )
        assert ex.executable_units == Decimal("40")
        assert ex.scaled_down
        assert ex.fully_hedged
        assert not ex.naked_exposure
        assert not ex.unfilled_legs
        assert ex.shortfall_vs_requested == Decimal("60")
        # Each leg gave up 60 against the request but nothing against the plan.
        for l in ex.legs:
            assert l.shortfall_vs_plan == 0
            assert l.shortfall_vs_requested == Decimal("60")

    def test_zero_units_requested(self, book_factory) -> None:  # noqa: ANN001
        ex = simulate_basket([leg(book_factory(yes_asks=[("0.4", "10")]))], Decimal("0"))
        assert ex.executable_units == 0
        assert not ex.fully_executable

    def test_serialisation_exposes_unfilled_quantity(self, book_factory) -> None:  # noqa: ANN001
        deep = book_factory(yes_asks=[("0.40", "5000")])
        thin = book_factory(yes_asks=[("0.55", "40")])
        payload = simulate_basket(
            [leg(deep, label="deep"), leg(thin, label="thin")], Decimal("100")
        ).as_dict()
        assert payload["binding_leg"] == "thin"
        assert payload["fully_executable"] is False
        assert payload["scaled_down"] is True
        assert payload["fully_hedged"] is True
        assert payload["naked_exposure"] is False
        by_label = {l["label"]: l for l in payload["legs"]}
        # Compare as Decimal: the payload carries the quantized money form.
        assert Decimal(by_label["thin"]["shortfall_vs_requested"]) == Decimal("60")
        assert Decimal(by_label["thin"]["shortfall_vs_plan"]) == 0


class TestBasketEdge:
    def test_profitable_complete_set(self, book_factory) -> None:  # noqa: ANN001
        yes = book_factory(yes_asks=[("0.45", "1000")])
        no = book_factory(no_asks=[("0.50", "1000")])
        ex = simulate_basket(
            [leg(yes, Side.YES, "yes"), leg(no, Side.NO, "no")], Decimal("100")
        )
        result = basket_edge(ex, guaranteed_payout_per_unit=Decimal("1"))
        assert result["total_cost"] == Decimal("95")
        assert result["net_profit"] == Decimal("5")
        assert result["net_edge"] > Decimal("0.05")

    def test_fees_can_erase_the_edge(self, book_factory) -> None:  # noqa: ANN001
        """A 1c gross edge does not survive 2c of fees."""
        yes = book_factory(yes_asks=[("0.495", "1000")])
        no = book_factory(no_asks=[("0.495", "1000")])
        ex = simulate_basket(
            [leg(yes, Side.YES, "yes"), leg(no, Side.NO, "no")], Decimal("100")
        )
        with_fees = basket_edge(
            ex, guaranteed_payout_per_unit=Decimal("1"), fees_per_unit=Decimal("0.02")
        )
        assert with_fees["net_profit"] < 0
        assert with_fees["net_edge"] < 0

    def test_slippage_erases_the_edge(self, book_factory) -> None:  # noqa: ANN001
        """Top of book shows an edge; the size intended walks past it."""
        yes = book_factory(yes_asks=[("0.45", "10"), ("0.60", "1000")])
        no = book_factory(no_asks=[("0.50", "10"), ("0.60", "1000")])
        small = basket_edge(
            simulate_basket([leg(yes, Side.YES, "y"), leg(no, Side.NO, "n")], Decimal("10")),
            guaranteed_payout_per_unit=Decimal("1"),
        )
        large = basket_edge(
            simulate_basket([leg(yes, Side.YES, "y"), leg(no, Side.NO, "n")], Decimal("500")),
            guaranteed_payout_per_unit=Decimal("1"),
        )
        assert small["net_profit"] > 0
        assert large["net_profit"] < 0

    def test_empty_execution_returns_zeros_not_a_division_error(self, book_factory) -> None:  # noqa: ANN001
        ex = simulate_basket([leg(book_factory(yes_asks=[]))], Decimal("100"))
        result = basket_edge(ex, guaranteed_payout_per_unit=Decimal("1"))
        assert result["net_edge"] == 0
        assert result["net_profit"] == 0
