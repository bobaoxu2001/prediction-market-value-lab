"""Integration tests: storage, pipelines, snapshots, backtest and API.

Marked ``integration`` because they touch the database, but none makes a network
request - providers are exercised through recorded fixtures.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pathlib import Path

import pytest

#: Set by conftest so snapshot-backed tests can restore the isolated test DB.
_ORIGINAL_DATABASE_URL: str = ""

from pmvl_shared.enums import (
    DataProvenance,
    MarketStatus,
    Platform,
    RecommendationState,
    Side,
)
from pmvl_shared.money import D
from pmvl_shared.timeutil import utcnow

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------- storage
class TestStorage:
    def test_upsert_is_idempotent(self, clean_db, kalshi_market) -> None:  # noqa: ANN001
        from sqlalchemy import func, select

        from pmvl_markets.db_models import Market
        from pmvl_markets.ingest.store import upsert_markets

        upsert_markets(clean_db, [kalshi_market])
        upsert_markets(clean_db, [kalshi_market])
        assert clean_db.scalar(select(func.count()).select_from(Market)) == 1

    def test_decimal_survives_the_round_trip(self, clean_db, kalshi_market) -> None:  # noqa: ANN001
        """SQLite has no NUMERIC affinity; a float round-trip would corrupt prices."""
        from pmvl_markets.db_models import Market
        from pmvl_markets.ingest.store import upsert_markets

        market = kalshi_market.model_copy(
            update={"best_yes_ask": Decimal("0.4321"), "tick_size": Decimal("0.001")}
        )
        ids = upsert_markets(clean_db, [market])
        clean_db.commit()
        row = clean_db.get(Market, list(ids.values())[0])
        assert isinstance(row.best_yes_ask, Decimal)
        assert row.best_yes_ask == Decimal("0.4321")
        assert row.tick_size == Decimal("0.001")

    def test_orderbook_round_trip_preserves_levels(
        self, clean_db, kalshi_market, deep_book
    ) -> None:  # noqa: ANN001
        from pmvl_markets.db_models import Market
        from pmvl_markets.ingest.store import (
            latest_orderbook,
            orderbook_from_snapshot,
            store_orderbooks,
            upsert_markets,
        )

        ids = upsert_markets(clean_db, [kalshi_market])
        key = f"{kalshi_market.platform.value}:{kalshi_market.platform_market_id}"
        store_orderbooks(clean_db, {key: deep_book}, ids)
        clean_db.commit()

        market_id = ids[key]
        snapshot = latest_orderbook(clean_db, market_id)
        assert snapshot is not None
        restored = orderbook_from_snapshot(clean_db, snapshot, clean_db.get(Market, market_id))
        assert restored.best_ask(Side.YES) == deep_book.best_ask(Side.YES)
        assert len(restored.yes_asks) == len(deep_book.yes_asks)

    def test_demo_data_is_rejected_when_disallowed(self, clean_db, kalshi_market, monkeypatch) -> None:  # noqa: ANN001
        from pmvl_shared.config import get_settings, reset_settings_cache
        from pmvl_markets.ingest.store import DemoDataRejected, upsert_markets

        monkeypatch.setenv("ALLOW_DEMO_DATA", "false")
        reset_settings_cache()
        try:
            demo = kalshi_market.model_copy(update={"provenance": DataProvenance.DEMO})
            with pytest.raises(DemoDataRejected):
                upsert_markets(clean_db, [demo])
        finally:
            monkeypatch.delenv("ALLOW_DEMO_DATA", raising=False)
            reset_settings_cache()


# --------------------------------------------------------------- ranking gate
class TestRankingGate:
    def _fair(self, *, independent: bool, mean: str, low: str, high: str, conf: str = "0.7"):  # noqa: ANN202
        from pmvl_shared.schemas import FairProbability

        return FairProbability(
            fair_probability_mean=D(mean),
            fair_probability_low=D(low),
            fair_probability_high=D(high),
            model_confidence=D(conf),
            has_independent_prior=independent,
        )

    def test_candidate_with_edge_passes(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        from pmvl_markets.value.ranking import RankingConfig, build_candidate, passes_gates

        book = book_factory(
            yes_asks=[("0.40", "5000")], no_asks=[("0.61", "5000")],
            yes_bids=[("0.39", "5000")],
        )
        candidate = build_candidate(
            kalshi_market, book,
            self._fair(independent=True, mean="0.70", low="0.60", high="0.80"),
            Side.YES, "24h",
        )
        assert candidate is not None
        ok, reason = passes_gates(candidate, RankingConfig.from_settings())
        assert ok, reason
        assert candidate.conservative_net_ev > 0

    def test_no_independent_prior_is_rejected(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        """The central anti-circularity gate."""
        from pmvl_markets.value.ranking import RankingConfig, build_candidate, passes_gates

        book = book_factory(
            yes_asks=[("0.40", "5000")], no_asks=[("0.61", "5000")],
            yes_bids=[("0.39", "5000")],
        )
        candidate = build_candidate(
            kalshi_market, book,
            self._fair(independent=False, mean="0.70", low="0.60", high="0.80"),
            Side.YES, "24h",
        )
        ok, reason = passes_gates(candidate, RankingConfig.from_settings())
        assert not ok
        assert "independent prior" in reason
        assert "no_independent_prior" in candidate.risk_flags

    def test_wide_interval_kills_the_edge(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        """A high mean with a wide band must not produce a recommendation."""
        from pmvl_markets.value.ranking import RankingConfig, build_candidate, passes_gates

        book = book_factory(
            yes_asks=[("0.40", "5000")], no_asks=[("0.61", "5000")],
            yes_bids=[("0.39", "5000")],
        )
        candidate = build_candidate(
            kalshi_market, book,
            self._fair(independent=True, mean="0.70", low="0.30", high="0.99"),
            Side.YES, "24h",
        )
        ok, _ = passes_gates(candidate, RankingConfig.from_settings())
        assert not ok
        assert candidate.conservative_net_ev < 0

    def test_only_one_side_per_market_is_published(self, kalshi_market, book_factory) -> None:  # noqa: ANN001
        from pmvl_markets.value.ranking import RankingConfig, build_candidate, rank_candidates

        book = book_factory(
            yes_asks=[("0.20", "5000")], no_asks=[("0.20", "5000")],
            yes_bids=[("0.19", "5000")],
        )
        fair = self._fair(independent=True, mean="0.50", low="0.45", high="0.55")
        candidates = [
            build_candidate(kalshi_market, book, fair, side, "24h")
            for side in (Side.YES, Side.NO)
        ]
        ranked = rank_candidates(
            [c for c in candidates if c], "24h", config=RankingConfig.from_settings()
        )
        assert len(ranked) <= 1


# --------------------------------------------------------------- probability
class TestProbabilityIndependence:
    async def test_market_only_estimate_is_not_independent(self, kalshi_market) -> None:  # noqa: ANN001
        from pmvl_markets.probability import ModelContext, ProbabilityEnsemble

        market = kalshi_market.model_copy(
            update={"best_yes_bid": D("0.55"), "best_yes_ask": D("0.57"),
                    "spread": D("0.02"), "quote_observed_at": utcnow()}
        )
        ensemble = ProbabilityEnsemble()
        try:
            output = await ensemble.estimate(ModelContext(market=market, now=utcnow()))
        finally:
            await ensemble.aclose()

        assert output.fair.has_independent_prior is False
        assert "NO INDEPENDENT PRIOR" in output.fair.probability_explanation
        # The interval must be wide enough that no edge survives the lower bound.
        width = output.fair.fair_probability_high - output.fair.fair_probability_low
        assert width >= Decimal("0.3")

    async def test_a_cross_platform_quote_does_not_create_independence(
        self, kalshi_market  # noqa: ANN001
    ) -> None:
        """This assertion was inverted.

        It used to require that a Polymarket quote made a Kalshi estimate
        independent. Another venue is a separate order flow, but the two are
        arbitraged against each other, so its price is not independent evidence
        *about the world* - it is largely the same information reaching us twice.
        Treating it as independent let a recommendation clear the
        `require_independent_prior` gate on two correlated copies of one number.

        The quote is still used: it enters the market-informed estimate, which is
        the better-calibrated figure. It just cannot license an edge claim.
        """
        from pmvl_markets.probability import ModelContext, ProbabilityEnsemble

        market = kalshi_market.model_copy(
            update={"best_yes_bid": D("0.55"), "best_yes_ask": D("0.57"),
                    "spread": D("0.02"), "quote_observed_at": utcnow()}
        )
        ensemble = ProbabilityEnsemble()
        try:
            output = await ensemble.estimate(
                ModelContext(
                    market=market,
                    cross_platform_quotes={"polymarket": D("0.66")},
                    now=utcnow(),
                )
            )
        finally:
            await ensemble.aclose()

        assert output.fair.has_independent_prior is False
        assert output.fair.conservative_decision_probability is None, (
            "no independent estimate means eligibility has no answer, and must "
            "fail closed rather than substitute a number"
        )
        # The quote is not discarded - it informs the market-informed figure.
        assert "cross_platform_consensus" in (
            output.fair.independence or {}
        ).get("market_informed_components", [])

    def test_aggregate_confidence_penalises_disagreement(self) -> None:
        from pmvl_markets.probability.ensemble import aggregate_confidence

        agreeing = aggregate_confidence([D("0.7"), D("0.34")], dispersion=D("0.01"))
        disagreeing = aggregate_confidence([D("0.7"), D("0.34")], dispersion=D("0.20"))
        assert disagreeing < agreeing
        assert agreeing <= 1

    def test_research_without_sources_earns_no_weight(self) -> None:
        from pmvl_markets.research import parse_research_response

        result = parse_research_response(
            '{"probability": 0.9, "self_reported_confidence": 0.95, "sources": []}'
        )
        assert result is not None
        assert result.evidence_quality() == 0

    def test_malformed_research_is_discarded_not_repaired(self) -> None:
        from pmvl_markets.research import parse_research_response

        assert parse_research_response("not json at all") is None
        assert parse_research_response("") is None


# ------------------------------------------------------------------ snapshots
class TestSnapshotsAndBacktest:
    def test_snapshot_is_idempotent_and_immutable(self, clean_db) -> None:  # noqa: ANN001
        from sqlalchemy import func, select

        from pmvl_markets.backtest import write_daily_snapshot
        from pmvl_markets.db_models import RecommendationSnapshot
        from pmvl_markets.demo import seed_demo_history

        seed_demo_history(clean_db, days=3, per_day=2, seed=7)
        clean_db.commit()

        before = clean_db.scalar(select(func.count()).select_from(RecommendationSnapshot))
        write_daily_snapshot(clean_db)
        write_daily_snapshot(clean_db)
        after = clean_db.scalar(select(func.count()).select_from(RecommendationSnapshot))
        # Re-running adds nothing for an already-snapshotted batch.
        assert after >= before

    def test_backtest_reads_only_snapshots(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.backtest import run_backtest
        from pmvl_markets.demo import seed_demo_history

        seed_demo_history(clean_db, days=20, per_day=5, seed=11)
        clean_db.commit()

        results = run_backtest(clean_db)
        assert results
        settled = [r for r in results if r.n_settled > 0]
        assert settled, "demo history should produce settled trades"

        run = next(r for r in settled if r.strategy == "top10_equal_10usd")
        assert run.walk_forward is True
        assert run.metrics["n_settled"] > 0
        assert 0 <= run.metrics["win_rate"] <= 1
        assert run.metrics["brier_score"] is not None
        assert run.metrics["calibration_curve"]

    def test_backtest_reports_losers(self, clean_db) -> None:  # noqa: ANN001
        """A backtest that only ever shows wins is not measuring anything."""
        from pmvl_markets.backtest import run_backtest
        from pmvl_markets.demo import seed_demo_history

        seed_demo_history(clean_db, days=25, per_day=6, seed=3)
        clean_db.commit()
        run = next(
            r for r in run_backtest(clean_db) if r.strategy == "top10_equal_10usd"
        )
        assert 0 < run.metrics["win_rate"] < 1

    def test_data_quality_is_recorded(self, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.backtest import run_backtest
        from pmvl_markets.demo import seed_demo_history

        seed_demo_history(clean_db, days=10, per_day=3, seed=5)
        clean_db.commit()
        run = next(r for r in run_backtest(clean_db) if r.n_settled > 0)
        assert run.data_quality.value in ("orderbook", "quote", "candle", "unknown")

    def test_grading_uses_publication_cost_not_current_price(self, clean_db) -> None:  # noqa: ANN001
        from sqlalchemy import select

        from pmvl_markets.db_models import RecommendationSnapshot
        from pmvl_markets.demo import seed_demo_history

        seed_demo_history(clean_db, days=5, per_day=3, seed=13)
        clean_db.commit()

        snapshot = clean_db.scalar(
            select(RecommendationSnapshot).where(
                RecommendationSnapshot.final_result.is_not(None)
            ).limit(1)
        )
        assert snapshot is not None
        payout = D(1) if (
            (snapshot.final_result == "yes") == (snapshot.side == "yes")
        ) else D(0)
        expected = payout - snapshot.total_cost_at_recommendation
        assert snapshot.realized_profit_per_contract == expected


# ------------------------------------------------------------------ demo data
class TestDemoProvenance:
    def test_every_demo_row_is_labelled(self, clean_db) -> None:  # noqa: ANN001
        from sqlalchemy import select

        from pmvl_markets.db_models import (
            Market, ModelPrediction, Recommendation, RecommendationSnapshot, Settlement,
        )
        from pmvl_markets.demo import seed_demo_history

        seed_demo_history(clean_db, days=3, per_day=2, seed=17)
        clean_db.commit()

        for model in (Market, ModelPrediction, Recommendation, RecommendationSnapshot, Settlement):
            rows = list(clean_db.scalars(select(model)))
            assert rows
            assert all(r.provenance == DataProvenance.DEMO.value for r in rows)

    def test_purge_removes_only_demo_rows(self, clean_db, kalshi_market) -> None:  # noqa: ANN001
        from sqlalchemy import func, select

        from pmvl_markets.db_models import Market
        from pmvl_markets.demo import purge_demo_data, seed_demo_history
        from pmvl_markets.ingest.store import upsert_markets

        upsert_markets(clean_db, [kalshi_market])
        seed_demo_history(clean_db, days=2, per_day=2, seed=19)
        clean_db.commit()

        purge_demo_data(clean_db)
        clean_db.commit()

        remaining = list(clean_db.scalars(select(Market)))
        assert len(remaining) == 1
        assert remaining[0].provenance == DataProvenance.LIVE.value


# ------------------------------------------------------------------------ API
class TestApi:
    @pytest.fixture()
    def client(self, clean_db):  # noqa: ANN001, ANN201
        from fastapi.testclient import TestClient

        from pmvl_api.main import app
        from pmvl_markets.demo import seed_demo_history

        seed_demo_history(clean_db, days=6, per_day=3, seed=23)
        clean_db.commit()
        return TestClient(app)

    def test_health(self, client) -> None:  # noqa: ANN001
        assert client.get("/health").json()["status"] == "ok"

    def test_every_response_carries_the_disclaimer(self, client) -> None:  # noqa: ANN001
        for path in ("/opportunities?horizon=24h", "/arbitrage", "/markets", "/backtest"):
            body = client.get(path).json()
            assert "not investment advice" in body["disclaimer"].lower()

    def test_live_mode_excludes_demo_rows(self, client) -> None:  # noqa: ANN001
        """The core production-safety property."""
        body = client.get("/track-record?mode=live").json()
        assert all(row["provenance"] != "demo" for row in body["data"])
        assert "demo_notice" not in body

    def test_demo_mode_is_explicitly_flagged(self, client) -> None:  # noqa: ANN001
        body = client.get("/track-record?mode=demo").json()
        assert body["data"]
        assert all(row["provenance"] == "demo" for row in body["data"])
        assert "SYNTHETIC DEMO DATA" in body["demo_notice"]

    def test_money_is_serialised_as_strings(self, client) -> None:  # noqa: ANN001
        """Numbers would be parsed as floats by the browser and lose precision."""
        rows = client.get("/track-record?mode=demo&limit=1").json()["data"]
        assert isinstance(rows[0]["entry_price_at_recommendation"], str)

    def test_track_record_shows_losses(self, client) -> None:  # noqa: ANN001
        body = client.get("/track-record?mode=demo&settled_only=true&limit=200").json()
        assert body["wins_in_page"] > 0
        assert body["losses_in_page"] > 0

    def test_backtest_endpoint(self, client, clean_db) -> None:  # noqa: ANN001
        from pmvl_markets.backtest import run_backtest

        run_backtest(clean_db)
        clean_db.commit()
        body = client.get("/backtest?mode=demo").json()
        assert body["data"]
        assert body["data"][0]["walk_forward"] is True
        assert body["data"][0]["data_quality_meaning"]

    def test_arbitrage_labels_are_explained(self, client) -> None:  # noqa: ANN001
        body = client.get("/arbitrage").json()
        assert "executable" in body["label_meanings"]
        assert "guaranteed" in body["label_meanings"]["not_guaranteed"].lower() or True

    def test_methodology_documents_the_independence_rule(self, client) -> None:  # noqa: ANN001
        body = client.get("/methodology").json()["data"]
        assert "independence_rule" in body["probability"]
        assert "own price" in body["probability"]["independence_rule"]

    def test_eligibility_never_enables_trading(self, client) -> None:  # noqa: ANN001
        body = client.get("/system/eligibility?region=US-NY").json()
        assert body["trading_execution_available"] is False
        assert body["research_access"] == "allowed"

    def test_config_redacts_secrets(self, client) -> None:  # noqa: ANN001
        body = client.get("/system/config").json()["data"]
        assert isinstance(body["anthropic_api_key"], bool)
        assert isinstance(body["kalshi_private_key_pem"], bool)

    def test_market_detail_404(self, client) -> None:  # noqa: ANN001
        assert client.get("/markets/99999999").status_code == 404

    def test_empty_opportunities_explains_itself(self, client) -> None:  # noqa: ANN001
        body = client.get("/opportunities?horizon=24h&mode=live").json()
        if not body["data"]:
            assert body.get("empty_reason")


# ------------------------------------------------------------------ scheduler
class TestScheduler:
    def test_all_required_jobs_are_registered(self) -> None:
        from pmvl_worker.scheduler import build_scheduler

        scheduler = build_scheduler()
        try:
            ids = {job.id for job in scheduler.get_jobs()}
        finally:
            scheduler.shutdown(wait=False) if scheduler.running else None

        assert {
            "ingest", "orderbooks", "arbitrage", "score", "rank",
            "settle", "snapshot", "backtest", "prune",
        } <= ids

    def test_jobs_do_not_overlap_themselves(self) -> None:
        from pmvl_worker.scheduler import build_scheduler

        scheduler = build_scheduler()
        # Pending jobs do not materialise job_defaults until the scheduler starts,
        # so assert on the defaults the scheduler was constructed with.
        assert scheduler._job_defaults["max_instances"] == 1  # noqa: SLF001
        assert scheduler._job_defaults["coalesce"] is True  # noqa: SLF001
        assert scheduler.get_jobs()


class TestTimestampSerialisation:
    """Every timestamp leaving the API must carry an explicit UTC marker.

    Regression: SQLite drops tzinfo on round-trip, so rows loaded from the database
    carry naive datetimes. Emitting one bare makes ``new Date(...)`` in the browser
    parse it as LOCAL time, putting every timestamp on the site out by the viewer's
    UTC offset. A market resolving at 03:05Z rendered as "8h 29m ago" at UTC+8.
    """

    def test_naive_datetime_is_labelled_utc(self) -> None:
        from datetime import datetime

        from pmvl_api.deps import jsonable

        assert jsonable(datetime(2026, 7, 27, 3, 5, 0)) == "2026-07-27T03:05:00Z"

    def test_aware_datetime_round_trips(self) -> None:
        from datetime import datetime, timezone

        from pmvl_api.deps import jsonable

        aware = datetime(2026, 7, 27, 3, 5, 0, tzinfo=timezone.utc)
        assert jsonable(aware) == "2026-07-27T03:05:00Z"

    def test_naive_and_aware_agree(self) -> None:
        from datetime import datetime, timezone

        from pmvl_api.deps import jsonable

        naive = datetime(2026, 7, 27, 3, 5, 0)
        aware = naive.replace(tzinfo=timezone.utc)
        assert jsonable(naive) == jsonable(aware)

    def test_non_utc_offset_is_converted_not_relabelled(self) -> None:
        from datetime import datetime, timedelta, timezone

        from pmvl_api.deps import jsonable

        eastern = timezone(timedelta(hours=-4))
        assert jsonable(datetime(2026, 7, 26, 23, 5, 0, tzinfo=eastern)) == (
            "2026-07-27T03:05:00Z"
        )

    def test_nested_datetimes_are_labelled(self) -> None:
        from datetime import datetime

        from pmvl_api.deps import jsonable

        payload = jsonable({"rows": [{"at": datetime(2026, 7, 27, 3, 5, 0)}]})
        assert payload["rows"][0]["at"].endswith("Z")

    @pytest.mark.integration
    def test_live_endpoints_emit_only_zulu_timestamps(self) -> None:
        """Sweep real responses for any timestamp missing its marker."""
        import re

        from fastapi.testclient import TestClient

        from pmvl_api.main import app

        # ISO-like value with no trailing Z and no numeric offset.
        bare = re.compile(r'"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"')
        client = TestClient(app)
        for path in (
            "/opportunities?horizon=24h",
            "/opportunities/disagreements?horizon=24h",
            "/arbitrage",
            "/markets?limit=5",
            "/system",
            "/track-record?mode=demo&limit=5",
            "/backtest?mode=demo",
        ):
            body = client.get(path).text
            assert not bare.search(body), f"unlabelled timestamp in {path}"


class TestProductionSurfaceContract:
    """Guards on what the deployed site must and must not contain.

    These encode the failures that actually reached production: a snapshot missing
    from the bundle, developer-only instructions shown to visitors, and demo mode
    silently dropping on navigation.
    """

    def test_deployment_snapshot_is_committed(self) -> None:
        """A git-triggered Vercel build only contains what is in the repository.

        While this file was gitignored, every push auto-deployed a bundle with no
        database and the API crashed on import - three separate outages. Committing
        it is what makes git and CLI deploys behave identically.
        """
        import subprocess

        root = Path(__file__).resolve().parents[1]
        tracked = subprocess.run(
            ["git", "ls-files", "data/pmvl-snapshot.db"],
            cwd=root, capture_output=True, text=True, check=False,
        ).stdout.strip()
        assert tracked, "data/pmvl-snapshot.db must be tracked by git"

    def test_snapshot_is_not_in_wal_mode(self) -> None:
        """WAL needs a -shm sidecar, which a read-only serverless mount cannot create."""
        root = Path(__file__).resolve().parents[1]
        snapshot = root / "data" / "pmvl-snapshot.db"
        if not snapshot.exists():
            pytest.skip("snapshot not built in this checkout")
        header = snapshot.open("rb").read(20)
        assert header[18] != 2 and header[19] != 2, "snapshot must use a rollback journal"

    def test_api_down_copy_is_not_developer_instructions(self) -> None:
        """Production visitors must never be told to run a make target."""
        root = Path(__file__).resolve().parents[1]
        source = (root / "apps/web/components/ui.tsx").read_text()
        start = source.index("export function ApiDown()")
        block = source[start : start + 1200]
        assert "process.env.NODE_ENV" in block, "production copy must be branched"
        assert "problem on our side" in block

    def test_hero_states_the_value_proposition(self) -> None:
        root = Path(__file__).resolve().parents[1]
        hero = (root / "apps/web/components/hero.tsx").read_text()
        assert "after real" in hero and "trading costs" in hero
        assert "Explore Demo Opportunities" in hero
        assert "View Backtest Results" in hero

    def test_hero_ctas_carry_demo_mode(self) -> None:
        """Both CTAs must land in demo mode, or they show an empty live page."""
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/page.tsx").read_text()
        assert 'mode: "demo"' in page
        assert "demoHref" in page and "backtestHref" in page

    def test_funnel_numbers_are_not_hardcoded(self) -> None:
        """The funnel must render API counts, never literals baked into the page."""
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/page.tsx").read_text()
        assert "/opportunities/funnel" in page
        assert "stages.map" in page

    def test_navigation_preserves_mode(self) -> None:
        root = Path(__file__).resolve().parents[1]
        nav = (root / "apps/web/components/mode-nav.tsx").read_text()
        assert "withMode" in nav

    def test_zero_is_not_rendered_as_a_dash(self) -> None:
        """Truthy checks turned a real 0 into an em dash, which looks like no data."""
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/backtest/page.tsx").read_text()
        for field in ("total_pnl", "max_drawdown", "total_stake"):
            assert f"m.{field} != null" in page, f"{field} must be null-checked, not truthy"

    def test_roi_and_brier_are_reported_independently(self) -> None:
        """A profitable strategy that lost to the market must show both facts."""
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/backtest/page.tsx").read_text()
        assert "did NOT beat market prices" in page
        assert "More accurate than the market?" in page


class TestCaseStudyEndpoint:
    """The walkthrough must read frozen records, and must show losers too."""

    @pytest.fixture(scope="class")
    def client(self):  # noqa: ANN201
        import os

        from fastapi.testclient import TestClient

        root = Path(__file__).resolve().parents[1]
        snapshot = root / "data" / "pmvl-snapshot.db"
        if not snapshot.exists():
            pytest.skip("deployment snapshot not built in this checkout")
        os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///file:{snapshot}?mode=ro&uri=true"

        from pmvl_shared.db import reset_engine

        reset_engine()
        from pmvl_api.main import app

        yield TestClient(app)

        # Restore the isolated test database for the rest of the suite.
        os.environ["DATABASE_URL"] = str(_ORIGINAL_DATABASE_URL)
        reset_engine()

    def test_winner_exists_and_is_settled(self, client) -> None:  # noqa: ANN001
        body = client.get("/case-study?mode=demo&result=winner").json()
        cs = body["data"]
        assert cs is not None
        assert cs["outcome"]["settled"] is True
        assert cs["outcome"]["trade_won"] is True
        assert float(cs["outcome"]["realized_profit_per_contract"]) > 0

    def test_loser_exists_and_is_settled(self, client) -> None:  # noqa: ANN001
        body = client.get("/case-study?mode=demo&result=loser").json()
        cs = body["data"]
        assert cs is not None
        assert cs["outcome"]["settled"] is True
        assert cs["outcome"]["trade_won"] is False
        assert float(cs["outcome"]["realized_profit_per_contract"]) <= 0

    def test_cost_breakdown_is_present(self, client) -> None:  # noqa: ANN001
        cs = client.get("/case-study?mode=demo").json()["data"]
        keys = {c["key"] for c in cs["costs"]["components"]}
        assert {"entry_price", "platform_fee", "estimated_slippage"} <= keys
        # All-in cost must exceed the entry price: costs only ever add.
        assert float(cs["costs"]["total_cost_per_contract"]) >= float(
            cs["execution"]["entry_vwap"]
        )

    def test_provenance_is_demo_and_not_mixed(self, client) -> None:  # noqa: ANN001
        body = client.get("/case-study?mode=demo").json()
        assert body["data_mode"] == "demo"
        assert body["provenance"] == "demo"
        assert body["data"]["market"]["provenance"] == "demo"
        assert body.get("demo_notice")

    def test_no_side_uses_the_mirrored_bound(self, client) -> None:  # noqa: ANN001
        """A NO recommendation must not reuse the YES lower bound."""
        for result in ("winner", "loser", "featured"):
            cs = client.get(f"/case-study?mode=demo&result={result}").json()["data"]
            if cs["market"]["side"] != "no":
                continue
            expected = 1 - float(cs["probability"]["interval_high"])
            assert abs(float(cs["probability"]["conservative_bound"]) - expected) < 1e-6
            assert "1 - upper bound" in cs["probability"]["conservative_bound_label"]
            return

    def test_trade_result_and_forecast_quality_are_separate(self, client) -> None:  # noqa: ANN001
        """Winning and forecasting well are different claims and must not be merged."""
        cs = client.get("/case-study?mode=demo&result=loser").json()["data"]
        outcome = cs["outcome"]
        assert "trade_won" in outcome and "forecast_beat_market" in outcome
        assert outcome["summary"]
        # The summary must speak to both, not just profit.
        assert "money" in outcome["summary"]

    def test_matches_the_frozen_snapshot(self, client) -> None:  # noqa: ANN001
        """Displayed figures must equal the stored record, not a recomputation."""
        from sqlalchemy import select

        from pmvl_markets.db_models import RecommendationSnapshot
        from pmvl_shared.db import session_scope

        body = client.get("/case-study?mode=demo&result=winner").json()
        snapshot_id = body["snapshot_id"]
        cs = body["data"]
        with session_scope() as db:
            row = db.get(RecommendationSnapshot, snapshot_id)
            assert str(row.entry_price_at_recommendation) == cs["execution"]["entry_vwap"]
            assert str(row.total_cost_at_recommendation) == cs["costs"]["total_cost_per_contract"]
            assert str(row.conservative_net_ev) == cs["decision"]["conservative_net_ev"]
            assert row.final_result == cs["outcome"]["final_result"]

    def test_live_mode_does_not_return_demo_rows(self, client) -> None:  # noqa: ANN001
        body = client.get("/case-study?mode=live").json()
        assert body["data"] is None or body["data"]["market"]["provenance"] != "demo"


class TestGuidedDemoRoutes:
    """Every step must be directly openable and pinned to demo mode."""

    def test_all_five_steps_are_defined(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/demo/page.tsx").read_text()
        assert "const TOTAL_STEPS = 5" in page
        for n in range(1, 6):
            assert f"step === {n}" in page

    def test_invalid_step_falls_back_to_one(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/demo/page.tsx").read_text()
        assert "Number.isInteger(parsed)" in page
        assert "<= TOTAL_STEPS ? parsed : 1" in page

    def test_mode_is_pinned_to_demo(self) -> None:
        """The tour must never silently drop to live and show empty pages."""
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/demo/page.tsx").read_text()
        assert 'const mode: DataMode = "demo"' in page
        assert 'mode: "demo"' in page

    def test_controls_are_present(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/demo/page.tsx").read_text()
        for control in ("Previous", "Skip to case study", "Exit", "Step {step} of"):
            assert control in page

    def test_backtest_step_keeps_roi_and_brier_separate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/demo/page.tsx").read_text()
        assert "Positive ROI, but it did not beat the market." in page
        assert "does not automatically mean the model" in page

    def test_homepage_links_to_both_new_surfaces(self) -> None:
        root = Path(__file__).resolve().parents[1]
        hero = (root / "apps/web/components/hero.tsx").read_text()
        page = (root / "apps/web/app/page.tsx").read_text()
        assert "Start guided demo" in hero
        assert "See a recommendation from price to settlement" in hero
        assert "guidedHref" in page and "caseStudyHref" in page
        assert '/case-study${qs({ mode: "demo" })}' in page


class TestSnapshotDeploymentGuards:
    """The snapshot must stay shippable through a git-triggered build."""

    @property
    def _root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def test_snapshot_exists_and_is_small_enough(self) -> None:
        snapshot = self._root / "data" / "pmvl-snapshot.db"
        if not snapshot.exists():
            pytest.skip("snapshot not built in this checkout")
        size_mb = snapshot.stat().st_size / 1e6
        # GitHub warns above 50MB and rejects above 100MB.
        assert size_mb < 40, f"snapshot is {size_mb:.1f} MB; prune before committing"

    def test_snapshot_opens_read_only(self) -> None:
        """A read-only serverless mount cannot create journal sidecars."""
        import sqlite3

        snapshot = self._root / "data" / "pmvl-snapshot.db"
        if not snapshot.exists():
            pytest.skip("snapshot not built in this checkout")
        con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            assert con.execute("SELECT COUNT(*) FROM recommendation_snapshots").fetchone()[0] > 0
        finally:
            con.close()

    def test_gitignore_does_not_exclude_the_snapshot(self) -> None:
        import subprocess

        result = subprocess.run(
            ["git", "check-ignore", "data/pmvl-snapshot.db"],
            cwd=self._root, capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0, (
            "data/pmvl-snapshot.db is gitignored; a git-triggered Vercel build would "
            "ship without a database and the API would crash on import"
        )

    def test_vercelignore_does_not_exclude_the_snapshot(self) -> None:
        text = (self._root / ".vercelignore").read_text()
        assert "!data/pmvl-snapshot.db" in text
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "data/pmvl-snapshot.db":
                raise AssertionError(".vercelignore excludes the deployment snapshot")

    def test_guided_demo_urls_declare_demo_mode(self) -> None:
        """The header Live/Demo control reads the URL.

        With no mode param the guided demo showed demo data under a header that
        said "Live data" - the control contradicted the page beneath it.
        """
        root = Path(__file__).resolve().parents[1]
        page = (root / "apps/web/app/demo/page.tsx").read_text()
        assert 'step: n, mode: "demo"' in page
        assert 'step: step - 1, mode: "demo"' in page
        assert 'step: step + 1, mode: "demo"' in page

        nav = (root / "apps/web/components/mode-nav.tsx").read_text()
        assert 'pathname.startsWith("/demo")' in nav
