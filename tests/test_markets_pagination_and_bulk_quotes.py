"""Market list pagination semantics and bounded quote loading.

The horizon filter used to run in Python AFTER offset/limit had already sliced
the stream, while 'total' counted the unfiltered table - the count overstates
and the pages skip relative to what the client believes it is paginating.
Horizon is now a SQL filter, so total/offset/limit all refer to one result set.

Quotes used to be resolved two queries per market over a 400-row window. The
bulk loader keeps the same per-market logic with a bounded query count.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from pmvl_shared.enums import DataProvenance, MarketStatus, Platform
from pmvl_shared.timeutil import utcnow

from pmvl_markets.db_models import Market, OrderbookLevel, OrderbookSnapshot


def _market(db, *, platform_market_id, resolution_offset_hours=None, volume="1000"):  # noqa: ANN001
    base = dict(
        platform=Platform.KALSHI.value,
        platform_market_id=platform_market_id,
        title="Paginate " + platform_market_id,
        status=MarketStatus.OPEN.value,
        provenance=DataProvenance.LIVE.value,
        created_at=utcnow(),
        volume_24h=Decimal(volume),
        best_yes_bid=Decimal("0.74"),
        best_yes_ask=Decimal("0.75"),
        quote_observed_at=utcnow(),
    )
    if resolution_offset_hours is not None:
        base["expected_resolution_time"] = utcnow() + timedelta(hours=resolution_offset_hours)
    market = Market(**base)
    db.add(market)
    db.flush()
    return market


def _book(db, market, *, yes_ask="0.65", yes_bid="0.64"):  # noqa: ANN001
    snapshot = OrderbookSnapshot(
        market_id=market.id,
        observed_at=utcnow(),
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
            OrderbookLevel(
                snapshot_id=snapshot.id, side="no", is_ask=True,
                price=Decimal("0.36"), size=Decimal("500"), level_index=0,
            ),
        ]
    )
    db.flush()


@pytest.fixture()
def client(clean_db):  # noqa: ANN001, ANN201
    from fastapi.testclient import TestClient

    from pmvl_api.main import app

    # Buckets: 24h x3, 7d x2, 30d x2, beyond x1, past x1, unresolved x1.
    for i in range(3):
        _market(clean_db, platform_market_id="H24-" + str(i), resolution_offset_hours=6 + i)
    for i in range(2):
        _market(clean_db, platform_market_id="H7D-" + str(i), resolution_offset_hours=24 + 12 + i)
    for i in range(2):
        _market(clean_db, platform_market_id="H30-" + str(i), resolution_offset_hours=7 * 24 + 24 + i)
    _market(clean_db, platform_market_id="BEYOND", resolution_offset_hours=40 * 24)
    _market(clean_db, platform_market_id="PAST", resolution_offset_hours=-24)
    _market(clean_db, platform_market_id="UNRESOLVED", resolution_offset_hours=None)
    clean_db.commit()
    return TestClient(app)


class TestHorizonPagination:
    def test_total_counts_exactly_the_horizon_bucket(self, client) -> None:  # noqa: ANN001
        body = client.get("/markets?horizon=24h").json()
        assert body["total"] == 3
        assert body["count"] == 3
        assert {m["platform_market_id"] for m in body["data"]} == {"H24-0", "H24-1", "H24-2"}
        assert all(m["horizon"] == "24h" for m in body["data"])

    def test_offset_and_limit_slice_the_same_set_total_counts(self, client) -> None:  # noqa: ANN001
        body = client.get("/markets?horizon=24h&limit=2&offset=1").json()
        assert body["total"] == 3
        assert body["count"] == 2
        ids = [m["platform_market_id"] for m in body["data"]]
        assert set(ids) <= {"H24-0", "H24-1", "H24-2"}

    def test_an_offset_past_the_end_returns_an_empty_page_with_the_true_total(
        self, client  # noqa: ANN001
    ) -> None:
        body = client.get("/markets?horizon=24h&limit=2&offset=9").json()
        assert body["total"] == 3
        assert body["count"] == 0
        assert body["data"] == []

    def test_horizon_buckets_do_not_overlap(self, client) -> None:  # noqa: ANN001
        assert client.get("/markets?horizon=7d").json()["total"] == 2
        assert client.get("/markets?horizon=30d").json()["total"] == 2
        # Past, unresolved and beyond-30d markets never appear in a bucket.
        assert client.get("/markets?horizon=24h").json()["total"] == 3

    def test_the_unfiltered_total_still_counts_everything(self, client) -> None:  # noqa: ANN001
        assert client.get("/markets").json()["total"] == 10


class TestBulkQuoteLoading:
    def test_a_page_of_markets_with_books_resolves_quotes_from_the_book(
        self, clean_db  # noqa: ANN001
    ) -> None:
        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        for i in range(25):
            m = _market(clean_db, platform_market_id="BOOK-" + str(i))
            _book(clean_db, m)
        clean_db.commit()

        statement_counts: list[int] = []

        def before_cursor_execute(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            statement_counts.append(1)

        engine = clean_db.get_bind()
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            body = TestClient(app).get("/markets?limit=25&sort=spread").json()
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)

        # The old path issued two queries per market (~50+ just for quotes on
        # this page); the bulk loader resolves all quotes in a bounded number of
        # statements no matter how many markets the page holds.
        assert len(statement_counts) < 25, (
            "quote resolution scales with the page size; "
            "expected a bounded query count, got " + str(len(statement_counts))
        )
        with_books = [m for m in body["data"] if m["quote_source"] == "orderbook"]
        assert len(with_books) == 25

    def test_bulk_quotes_match_the_single_market_path(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_api.quotes import bulk_coherent_quotes, coherent_quote

        markets = []
        for i in range(5):
            m = _market(clean_db, platform_market_id="BULK-" + str(i))
            _book(clean_db, m)
            markets.append(m)
        clean_db.commit()

        bulk = bulk_coherent_quotes(clean_db, markets, ("live",))
        for m in markets:
            single = coherent_quote(clean_db, m, ("live",))
            assert bulk[m.id] == single
