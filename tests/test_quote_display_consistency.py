"""Sorting, counterpart quotes and depth naming must match what is displayed."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_shared.enums import DataProvenance, MarketStatus, Platform
from pmvl_shared.timeutil import utcnow

from pmvl_api.quotes import coherent_quote


def _market(db, mid: str, **overrides):  # noqa: ANN001, ANN003
    from pmvl_markets.db_models import Market

    base = dict(
        platform=Platform.KALSHI.value, platform_market_id=mid,
        title=f"Market {mid}", status=MarketStatus.OPEN.value,
        provenance=DataProvenance.LIVE.value, created_at=utcnow(),
        volume_24h=Decimal("10000"),
        expected_resolution_time=utcnow() + timedelta(days=1),
        quote_observed_at=utcnow() - timedelta(days=2),
    )
    base.update(overrides)
    m = Market(**base)
    db.add(m); db.flush()
    return m


def _book(db, market, *, yes_ask, yes_bid, size="100", observed=None):  # noqa: ANN001
    from pmvl_markets.db_models import OrderbookLevel, OrderbookSnapshot

    snap = OrderbookSnapshot(
        market_id=market.id, observed_at=observed or utcnow(),
        provenance=DataProvenance.LIVE.value,
    )
    db.add(snap); db.flush()
    db.add_all([
        OrderbookLevel(snapshot_id=snap.id, side="yes", is_ask=True,
                       price=Decimal(yes_ask), size=Decimal(size), level_index=0),
        OrderbookLevel(snapshot_id=snap.id, side="yes", is_ask=False,
                       price=Decimal(yes_bid), size=Decimal(size), level_index=0),
    ])
    db.flush()
    return snap


class TestCounterpartQuotes:
    """A counterpart gets the same treatment as the primary market."""

    def test_stale_counterpart_summary_is_ignored(self, clean_db) -> None:  # noqa: ANN001
        other = _market(clean_db, "CP-1", best_yes_ask=Decimal("0.90"),
                        best_yes_bid=Decimal("0.89"), spread=Decimal("0.01"))
        _book(clean_db, other, yes_ask="0.74", yes_bid="0.73")
        quote = coherent_quote(clean_db, other)
        assert quote.best_yes_ask == Decimal("0.74")
        assert quote.summary_disagrees is True

    def test_counterpart_timestamp_comes_from_its_own_book(self, clean_db) -> None:  # noqa: ANN001
        other = _market(clean_db, "CP-2", best_yes_ask=Decimal("0.90"))
        snap = _book(clean_db, other, yes_ask="0.74", yes_bid="0.73")
        quote = coherent_quote(clean_db, other)
        assert quote.observed_at == snap.observed_at
        assert quote.observed_at != other.quote_observed_at

    def test_counterpart_falls_back_to_summary_without_a_book(self, clean_db) -> None:  # noqa: ANN001
        other = _market(clean_db, "CP-3", best_yes_ask=Decimal("0.55"),
                        best_yes_bid=Decimal("0.54"), spread=Decimal("0.01"))
        quote = coherent_quote(clean_db, other)
        assert quote.source == "venue_summary"
        assert quote.best_yes_ask == Decimal("0.55")

    def test_payload_never_mixes_price_and_timestamp_sources(self, clean_db) -> None:  # noqa: ANN001
        """Every field in the flattened counterpart block from one observation."""
        from pmvl_api.routers.markets import _counterpart_quote_fields

        other = _market(clean_db, "CP-4", best_yes_ask=Decimal("0.90"))
        snap = _book(clean_db, other, yes_ask="0.74", yes_bid="0.70")
        fields = _counterpart_quote_fields(clean_db, other)
        assert fields["other_best_yes_ask"] == Decimal("0.74")
        assert fields["other_spread"] == Decimal("0.04")
        assert fields["other_quote_observed_at"] == snap.observed_at
        assert fields["other_quote_source"] == "orderbook"
        assert fields["other_quote_is_stale_summary"] is True

    def test_missing_counterpart_is_explicit(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_api.routers.markets import _counterpart_quote_fields

        fields = _counterpart_quote_fields(clean_db, None)
        assert fields["other_quote_source"] == "none"
        assert fields["other_best_yes_ask"] is None


class TestSortMatchesDisplay:
    """A ranking must be by the numbers on the page, not by a stale column."""

    def _client(self):  # noqa: ANN202
        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        return TestClient(app)

    def test_spread_order_matches_displayed_spread(self, clean_db) -> None:  # noqa: ANN001
        # Summaries claim the opposite order from the books.
        wide = _market(clean_db, "S-1", spread=Decimal("0.001"), volume_24h=Decimal("9000"))
        tight = _market(clean_db, "S-2", spread=Decimal("0.500"), volume_24h=Decimal("8000"))
        _book(clean_db, wide, yes_ask="0.60", yes_bid="0.40")    # displayed spread 0.20
        _book(clean_db, tight, yes_ask="0.51", yes_bid="0.50")   # displayed spread 0.01
        clean_db.commit()

        body = self._client().get("/markets?sort=spread&limit=10").json()
        spreads = [Decimal(str(r["spread"])) for r in body["data"] if r["spread"] is not None]
        assert spreads == sorted(spreads), f"not ordered by displayed spread: {spreads}"
        # The market whose SUMMARY claimed the tightest spread must not lead.
        assert body["data"][0]["platform_market_id"] == "S-2"

    def test_liquidity_order_matches_displayed_depth(self, clean_db) -> None:  # noqa: ANN001
        thin = _market(clean_db, "L-1", orderbook_depth_usd=Decimal("999999"),
                       volume_24h=Decimal("9000"))
        deep = _market(clean_db, "L-2", orderbook_depth_usd=Decimal("1"),
                       volume_24h=Decimal("8000"))
        _book(clean_db, thin, yes_ask="0.50", yes_bid="0.49", size="10")
        _book(clean_db, deep, yes_ask="0.50", yes_bid="0.49", size="5000")
        clean_db.commit()

        body = self._client().get("/markets?sort=liquidity&limit=10").json()
        depths = [
            Decimal(str(r["yes_ask_depth_usd"]))
            for r in body["data"] if r.get("yes_ask_depth_usd") is not None
        ]
        assert depths == sorted(depths, reverse=True), f"not ordered by depth: {depths}"
        assert body["data"][0]["platform_market_id"] == "L-2"

    def test_sort_note_states_the_candidate_window(self, clean_db) -> None:  # noqa: ANN001
        """Claiming a global ranking after sorting a page would be dishonest."""
        _market(clean_db, "N-1", spread=Decimal("0.01"))
        clean_db.commit()
        body = self._client().get("/markets?sort=spread&limit=5").json()
        assert body["sort"] == "spread"
        assert "not" in (body["sort_note"] or "").lower()

    def test_markets_without_a_quote_sort_last(self, clean_db) -> None:  # noqa: ANN001
        """No spread is not the tightest spread."""
        quoted = _market(clean_db, "Q-1", volume_24h=Decimal("9000"))
        _market(clean_db, "Q-2", volume_24h=Decimal("9999"), spread=None,
                best_yes_ask=None, best_yes_bid=None)
        _book(clean_db, quoted, yes_ask="0.51", yes_bid="0.50")
        clean_db.commit()
        body = self._client().get("/markets?sort=spread&limit=10").json()
        ids = [r["platform_market_id"] for r in body["data"]]
        assert ids.index("Q-1") < ids.index("Q-2")


class TestDepthNaming:
    def test_ask_depth_is_named_explicitly(self, clean_db) -> None:  # noqa: ANN001
        """A generic depth label for two definitions is how rankings disagree."""
        market = _market(clean_db, "D-1")
        _book(clean_db, market, yes_ask="0.50", yes_bid="0.49", size="100")
        clean_db.commit()

        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        row = TestClient(app).get("/markets?limit=50").json()["data"]
        row = next(r for r in row if r["platform_market_id"] == "D-1")
        assert "yes_ask_depth_usd" in row
        assert "no_ask_depth_usd" in row
        assert "total_displayed_depth_usd" in row
        # 0.50 * 100 contracts on the ask side.
        assert Decimal(str(row["yes_ask_depth_usd"])) == Decimal("50")


class TestQuoteSortedPaginationIsBounded:
    """A quote-derived sort ranks a window, and must say so.

    `total` counts the table. The ranking only ever considers the highest-volume
    slice, so reporting `total` alone let a client render "351-400 of 1,388"
    under an ordering that never looked at 988 of those rows, and an offset past
    the window returned an empty page while still claiming more existed.
    """

    @pytest.fixture()
    def client(self, clean_db):  # noqa: ANN001, ANN201
        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        for i in range(12):
            market = _market(clean_db, f"SORTWIN-{i}", volume_24h=Decimal(1000 - i))
            _book(clean_db, market, yes_ask=f"0.{50 + i}", yes_bid=f"0.{40 + i}")
        clean_db.commit()
        return TestClient(app)

    def test_quote_sorted_response_reports_what_was_ranked(self, client) -> None:  # noqa: ANN001
        body = client.get("/markets?sort=spread&limit=5").json()
        assert body["ranked_total"] is not None
        assert body["ranked_total"] <= body["total"]
        assert body["count"] <= body["ranked_total"]

    def test_the_note_states_the_bound_and_the_paging_consequence(self, client) -> None:  # noqa: ANN001
        note = client.get("/markets?sort=spread&limit=5").json()["sort_note"]
        assert "not all" in note
        assert "beyond that window returns nothing" in note

    def test_offset_past_the_window_is_empty_and_still_declares_the_bound(
        self, client  # noqa: ANN001
    ) -> None:
        body = client.get("/markets?sort=spread&limit=5&offset=100000").json()
        assert body["count"] == 0
        # The bound must still be reported, so an empty page is explicable rather
        # than looking like the data disappeared.
        assert body["ranked_total"] is not None

    def test_a_plain_sql_sort_reports_no_ranked_bound(self, client) -> None:  # noqa: ANN001
        """Volume sorts in SQL over the whole table, so `total` is the truth and
        a `ranked_total` would imply a window that does not exist."""
        body = client.get("/markets?sort=volume&limit=5").json()
        assert body["ranked_total"] is None
        assert body["sort_note"] is None
