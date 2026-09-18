"""A demo market must not reach a matcher or a scanner.

``DataProvenance.DEMO`` means synthetic and illustrative - never a real opportunity.
Until now nothing enforced that on the matching path: ``load_matchable_markets``
deliberately does not require an order book, so the only thing keeping seeded demo
markets out of ``refresh_matches`` was that they happened to have none. That is a fact
about ingest coverage, not a guarantee, and a demo market matched against a live one
persists a ``MarketMatch`` indistinguishable from a real one.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmvl_markets.arbitrage.pipeline import _load_books, load_matchable_markets
from pmvl_shared.db.models import Market, OrderbookSnapshot
from pmvl_shared.enums import Category, DataProvenance, MarketStatus, Platform
from pmvl_shared.timeutil import utcnow


def _market(db, *, platform, market_id, provenance, hours=6):
    row = Market(
        platform=platform.value,
        platform_market_id=market_id,
        title=f"Will the test resolve above 100? ({market_id})",
        category=Category.CRYPTO.value,
        status=MarketStatus.OPEN.value,
        expected_resolution_time=utcnow() + timedelta(hours=hours),
        close_time=utcnow() + timedelta(hours=hours + 2),
        provenance=provenance.value,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        fee_rate=Decimal("0.07"),
        accepting_orders=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def test_a_demo_market_is_excluded_from_the_matching_universe(clean_db):
    live = _market(
        clean_db, platform=Platform.KALSHI, market_id="KXLIVE",
        provenance=DataProvenance.LIVE,
    )
    demo = _market(
        clean_db, platform=Platform.KALSHI, market_id="KXDEMO",
        provenance=DataProvenance.DEMO,
    )
    clean_db.flush()

    ids = {row.id for row, _market in load_matchable_markets(clean_db)}
    assert live.id in ids
    assert demo.id not in ids


def test_a_demo_market_is_excluded_from_the_scanner_even_when_it_has_a_book(clean_db):
    """The exclusion must not depend on demo markets happening to lack a book."""
    demo = _market(
        clean_db, platform=Platform.KALSHI, market_id="KXDEMO2",
        provenance=DataProvenance.DEMO,
    )
    clean_db.add(
        OrderbookSnapshot(
            market_id=demo.id,
            observed_at=utcnow(),
            best_yes_bid=Decimal("0.44"),
            best_yes_ask=Decimal("0.45"),
            best_no_bid=Decimal("0.54"),
            best_no_ask=Decimal("0.55"),
            provenance=DataProvenance.DEMO.value,
            raw_payload={
                "yes_bids": [[0.44, 100]], "yes_asks": [[0.45, 100]],
                "no_bids": [[0.54, 100]], "no_asks": [[0.55, 100]],
            },
        )
    )
    clean_db.flush()

    ids = {row.id for row, _m, _b in _load_books(clean_db)}
    assert demo.id not in ids


def test_a_live_market_is_still_included(clean_db):
    """The filter must exclude demo without quietly excluding everything else."""
    live = _market(
        clean_db, platform=Platform.POLYMARKET, market_id="POLYLIVE",
        provenance=DataProvenance.LIVE,
    )
    clean_db.flush()
    assert live.id in {row.id for row, _m in load_matchable_markets(clean_db)}
