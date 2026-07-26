"""Shared fixtures. No test in this suite requires live network access."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pmvl_shared.config import get_settings, reset_settings_cache
from pmvl_shared.db import reset_engine, session_scope
from pmvl_shared.db.base import Base
from pmvl_shared.enums import Category, MarketStatus, Platform, Side
from pmvl_shared.schemas import BookLevel, NormalizedMarket, OrderBook
from pmvl_shared.timeutil import utcnow

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a recorded provider response.

    These are real responses captured from the venues' production APIs, so the
    provider tests exercise the actual payload shape without the suite depending on
    the network being reachable or the venues being up.
    """
    with (FIXTURE_DIR / name).open() as handle:
        return json.load(handle)


@pytest.fixture(scope="session", autouse=True)
def _test_environment(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point every test at an isolated SQLite file, never the developer's database."""
    import os

    db_path = tmp_path_factory.mktemp("pmvl") / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db_path}"
    os.environ["ALLOW_DEMO_DATA"] = "true"
    os.environ["RESEARCH_ENABLED"] = "false"
    reset_settings_cache()
    engine = reset_engine()

    # Importing the models module is what registers the mappers on Base.metadata.
    # Without it create_all() silently creates nothing and every DB test fails with
    # "no such table".
    from pmvl_shared.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db():  # noqa: ANN201
    """A transactional session that is rolled back after each test."""
    from pmvl_shared.db import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def clean_db(db):  # noqa: ANN001, ANN201
    """A session with every table truncated first."""
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.commit()
    yield db
    db.rollback()


# ------------------------------------------------------------------ builders
@pytest.fixture()
def kalshi_market() -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.KALSHI,
        platform_market_id="KXTEST-26JUL25-T100",
        platform_event_id="KXTEST-26JUL25",
        series_ticker="KXTEST",
        title="Test market above 100?",
        subtitle="100 or above",
        category=Category.CRYPTO,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("0.01"),
        fee_rate=Decimal("0.07"),
        fee_type="quadratic",
        expected_resolution_time=utcnow() + timedelta(hours=6),
        close_time=utcnow() + timedelta(hours=8),
        volume_24h=Decimal("50000"),
    )


@pytest.fixture()
def polymarket_market() -> NormalizedMarket:
    return NormalizedMarket(
        platform=Platform.POLYMARKET,
        platform_market_id="123456",
        title="Test market above 100?",
        category=Category.CRYPTO,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        yes_token_id="111",
        no_token_id="222",
        tick_size=Decimal("0.001"),
        min_order_size=Decimal("5"),
        fee_rate=Decimal("0.07"),
        fee_type="general_fees",
        expected_resolution_time=utcnow() + timedelta(hours=6),
        volume_24h=Decimal("50000"),
    )


def make_book(
    platform: Platform = Platform.KALSHI,
    market_id: str = "KXTEST-26JUL25-T100",
    yes_asks: list[tuple[str, str]] | None = None,
    no_asks: list[tuple[str, str]] | None = None,
    yes_bids: list[tuple[str, str]] | None = None,
    no_bids: list[tuple[str, str]] | None = None,
    observed_at=None,  # noqa: ANN001
) -> OrderBook:
    def levels(raw: list[tuple[str, str]] | None) -> list[BookLevel]:
        return [
            BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (raw or [])
        ]

    return OrderBook(
        platform=platform,
        platform_market_id=market_id,
        observed_at=observed_at or utcnow(),
        yes_asks=levels(yes_asks),
        no_asks=levels(no_asks),
        yes_bids=levels(yes_bids),
        no_bids=levels(no_bids),
    )


@pytest.fixture()
def book_factory():  # noqa: ANN201
    return make_book


@pytest.fixture()
def deep_book() -> OrderBook:
    """A book with several ask levels on both sides, for VWAP and depth tests."""
    return make_book(
        yes_asks=[("0.40", "100"), ("0.42", "200"), ("0.45", "500")],
        no_asks=[("0.58", "150"), ("0.60", "300")],
        yes_bids=[("0.39", "100"), ("0.38", "200")],
        no_bids=[("0.57", "100")],
    )
