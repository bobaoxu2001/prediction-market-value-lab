"""Shorting a complete set: buy NO on every outcome.

An event with N mutually exclusive outcomes can be traded either way. Long, exactly
one leg pays and the basket is worth $1. Short, every leg but the winner pays and the
basket is worth $(N-1). The scanner previously priced only the long side, which on
live data is the side where edge does *not* appear: of twelve complete, fully-priced
baskets measured on 14 September 2026, zero were gross-profitable long and one was
gross-profitable short.

The two directions need different guarantees, and getting that backwards is the way
this goes wrong - see :class:`TestExhaustivenessIsLoadBearingOneWayOnly`.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import ArbitrageLabel, Side
from pmvl_shared.timeutil import utcnow

from pmvl_markets.arbitrage.multi_outcome import OutcomeLeg, scan_multi_outcome

from .conftest import make_book


def basket(
    kalshi_market,  # noqa: ANN001
    yes_asks: list[str],
    no_asks: list[str],
    *,
    size: str = "1000",
    accepting: list[bool] | None = None,
) -> list[OutcomeLeg]:
    """One leg per outcome, each with its own two-sided book."""
    legs = []
    for index, (yes, no) in enumerate(zip(yes_asks, no_asks)):
        market = kalshi_market.model_copy(
            update={
                "platform_market_id": f"KXOUT-{index}",
                "accepting_orders": True if accepting is None else accepting[index],
            }
        )
        legs.append(
            OutcomeLeg(
                market=market,
                book=make_book(
                    market_id=f"KXOUT-{index}",
                    yes_asks=[(yes, size)],
                    no_asks=[(no, size)],
                ),
                market_id=index + 1,
            )
        )
    return legs


def scan(legs, **kw):  # noqa: ANN001, ANN201
    defaults = dict(
        event_title="event",
        mutually_exclusive=True,
        exhaustive=True,
        negative_risk=True,
        expected_outcome_count=len(legs),
    )
    defaults.update(kw)
    return scan_multi_outcome(legs, **defaults)


class TestTheShortDirectionIsPriced:
    def test_a_profitable_short_basket_is_found(self, kalshi_market) -> None:  # noqa: ANN001
        """Three outcomes whose NO asks sum to $1.80 against a $2.00 payout.

        The long side of this same book is unprofitable - the YES asks sum to $1.20 -
        so before this direction existed the scanner returned nothing at all.
        """
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(legs)

        assert result is not None
        assert result.cost_breakdown["direction"] == "short"
        assert {leg.side for leg in result.legs} == {Side.NO}
        assert result.net_profit_per_set > 0
        assert result.max_net_profit > 0

    def test_the_payout_is_n_minus_one_not_one(self, kalshi_market) -> None:  # noqa: ANN001
        """Every leg but the winner pays, so a 3-outcome short basket is worth $2."""
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(legs)
        assert result is not None
        assert Decimal(result.cost_breakdown["payout_per_set"]) == Decimal("2")
        # Reported figures are relative to that payout, not to $1.
        assert result.gross_edge_per_set == Decimal("2") - Decimal("1.80")

    def test_the_long_side_is_still_found_when_it_is_the_profitable_one(
        self, kalshi_market  # noqa: ANN001
    ) -> None:
        legs = basket(kalshi_market, ["0.30"] * 3, ["0.80"] * 3)
        result = scan(legs)
        assert result is not None
        assert result.cost_breakdown["direction"] == "long"
        assert {leg.side for leg in result.legs} == {Side.YES}
        assert Decimal(result.cost_breakdown["payout_per_set"]) == Decimal("1")

    def test_a_book_with_no_edge_either_way_returns_nothing(self, kalshi_market) -> None:  # noqa: ANN001
        """Both directions unprofitable is the normal case, and must stay silent."""
        legs = basket(kalshi_market, ["0.34"] * 3, ["0.67"] * 3)
        assert scan(legs) is None

    def test_the_no_side_token_and_outcome_are_carried(self, kalshi_market) -> None:  # noqa: ANN001
        """A leg that buys NO must not report the YES token."""
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(legs)
        assert result is not None
        for leg in result.legs:
            assert leg.side is Side.NO
            assert leg.token_id == legs[0].market.no_token_id


class TestExhaustivenessIsLoadBearingOneWayOnly:
    """The asymmetry that makes this direction worth having.

    Going long, an unlisted outcome winning means every listed leg resolves NO and
    the basket pays nothing - a total loss, which is why exhaustiveness is required.
    Going short, the same event means every leg pays and the basket collects $N
    instead of $(N-1). The missing outcome helps. Requiring exhaustiveness to short
    would reject sound positions for a risk that runs the other way.
    """

    def test_a_non_exhaustive_short_basket_is_not_downgraded_for_exhaustiveness(
        self, kalshi_market  # noqa: ANN001
    ) -> None:
        """It is downgraded - but for void exposure, never for the missing outcome.

        The distinction matters: a missing outcome genuinely helps a short basket, so
        citing it as the risk would be wrong in substance even where the label is
        right in effect.
        """
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(legs, exhaustive=False, negative_risk=False)

        assert result is not None
        assert result.cost_breakdown["direction"] == "short"
        assert not any(
            "does not assert" in f and "exhaustiveness" in f for f in result.risk_flags
        ), result.risk_flags
        assert any("in your favour" in f for f in result.risk_flags)

    def test_a_venue_enforced_short_basket_is_executable(self, kalshi_market) -> None:  # noqa: ANN001
        """Negative-risk events carry a fallback that always declares a winner."""
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(legs, exhaustive=False, negative_risk=True)

        assert result is not None
        assert result.label is ArbitrageLabel.EXECUTABLE

    def test_a_non_exhaustive_long_basket_is_still_downgraded(
        self, kalshi_market  # noqa: ANN001
    ) -> None:
        legs = basket(kalshi_market, ["0.30"] * 3, ["0.80"] * 3)
        result = scan(legs, exhaustive=False, negative_risk=False)

        assert result is not None
        assert result.cost_breakdown["direction"] == "long"
        assert result.label is ArbitrageLabel.NOT_GUARANTEED
        assert any("exhaustiveness" in f for f in result.risk_flags)

    def test_mutual_exclusivity_is_required_in_both_directions(
        self, kalshi_market  # noqa: ANN001
    ) -> None:
        """Two outcomes both resolving YES costs the short basket a paying leg."""
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(legs, mutually_exclusive=False, negative_risk=False)

        assert result is not None
        assert result.label is ArbitrageLabel.NOT_GUARANTEED
        assert any("mutual exclusivity" in f for f in result.risk_flags)
        assert any("collects less than it is priced for" in f for f in result.risk_flags)


class TestTheSameGatesApply:
    def test_a_partial_basket_is_refused(self, kalshi_market) -> None:  # noqa: ANN001
        """Pricing 3 of 5 outcomes is not a complete set in either direction."""
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        assert scan(legs, expected_outcome_count=5) is None

    def test_a_leg_that_rejects_orders_blocks_the_basket(self, kalshi_market) -> None:  # noqa: ANN001
        legs = basket(
            kalshi_market, ["0.40"] * 3, ["0.60"] * 3, accepting=[True, False, True]
        )
        result = scan(legs)
        assert result is not None
        assert result.label is ArbitrageLabel.EXECUTION_RISK
        assert any("not accepting orders" in f for f in result.risk_flags)

    def test_a_stale_book_is_downgraded(self, kalshi_market) -> None:  # noqa: ANN001
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        old = utcnow() - timedelta(hours=4)
        legs[1] = OutcomeLeg(
            market=legs[1].market,
            book=make_book(
                market_id="KXOUT-1",
                yes_asks=[("0.40", "1000")],
                no_asks=[("0.60", "1000")],
                observed_at=old,
            ),
            market_id=2,
        )
        result = scan(legs)
        assert result is not None
        assert result.label is ArbitrageLabel.STALE_QUOTE

    def test_an_unquotable_leg_refuses_the_basket(self, kalshi_market) -> None:  # noqa: ANN001
        """A partition with an unbuyable member cannot be completed either way."""
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        legs[2] = OutcomeLeg(
            market=legs[2].market,
            book=make_book(market_id="KXOUT-2", yes_asks=[("0.40", "1000")], no_asks=[]),
            market_id=3,
        )
        result = scan(legs)
        # The short side cannot be priced; the long side is unprofitable here.
        assert result is None

    def test_size_is_capped_by_the_thinnest_leg(self, kalshi_market) -> None:  # noqa: ANN001
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        legs[1] = OutcomeLeg(
            market=legs[1].market,
            book=make_book(
                market_id="KXOUT-1", yes_asks=[("0.40", "1000")], no_asks=[("0.60", "40")]
            ),
            market_id=2,
        )
        result = scan(legs)
        assert result is not None
        assert result.max_executable_sets <= Decimal("40")
        assert all(leg.size_available <= Decimal("40") for leg in result.legs)


class TestCostsAreChargedTheSameWay:
    def test_a_thin_gross_edge_does_not_survive_fees(self, kalshi_market) -> None:  # noqa: ANN001
        """The live case this was built for.

        A real Polymarket negative-risk event ("Fed Decision in October?") carried a
        +$0.006 per-set gross short edge on 14 September 2026 and lost $4.18 per 100
        sets once Polymarket's fee and the slippage pad were charged. A basket whose
        edge is thinner than its costs must return nothing, not a small number.
        """
        legs = basket(kalshi_market, ["0.335"] * 3, ["0.665"] * 3)
        assert scan(legs) is None


class TestEventVoidIsTheShortBasketsOwnAsymmetry:
    """The risk that runs the OTHER way from exhaustiveness.

    "Mutually exclusive and exhaustive" describes the outcome *space*; it does not
    promise the event resolves at all. If it voids and every leg settles at $0.50
    instead of $0/$1, a short basket collects N/2 against a cost near N-1 - a loss of
    (N-2)/2 per set. The long basket is unharmed, because N/2 exceeds the $1 it paid.

    Missing this is how a short basket gets labelled riskless when it is not.
    """

    def test_a_short_basket_without_an_enforced_partition_is_not_guaranteed(
        self, kalshi_market  # noqa: ANN001
    ) -> None:
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(
            legs, mutually_exclusive=True, exhaustive=True, negative_risk=False
        )

        assert result is not None
        assert result.cost_breakdown["direction"] == "short"
        assert result.label is ArbitrageLabel.NOT_GUARANTEED
        assert any("void" in f for f in result.risk_flags)
        assert any("NOT a guaranteed profit" in f for f in result.risk_flags)

    def test_the_exposure_is_quantified(self, kalshi_market) -> None:  # noqa: ANN001
        """(N-2)/2 per set - a reader should not have to derive it."""
        legs = basket(kalshi_market, ["0.40"] * 5, ["0.71"] * 5)
        result = scan(legs, negative_risk=False)
        assert result is not None
        assert result.cost_breakdown["direction"] == "short"
        # Five outcomes: (5-2)/2 = $1.50 per set.
        assert any("$1.5" in f for f in result.risk_flags), result.risk_flags

    def test_an_enforced_partition_still_states_what_a_void_would_cost(
        self, kalshi_market  # noqa: ANN001
    ) -> None:
        """Executable, but the reader is told what the label depends on."""
        legs = basket(kalshi_market, ["0.40"] * 3, ["0.60"] * 3)
        result = scan(legs, negative_risk=True)

        assert result is not None
        assert result.label is ArbitrageLabel.EXECUTABLE
        assert any("negative-risk construction always declares a winner" in f
                   for f in result.risk_flags)

    def test_two_outcomes_have_no_void_exposure(self, kalshi_market) -> None:  # noqa: ANN001
        """At N=2 a void pays $1.00 against a $1.00 cost - exactly break-even."""
        legs = basket(kalshi_market, ["0.55", "0.55"], ["0.40", "0.40"])
        result = scan(legs, negative_risk=False)

        assert result is not None
        assert result.cost_breakdown["direction"] == "short"
        assert not any("void" in f for f in result.risk_flags)
        assert result.label is ArbitrageLabel.EXECUTABLE
