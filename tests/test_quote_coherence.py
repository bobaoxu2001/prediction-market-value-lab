"""One market must not show three different prices.

A market carries two independent price observations - the venue's market-summary
payload captured at metadata ingest, and the latest order book, refreshed far more
often. They drift. On the shipped snapshot ten markets disagreed by more than a cent,
with the summary two days behind, and the detail page rendered both plus a
model-implied probability derived from a third path.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import DataProvenance, MarketStatus, Platform
from pmvl_shared.timeutil import utcnow

from pmvl_api.quotes import coherent_quote


def _market(db, **overrides):  # noqa: ANN001, ANN003
    from pmvl_markets.db_models import Market

    base = dict(
        platform=Platform.KALSHI.value,
        platform_market_id="QC-1",
        title="Quote coherence fixture",
        status=MarketStatus.OPEN.value,
        provenance=DataProvenance.LIVE.value,
        created_at=utcnow(),
        best_yes_bid=Decimal("0.74"),
        best_yes_ask=Decimal("0.75"),
        spread=Decimal("0.01"),
        quote_observed_at=utcnow() - timedelta(days=2),
    )
    base.update(overrides)
    market = Market(**base)
    db.add(market)
    db.flush()
    return market


def _book(db, market, *, yes_ask="0.65", yes_bid="0.64", observed=None):  # noqa: ANN001
    from pmvl_markets.db_models import OrderbookLevel, OrderbookSnapshot

    snapshot = OrderbookSnapshot(
        market_id=market.id,
        observed_at=observed or utcnow(),
        provenance=DataProvenance.LIVE.value,
    )
    db.add(snapshot)
    db.flush()
    db.add_all(
        [
            OrderbookLevel(
                snapshot_id=snapshot.id, side="yes", is_ask=True,
                price=Decimal(yes_ask), size=Decimal("500"), level_index=0,
            ),
            OrderbookLevel(
                snapshot_id=snapshot.id, side="yes", is_ask=False,
                price=Decimal(yes_bid), size=Decimal("500"), level_index=0,
            ),
        ]
    )
    db.flush()
    return snapshot


class TestCoherentQuote:
    def test_orderbook_wins_over_a_stale_summary(self, clean_db) -> None:  # noqa: ANN001
        """The book is refreshed far more often and is what an order executes against."""
        market = _market(clean_db)
        _book(clean_db, market, yes_ask="0.65")
        quote = coherent_quote(clean_db, market)
        assert quote.source == "orderbook"
        assert quote.best_yes_ask == Decimal("0.65")
        assert quote.summary_ask == Decimal("0.75")

    def test_stale_summary_is_flagged_not_hidden(self, clean_db) -> None:  # noqa: ANN001
        """A drifting summary means metadata ingest has fallen behind - worth knowing."""
        market = _market(clean_db, platform_market_id="QC-2")
        _book(clean_db, market, yes_ask="0.65")
        assert coherent_quote(clean_db, market).summary_disagrees is True

    def test_small_drift_is_not_flagged(self, clean_db) -> None:  # noqa: ANN001
        """A cent between two observations is normal, not staleness."""
        market = _market(clean_db, platform_market_id="QC-3")
        _book(clean_db, market, yes_ask="0.75")
        assert coherent_quote(clean_db, market).summary_disagrees is False

    def test_timestamp_comes_from_the_same_observation(self, clean_db) -> None:  # noqa: ANN001
        """Showing a book price next to the summary's timestamp is its own lie."""
        market = _market(clean_db, platform_market_id="QC-4")
        snapshot = _book(clean_db, market)
        quote = coherent_quote(clean_db, market)
        assert quote.observed_at == snapshot.observed_at
        assert quote.observed_at != market.quote_observed_at

    def test_spread_is_derived_from_the_same_book(self, clean_db) -> None:  # noqa: ANN001
        market = _market(clean_db, platform_market_id="QC-5")
        _book(clean_db, market, yes_ask="0.65", yes_bid="0.60")
        quote = coherent_quote(clean_db, market)
        assert quote.spread == Decimal("0.05")
        # Not the summary's stale 0.01.
        assert quote.spread != market.spread

    def test_falls_back_to_summary_when_no_book(self, clean_db) -> None:  # noqa: ANN001
        market = _market(clean_db, platform_market_id="QC-6")
        quote = coherent_quote(clean_db, market)
        assert quote.source == "venue_summary"
        assert quote.best_yes_ask == Decimal("0.75")

    def test_no_quote_at_all_is_reported_as_none(self, clean_db) -> None:  # noqa: ANN001
        market = _market(
            clean_db, platform_market_id="QC-7",
            best_yes_bid=None, best_yes_ask=None, spread=None,
        )
        assert coherent_quote(clean_db, market).source == "none"

    def test_latest_book_wins_over_an_older_one(self, clean_db) -> None:  # noqa: ANN001
        market = _market(clean_db, platform_market_id="QC-8")
        _book(clean_db, market, yes_ask="0.50", observed=utcnow() - timedelta(hours=6))
        _book(clean_db, market, yes_ask="0.65", observed=utcnow())
        assert coherent_quote(clean_db, market).best_yes_ask == Decimal("0.65")

    def test_payload_names_its_source(self, clean_db) -> None:  # noqa: ANN001
        """A reader must be able to tell where a number came from."""
        market = _market(clean_db, platform_market_id="QC-9")
        _book(clean_db, market)
        payload = coherent_quote(clean_db, market).as_dict()
        assert payload["source"] == "orderbook"
        assert "latest order book" in payload["note"]


class TestSpreadIsAlwaysDerivedFromTheDisplayedPrices:
    """Including on the venue-summary fallback path.

    That path used to copy the venue's own `spread` column. The venue computes it
    from quotes it holds and we may not, so a market with no bid rendered as
    "YES BID -, YES ASK 0.1c, SPREAD 0.1c" - a spread measured from nothing. The
    order-book path already derived it; both paths must agree on the rule.
    """

    def test_missing_bid_yields_no_spread_even_when_the_venue_reports_one(
        self, clean_db  # noqa: ANN001
    ) -> None:
        market = _market(
            clean_db,
            platform_market_id="SUMMARY-NO-BID",
            best_yes_bid=None,
            best_yes_ask=Decimal("0.0010"),
            best_no_bid=Decimal("0.9990"),
            best_no_ask=None,
            spread=Decimal("0.0010"),  # what the venue claims
        )
        quote = coherent_quote(clean_db, market)

        assert quote.source == "venue_summary"
        assert quote.best_yes_bid is None
        assert quote.spread is None, (
            "a spread cannot be shown next to a missing bid: there is nothing to "
            "measure it from"
        )

    def test_present_sides_derive_the_spread_and_ignore_the_venue_column(
        self, clean_db  # noqa: ANN001
    ) -> None:
        market = _market(
            clean_db,
            platform_market_id="SUMMARY-BOTH-SIDES",
            best_yes_bid=Decimal("0.4000"),
            best_yes_ask=Decimal("0.4300"),
            spread=Decimal("0.9900"),  # deliberately wrong
        )
        quote = coherent_quote(clean_db, market)

        assert quote.spread == Decimal("0.0300")
        assert quote.spread == quote.best_yes_ask - quote.best_yes_bid
