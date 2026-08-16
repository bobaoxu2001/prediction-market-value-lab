"""Kalshi settlement parsing: the venue's published value is authoritative.

The get-market API reference (docs.kalshi.com) documents settlement_value_dollars
as "the settlement value of the YES/LONG side of the contract in dollars", and
the current result enum as {yes, no, scalar, ""} - there is no documented "void"
outcome. This provider must therefore grade from the venue's own number and
refuse to fabricate a payout for states the venue does not document.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pmvl_shared.enums import Category, Platform
from pmvl_shared.schemas import NormalizedMarket


def _market() -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id="KXVOIDTEST",
        title="Settlement test market",
        category=Category.OTHER,
        settlement_source="Kalshi",
    )


class _FakeClient:
    def __init__(self, row: dict) -> None:
        self._row = row

    async def get_json(self, url: str, **kwargs):  # noqa: ANN003, ANN202
        return {"market": self._row}

    async def aclose(self) -> None:
        return None


async def _resolve(row: dict):  # noqa: ANN202
    from pmvl_markets.providers.kalshi import KalshiProvider

    provider = KalshiProvider()
    provider._client = _FakeClient(row)  # type: ignore[assignment]  # noqa: SLF001
    try:
        return await provider.get_resolution(_market())
    finally:
        await provider.aclose()


class TestVenueSettlementValue:
    async def test_the_venue_value_wins_over_local_inference(self) -> None:
        info = await _resolve(
            {
                "status": "settled",
                "result": "yes",
                # A yes settlement the venue values at 0.50 (refund-style or
                # scalar-like) must be graded at 0.50, not at the inferred 1.
                "settlement_value_dollars": "0.5000",
                "settled_time": "2026-08-01T12:00:00Z",
            }
        )
        assert info is not None
        assert info.resolved is True
        assert info.yes_payout == Decimal("0.5")

    async def test_a_scalar_settlement_uses_the_venue_value(self) -> None:
        info = await _resolve(
            {
                "status": "settled",
                "result": "scalar",
                "settlement_value_dollars": "0.3333",
            }
        )
        assert info is not None
        assert info.yes_payout == Decimal("0.3333")

    async def test_an_unparseable_venue_value_is_not_guessed(self) -> None:
        info = await _resolve(
            {"status": "settled", "result": "yes", "settlement_value_dollars": "garbage"}
        )
        assert info is None


class TestBinaryFallback:
    async def test_yes_infers_one_without_the_field(self) -> None:
        info = await _resolve({"status": "settled", "result": "yes"})
        assert info is not None
        assert info.yes_payout == Decimal("1")

    async def test_no_infers_zero_without_the_field(self) -> None:
        info = await _resolve({"status": "settled", "result": "no"})
        assert info is not None
        assert info.yes_payout == Decimal("0")


class TestUnknownStatesFailClosed:
    async def test_a_legacy_void_result_is_not_graded_as_a_total_loss(self) -> None:
        """The venue's current docs define no void payout. Paying $0 would grade
        a possible refund as a loss; recording nothing is the honest output."""
        info = await _resolve({"status": "settled", "result": "void"})
        assert info is None

    async def test_an_unknown_result_is_not_graded(self) -> None:
        info = await _resolve({"status": "settled", "result": "sideways"})
        assert info is None

    async def test_an_unsettled_market_is_not_graded(self) -> None:
        info = await _resolve({"status": "open", "result": ""})
        assert info is None
