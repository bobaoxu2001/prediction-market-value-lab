"""Withdrawing a claim is as important as making one.

A stored match is a live assertion that two contracts settle the same way. Three
things can falsify it: a venue edits its rules, the parser learns to read a term it
used to miss, or candidate generation stops proposing the pair at all. In every case
the previous verdict must be *overwritten*, because the row is read downstream as
current - the arbitrage scanner sizes trades from it, and the value engine feeds the
other venue's price in as an independent prior.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from pmvl_shared.enums import (
    ArbitrageLabel,
    Category,
    DataProvenance,
    JobStatus,
    MarketStatus,
    Platform,
    RuleCompatibility,
)
from pmvl_shared.schemas import NormalizedMarket
from pmvl_shared.timeutil import utcnow

from pmvl_markets.arbitrage.pipeline import load_matchable_markets, refresh_matches
from pmvl_markets.db_models import ArbitrageOpportunity, JobRun, MarketMatch
from pmvl_markets.matching.equivalence import EquivalenceVerdict

RULES = (
    "This market will resolve to Yes if the CF Benchmarks BRTI closing price of "
    "Bitcoin on 2026-12-31 at 16:00 UTC is above {strike} USD. If the CF Benchmarks "
    "index is cancelled or unavailable, this market will be voided and all stakes "
    "refunded."
)


def market(platform: Platform, market_id: str, strike: str, title: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        platform_market_id=market_id,
        title=title,
        description=RULES.format(strike=strike),
        settlement_rules_raw=RULES.format(strike=strike),
        settlement_source="CF Benchmarks BRTI",
        category=Category.CRYPTO,
        status=MarketStatus.OPEN,
        accepting_orders=True,
        floor_strike=Decimal(strike),
        strike_type="greater",
        expected_resolution_time=utcnow() + timedelta(hours=6),
        close_time=utcnow() + timedelta(hours=6),
        volume_24h=Decimal("500000"),
    )


def pair(strike_b: str = "100000", title_b: str = "Will Bitcoin be above $100,000 on December 31?"):  # noqa: ANN201
    return [
        market(Platform.KALSHI, "KXBTC-T100000", "100000",
               "Will Bitcoin be above $100,000 on December 31?"),
        market(Platform.POLYMARKET, "998877", strike_b, title_b),
    ]


def store(db, markets) -> None:  # noqa: ANN001
    from pmvl_markets.ingest.store import upsert_markets

    upsert_markets(db, markets)
    db.flush()


def run(db):  # noqa: ANN001
    return refresh_matches(db, load_matchable_markets(db))


class TestMatchInvalidation:
    def test_a_matching_pair_is_stored_with_its_verdict(self, clean_db) -> None:  # noqa: ANN001
        store(clean_db, pair())
        stored = run(clean_db)

        assert len(stored) == 1
        row, _verdict, score = stored[0]
        assert row.rule_compatibility == RuleCompatibility.IDENTICAL.value
        assert row.equivalence_verdict == EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT.value
        assert score.verdict.allows_guaranteed_arbitrage
        assert {c["name"] for c in row.equivalence_components} >= {
            "threshold", "cancellation", "data_source"
        }

    def test_a_pair_that_stops_matching_has_its_claim_withdrawn(self, clean_db) -> None:  # noqa: ANN001
        """The defect: the incompatible verdict was skipped before the row was touched,
        so a stale ``identical`` survived and kept backing downstream decisions."""
        store(clean_db, pair())
        assert len(run(clean_db)) == 1

        # The venue edits its rules: same question, different strike.
        store(clean_db, pair(strike_b="120000"))
        clean_db.expire_all()
        assert run(clean_db) == []

        row = clean_db.scalar(select(MarketMatch))
        assert row is not None, "the row must be kept as an auditable withdrawal"
        assert row.rule_compatibility == RuleCompatibility.INCOMPATIBLE.value
        assert row.equivalence_verdict == EquivalenceVerdict.REJECTED.value

    def test_a_withdrawn_match_stops_supplying_an_independent_prior(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.value.pipeline import build_cross_platform_quotes

        store(clean_db, pair())
        stored = run(clean_db)
        ids = [stored[0][0].market_a_id, stored[0][0].market_b_id]
        clean_db.flush()

        store(clean_db, pair(strike_b="120000"))
        clean_db.expire_all()
        run(clean_db)
        clean_db.flush()

        assert build_cross_platform_quotes(clean_db, ids) == {}

    def test_a_pair_no_longer_proposed_is_withdrawn_too(self, clean_db) -> None:  # noqa: ANN001
        """Candidate generation declining to propose a pair is itself a verdict."""
        store(clean_db, pair())
        assert len(run(clean_db)) == 1

        # A wholesale retitling: the pair is no longer a candidate at all, so
        # verification never sees it and cannot demote it.
        store(clean_db, pair(title_b="Will the Chiefs win the Super Bowl?"))
        clean_db.expire_all()
        run(clean_db)

        row = clean_db.scalar(select(MarketMatch))
        assert row.rule_compatibility == RuleCompatibility.INCOMPATIBLE.value
        assert row.equivalence_verdict == EquivalenceVerdict.REJECTED.value
        assert any("no longer proposes" in r for r in (row.mismatch_reasons or []))

    def test_a_non_matching_pair_writes_no_row(self, clean_db) -> None:  # noqa: ANN001
        """Storing a rejection per non-match would write most of the candidate space."""
        store(clean_db, pair(strike_b="120000"))
        assert run(clean_db) == []
        assert clean_db.scalar(select(MarketMatch)) is None


class TestEmptyScanSupersedesItsPredecessor:
    """A successful scan that finds nothing writes no row - and must still be heard."""

    @pytest.fixture()
    def client(self, clean_db):  # noqa: ANN001, ANN201
        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        clean_db.add(
            ArbitrageOpportunity(
                batch_id="oldbatch",
                kind="cross_platform",
                label=ArbitrageLabel.EXECUTABLE.value,
                title="yesterday's arbitrage",
                legs=[],
                max_net_profit=Decimal("500"),
                net_profit_per_set=Decimal("0.10"),
                rule_compatibility=RuleCompatibility.IDENTICAL.value,
                equivalence_verdict=EquivalenceVerdict.VERIFIED_EQUIVALENT_STRICT.value,
                provenance=DataProvenance.LIVE.value,
                created_at=utcnow() - timedelta(days=1),
            )
        )
        clean_db.commit()
        return TestClient(app)

    def test_the_stale_batch_is_visible_until_a_newer_scan_completes(self, client) -> None:  # noqa: ANN001
        body = client.get("/arbitrage?view=actionable").json()
        assert body["batch_id"] == "oldbatch"
        assert len(body["data"]) == 1

    def test_an_empty_successful_scan_clears_it(self, client, clean_db) -> None:  # noqa: ANN001
        clean_db.add(
            JobRun(
                job_name="arbitrage",
                status=JobStatus.SUCCESS.value,
                started_at=utcnow(),
                finished_at=utcnow(),
                details={"batch_id": "newbatch", "opportunities": {}},
            )
        )
        clean_db.commit()

        body = client.get("/arbitrage?view=actionable").json()
        assert body["data"] == []
        assert body["batch_id"] is None
        assert "found nothing" in body["empty_reason"]

    def test_a_failed_scan_does_not_clear_it(self, client, clean_db) -> None:  # noqa: ANN001
        """A scan that crashed established nothing about whether the edge is gone."""
        clean_db.add(
            JobRun(
                job_name="arbitrage",
                status=JobStatus.FAILED.value,
                started_at=utcnow(),
                finished_at=utcnow(),
                details={"batch_id": "newbatch"},
                error="venue timeout",
            )
        )
        clean_db.commit()

        body = client.get("/arbitrage?view=actionable").json()
        assert body["batch_id"] == "oldbatch"

    def test_a_scan_that_found_something_still_serves_its_rows(self, client, clean_db) -> None:  # noqa: ANN001
        clean_db.add(
            JobRun(
                job_name="arbitrage",
                status=JobStatus.SUCCESS.value,
                started_at=utcnow(),
                finished_at=utcnow(),
                details={"batch_id": "oldbatch"},
            )
        )
        clean_db.commit()

        body = client.get("/arbitrage?view=actionable").json()
        assert body["batch_id"] == "oldbatch"
        assert len(body["data"]) == 1
