"""Concurrent ingest must converge, and the persisted rule hash must be
the same construction the provider stamped.

Two writers against one database - the scheduler and a manual 'pmvl pipeline' -
can both miss the same SELECT and race to INSERT the same key. The database's
unique constraints are the arbiter; these tests simulate the missed SELECT
deterministically (the row exists, the session pretends not to see it) and assert
the loser reloads and updates instead of raising or duplicating.

The second half proves the rule-hash round-trip: provider -> persistence -> reload
must not mutate the semantic hash, on both venues' real payload shapes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pmvl_shared.db.base import insert_or_skip
from pmvl_shared.enums import DataProvenance, MarketStatus, Platform, Side
from pmvl_shared.schemas import NormalizedEvent, NormalizedMarket, TradeTick
from pmvl_shared.timeutil import utcnow

from pmvl_markets.db_models import Event, Market, MarketRule, MarketRuleVersion, Trade
from pmvl_markets.ingest.store import store_trades, upsert_events, upsert_markets
from pmvl_markets.normalize.rules import market_rule_inputs, rules_from_inputs

from .conftest import load_fixture


def _market(**overrides) -> NormalizedMarket:  # noqa: ANN003
    base = dict(
        platform=Platform.KALSHI,
        platform_market_id="RACE-1",
        title="Will BTC be above $70,000 on Jul 31, 2026?",
        subtitle="Bitcoin price",
        status=MarketStatus.OPEN,
        provenance=DataProvenance.LIVE,
        settlement_source="CF Benchmarks BRTI",
        settlement_rules_raw="If the BRTI is above 70000 at 5 PM EDT on Jul 31, 2026, Yes.",
        volume_24h=Decimal("1000"),
        quote_observed_at=utcnow(),
        raw={"ticker": "RACE-1"},
    )
    base.update(overrides)
    return NormalizedMarket(**base)


def _event(**overrides) -> NormalizedEvent:  # noqa: ANN003
    base = dict(
        platform=Platform.KALSHI,
        platform_event_id="EVT-1",
        series_ticker="KXRACE",
        title="Bitcoin price event",
        provenance=DataProvenance.LIVE,
        raw={},
    )
    base.update(overrides)
    return NormalizedEvent(**base)


class _MissOnce:
    """Make session.scalar miss one SELECT for a given table, then delegate.

    This is the deterministic stand-in for the race: the row exists in the
    database, but this session's SELECT fails to see it, so the INSERT runs and
    the unique constraint is the only thing that can catch the collision.
    """

    def __init__(self, session: Session, table_fragment: str) -> None:
        self._original = session.scalar
        self._fragment = table_fragment
        self._missed = False

    def __call__(self, stmt, **kwargs):  # noqa: ANN001, ANN003, ANN204
        if not self._missed and isinstance(stmt, Select):
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            if ("FROM " + self._fragment) in compiled:
                self._missed = True
                return None
        return self._original(stmt, **kwargs)


class TestInsertOrSkip:
    def test_a_non_unique_integrity_error_is_not_swallowed(self, db) -> None:  # noqa: ANN001
        """NOT NULL violations must surface; only the declared conflict target
        is absorbed as "another writer beat us"."""
        with pytest.raises(IntegrityError):
            insert_or_skip(
                db,
                Event.__table__,
                {"platform_event_id": "EVT-NONULL"},  # platform omitted
                conflict_cols=["platform", "platform_event_id"],
            )
        db.rollback()

    def test_the_winner_is_reported_by_rowcount(self, clean_db) -> None:  # noqa: ANN001
        values = {
            "platform": "kalshi",
            "platform_event_id": "EVT-RC",
            "created_at": utcnow(),
            "title": "",
            "normalized_title": "",
            "category": "other",
            "negative_risk": False,
            "mutually_exclusive": False,
            "exhaustive": False,
            "outcome_count": 0,
            "provenance": "live",
        }
        assert (
            insert_or_skip(
                clean_db, Event.__table__, dict(values),
                conflict_cols=["platform", "platform_event_id"],
            )
            is True
        )
        clean_db.commit()
        assert (
            insert_or_skip(
                clean_db, Event.__table__, dict(values),
                conflict_cols=["platform", "platform_event_id"],
            )
            is False
        )


class TestEventUpsertRace:
    def test_a_missed_select_updates_the_winner_instead_of_raising(
        self, clean_db  # noqa: ANN001
    ) -> None:
        upsert_events(clean_db, [_event()])

        ev = _event(title="Updated by the loser")
        miss = _MissOnce(clean_db, "events")
        clean_db.scalar = miss  # type: ignore[method-assign]
        try:
            written = upsert_events(clean_db, [ev])
        finally:
            del clean_db.scalar
        clean_db.commit()

        rows = list(clean_db.scalars(select(Event).where(Event.platform_event_id == "EVT-1")))
        assert written == 1
        assert len(rows) == 1, "the race produced a duplicate row"
        assert rows[0].title == "Updated by the loser"


class TestMarketUpsertRace:
    def test_a_missed_select_updates_the_winner_and_keeps_one_rule(
        self, clean_db  # noqa: ANN001
    ) -> None:
        upsert_markets(clean_db, [_market()])

        miss = _MissOnce(clean_db, "markets")
        clean_db.scalar = miss  # type: ignore[method-assign]
        try:
            ids = upsert_markets(clean_db, [_market(title="Retitled by the loser")])
        finally:
            del clean_db.scalar
        clean_db.commit()

        market_id = ids["kalshi:RACE-1"]
        assert clean_db.get(Market, market_id).title == "Retitled by the loser"
        rules = list(
            clean_db.scalars(select(MarketRule).where(MarketRule.market_id == market_id))
        )
        assert len(rules) == 1, "the race produced two rule rows for one market"

    def test_a_missed_rule_select_converges_on_one_rule(
        self, clean_db  # noqa: ANN001
    ) -> None:
        upsert_markets(clean_db, [_market()])
        clean_db.commit()

        miss = _MissOnce(clean_db, "market_rules")
        clean_db.scalar = miss  # type: ignore[method-assign]
        try:
            upsert_markets(clean_db, [_market()])
        finally:
            del clean_db.scalar
        clean_db.commit()

        market_id = clean_db.scalar(
            select(Market.id).where(Market.platform_market_id == "RACE-1")
        )
        rules = list(
            clean_db.scalars(select(MarketRule).where(MarketRule.market_id == market_id))
        )
        assert len(rules) == 1


class TestTradeInsertRace:
    def test_a_duplicate_trade_print_is_skipped_not_duplicated(
        self, clean_db  # noqa: ANN001
    ) -> None:
        ids = upsert_markets(clean_db, [_market()])
        id_map = {"kalshi:RACE-1": ids["kalshi:RACE-1"]}
        trade = TradeTick(
            platform=Platform.KALSHI,
            platform_trade_id="TRD-1",
            platform_market_id="RACE-1",
            traded_at=utcnow(),
            price=Decimal("0.50"),
            size=Decimal("1"),
            taker_side=Side.YES.value,
            provenance=DataProvenance.LIVE,
        )
        assert store_trades(clean_db, [trade], id_map) == 1
        clean_db.commit()

        miss = _MissOnce(clean_db, "trades")
        clean_db.scalar = miss  # type: ignore[method-assign]
        try:
            written = store_trades(clean_db, [trade], id_map)
        finally:
            del clean_db.scalar
        clean_db.commit()

        assert written == 0
        trades = list(
            clean_db.scalars(select(Trade).where(Trade.platform_trade_id == "TRD-1"))
        )
        assert len(trades) == 1


class TestRuleInputVector:
    def test_the_builder_round_trips_through_rules_from_inputs(self) -> None:
        from datetime import datetime, timezone

        kwargs = dict(
            title="Will BTC be above $70,000?",
            subtitle="Bitcoin price",
            description="Settles on the BRTI at 5 PM EDT.",
            settlement_source="CF Benchmarks BRTI",
            cutoff_time=datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc),
            explicit_threshold=Decimal("70000"),
            explicit_comparator="gt",
            has_structured_strike=True,
        )
        from pmvl_markets.normalize.rules import normalize_rules

        direct = normalize_rules(**kwargs)
        replayed = rules_from_inputs(market_rule_inputs(**kwargs))
        assert replayed.resolution_hash == direct.resolution_hash
        assert replayed.comparator == direct.comparator
        assert replayed.threshold == direct.threshold
        assert replayed.cutoff_utc == direct.cutoff_utc


class TestProviderRoundTrip:
    def test_kalshi_round_trip_keeps_the_semantic_hash(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.providers.kalshi import KalshiProvider

        provider = KalshiProvider()
        series = load_fixture("kalshi_series.json")["series"]
        provider._series_cache[series["ticker"]] = series  # noqa: SLF001
        row = load_fixture("kalshi_markets.json")["markets"][0]
        market = provider._normalize_market(row)  # noqa: SLF001

        assert market is not None
        assert market.rule_inputs is not None, "the provider must stamp its rule inputs"
        assert market.resolution_hash

        key = market.platform.value + ":" + market.platform_market_id
        market_id = upsert_markets(clean_db, [market])[key]
        stored = clean_db.get(Market, market_id)
        rule = clean_db.scalar(select(MarketRule).where(MarketRule.market_id == market_id))

        assert stored.resolution_hash == market.resolution_hash
        assert rule.resolution_hash == market.resolution_hash, (
            "the persisted rule hash diverged from the provider's stamp"
        )
        assert rule.resolution_hash, "hash must not be empty"

        # Re-ingesting the same market must keep the same hash (idempotent).
        upsert_markets(clean_db, [market])
        assert clean_db.get(Market, market_id).resolution_hash == market.resolution_hash
        assert (
            clean_db.scalar(
                select(MarketRule).where(MarketRule.market_id == market_id)
            ).resolution_hash
            == market.resolution_hash
        )

    def test_polymarket_round_trip_keeps_the_semantic_hash(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.providers.polymarket import PolymarketProvider

        provider = PolymarketProvider()
        row = load_fixture("polymarket_markets.json")
        markets = row["data"] if isinstance(row, dict) and "data" in row else row
        assert markets, "fixture has no markets"
        market = provider._normalize_market(markets[0])  # noqa: SLF001

        assert market is not None
        assert market.rule_inputs is not None, "the provider must stamp its rule inputs"
        assert market.resolution_hash

        key = market.platform.value + ":" + market.platform_market_id
        market_id = upsert_markets(clean_db, [market])[key]
        stored = clean_db.get(Market, market_id)
        rule = clean_db.scalar(select(MarketRule).where(MarketRule.market_id == market_id))

        assert stored.resolution_hash == market.resolution_hash
        assert rule.resolution_hash == market.resolution_hash, (
            "the persisted rule hash diverged from the provider's stamp"
        )

    def test_a_legacy_market_without_rule_inputs_still_upserts(
        self, clean_db  # noqa: ANN001
    ) -> None:
        """Rows normalised before providers stamped their inputs keep working;
        the market's own hash stays authoritative for matching."""
        legacy = _market(rule_inputs=None, resolution_hash="legacy-hash-123")
        market_id = upsert_markets(clean_db, [legacy])["kalshi:RACE-1"]
        stored = clean_db.get(Market, market_id)
        rule = clean_db.scalar(select(MarketRule).where(MarketRule.market_id == market_id))
        assert stored.resolution_hash == "legacy-hash-123"
        assert rule is not None
        assert rule.resolution_hash, "the fallback must still derive a rule hash"


class TestRuleVersionRace:
    def test_concurrent_version_insert_converges(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.matching.rule_completeness import rule_hash
        from pmvl_markets.matching.rule_history import record_rule_version

        market_id = upsert_markets(clean_db, [_market()])["kalshi:RACE-1"]
        clean_db.commit()

        args = dict(
            market_id=market_id,
            raw_title="Will BTC be above $70,000?",
            raw_rules="Identical wording for both writers.",
        )
        digest = rule_hash(args["raw_rules"])
        record_rule_version(clean_db, **args)
        clean_db.commit()

        miss = _MissOnce(clean_db, "market_rule_versions")
        clean_db.scalar = miss  # type: ignore[method-assign]
        try:
            record_rule_version(clean_db, **args)
        finally:
            del clean_db.scalar
        clean_db.commit()

        versions = list(
            clean_db.scalars(
                select(MarketRuleVersion).where(
                    MarketRuleVersion.market_id == market_id,
                    MarketRuleVersion.rule_hash == digest,
                )
            )
        )
        assert len(versions) == 1, "the race produced two versions of the same wording"
