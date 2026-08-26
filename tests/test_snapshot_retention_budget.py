"""The artefact must be bounded by a decision, not by a coincidence.

The snapshot crossed its size ceiling twice, and both times every job in the
pipeline reported SUCCESS while the artefact silently stopped being publishable.
The second time, roughly 199 consecutive scheduled runs failed on a candidate
that had processed correctly and then measured 43.3 MB against a 40 MiB gate.

The first fix - cascading orphaned ``market_rule_versions`` - was correct and
insufficient, because it removed one table's growth while leaving the artefact's
size a function of things nobody had bounded:

* backtest history, which grows by ten runs every time the pipeline executes,
* resolved markets, which are never removed because they never stop being settled,
* per-market trade and price history, which had no per-market cap at all,
* and the live board, which grows with the venues.

These tests assert the *property* the fix has to have: for a database that keeps
growing along each of those axes, the artefact still comes out under the ceiling,
deterministically, with the audit trail intact - or the build fails loudly. They
are deliberately written against budgets rather than against today's numbers, so
they keep meaning something after the venues double in size.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmvl_shared.snapshot_retention import (
    DEFAULT_RETENTION,
    MARKET_SCOPED_TABLES,
    MAX_SNAPSHOT_BYTES,
    RetentionPolicy,
    SnapshotTooLargeError,
    apply_retention,
)

ROOT = Path(__file__).resolve().parents[1]

#: Roughly the free text a real Kalshi market row carries.
_RULE_TEXT = "Settles to the official published figure. " * 24
_DESCRIPTION = "This market resolves YES if the reported value exceeds. " * 20


def _create_schema(path: Path) -> None:
    from sqlalchemy import create_engine

    from pmvl_shared.db import models  # noqa: F401  (registers the mappers)
    from pmvl_shared.db.base import Base

    engine = create_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()


def _insert(cur: sqlite3.Cursor, table: str, **values: object) -> None:
    """Insert a row, filling whatever else the schema insists on.

    Same device as ``test_snapshot_retention_cascade``: these tables carry dozens
    of irrelevant NOT NULL columns, and deriving the filler from the schema keeps
    the test about retention instead of about column churn.
    """
    now = datetime.now(timezone.utc).isoformat(sep=" ")
    filler: dict[str, object] = {}
    for _, name, column_type, not_null, default, _pk in cur.execute(
        f"PRAGMA table_info({table})"
    ):
        if name in values or not not_null or default is not None or name == "id":
            continue
        filler[name] = now if column_type == "DATETIME" else "0"
    row = {**filler, **values}
    columns = ", ".join(f'"{name}"' for name in row)
    placeholders = ", ".join("?" for _ in row)
    cur.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values())
    )


def _seed(
    path: Path,
    *,
    live_markets: int = 200,
    resolved_markets: int = 200,
    backtest_runs: int = 40,
    trades_per_market: int = 40,
    prices_per_market: int = 30,
    recommended_market_ids: tuple[int, ...] = (1,),
) -> None:
    """A database shaped like the real one, at whatever scale a test needs.

    Every count is a parameter because the thing under test is how the artefact
    behaves *as those counts grow*, and a fixture with one hardcoded size can
    only ever prove the behaviour at that size.
    """
    _create_schema(path)
    con = sqlite3.connect(path)
    cur = con.cursor()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    market_id = 0
    for index in range(live_markets):
        market_id += 1
        _insert(
            cur,
            "markets",
            id=market_id,
            platform="kalshi",
            platform_market_id=f"live-{index}",
            status="open",
            accepting_orders=1,
            best_yes_ask="0.34",
            # Descending so the volume ranking has something to rank, and unique
            # so the ordering is total before the id tiebreak even matters.
            volume_24h=f"{(live_markets - index) * 10}.00",
            description=_DESCRIPTION,
            settlement_rules_raw=_RULE_TEXT,
        )
    for index in range(resolved_markets):
        market_id += 1
        _insert(
            cur,
            "markets",
            id=market_id,
            platform="kalshi",
            platform_market_id=f"settled-{index}",
            status="settled",
            accepting_orders=0,
            description=_DESCRIPTION,
            settlement_rules_raw=_RULE_TEXT,
            actual_settlement_time=(base + timedelta(hours=index)).isoformat(sep=" "),
        )
        _insert(
            cur,
            "settlements",
            market_id=market_id,
            platform="kalshi",
            platform_market_id=f"settled-{index}",
            result="yes",
        )

    for identifier in range(1, market_id + 1):
        _insert(cur, "market_rules", market_id=identifier)
        _insert(
            cur,
            "market_rule_versions",
            market_id=identifier,
            version=1,
            raw_rules=_RULE_TEXT,
            rule_hash=f"hash-{identifier}",
        )
        for n in range(trades_per_market):
            _insert(
                cur,
                "trades",
                market_id=identifier,
                platform="kalshi",
                platform_trade_id=f"t-{identifier}-{n}",
            )
        for _n in range(prices_per_market):
            _insert(cur, "price_snapshots", market_id=identifier)

    # The builder reads a model version out of this table to stamp the manifest.
    _insert(cur, "model_predictions", market_id=1)

    for recommended in recommended_market_ids:
        _insert(cur, "recommendations", market_id=recommended)
        _insert(
            cur,
            "recommendation_snapshots",
            market_id=recommended,
            batch_id="b1",
            snapshot_date="2026-01-01",
            recommendation_created_at=base.isoformat(sep=" "),
            platform="kalshi",
            platform_market_id=f"live-{recommended - 1}",
            horizon="24h",
            rank=1,
            side="yes",
        )

    for run in range(backtest_runs):
        _insert(
            cur,
            "backtest_runs",
            run_id=f"run-{run:04d}",
            strategy="top10_equal_10usd",
            created_at=(base + timedelta(minutes=run)).isoformat(sep=" "),
        )
        for _n in range(30):
            _insert(cur, "backtest_trades", run_id=f"run-{run:04d}", market_id=1)

    con.commit()
    con.close()


def _count(path: Path, table: str) -> int:
    con = sqlite3.connect(path)
    try:
        return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        con.close()


def _market_ids(path: Path, table: str = "markets") -> set[int]:
    column = "id" if table == "markets" else "market_id"
    con = sqlite3.connect(path)
    try:
        return {row[0] for row in con.execute(f'SELECT {column} FROM "{table}"')}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# The bounds themselves
# ---------------------------------------------------------------------------


class TestTheUnboundedGrowthVectors:
    """Each of these had no bound, and each of them is why the ceiling was hit."""

    def test_backtest_history_is_capped_regardless_of_how_often_the_pipeline_ran(
        self, tmp_path: Path
    ) -> None:
        """Ten runs are written per execution and nothing ever removed one.

        On the database that motivated this module they were 150 runs and 35,406
        trades: 12.9 MB of a 52.2 MB build, rising every day the pipeline ran.
        """
        db = tmp_path / "grown.db"
        _seed(db, backtest_runs=400, live_markets=20, resolved_markets=20)
        apply_retention(db)
        assert _count(db, "backtest_runs") == DEFAULT_RETENTION.max_backtest_runs

    def test_no_backtest_trade_outlives_its_run(self, tmp_path: Path) -> None:
        db = tmp_path / "grown.db"
        _seed(db, backtest_runs=400, live_markets=20, resolved_markets=20)
        apply_retention(db)
        con = sqlite3.connect(db)
        try:
            orphans = con.execute(
                "SELECT COUNT(*) FROM backtest_trades WHERE run_id NOT IN "
                "(SELECT run_id FROM backtest_runs)"
            ).fetchone()[0]
        finally:
            con.close()
        assert orphans == 0

    def test_the_newest_runs_are_the_ones_kept(self, tmp_path: Path) -> None:
        """Ordering matters: keeping an arbitrary 30 is not a retention policy."""
        db = tmp_path / "grown.db"
        _seed(db, backtest_runs=100, live_markets=20, resolved_markets=20)
        apply_retention(db)
        con = sqlite3.connect(db)
        try:
            kept = {row[0] for row in con.execute("SELECT run_id FROM backtest_runs")}
        finally:
            con.close()
        assert kept == {f"run-{n:04d}" for n in range(70, 100)}

    def test_per_market_trade_history_is_capped(self, tmp_path: Path) -> None:
        db = tmp_path / "grown.db"
        _seed(db, live_markets=30, resolved_markets=0, trades_per_market=500)
        apply_retention(db)
        con = sqlite3.connect(db)
        try:
            worst = con.execute(
                "SELECT MAX(n) FROM (SELECT COUNT(*) n FROM trades GROUP BY market_id)"
            ).fetchone()[0]
        finally:
            con.close()
        assert worst <= DEFAULT_RETENTION.max_trades_per_market

    def test_per_market_price_history_is_capped(self, tmp_path: Path) -> None:
        db = tmp_path / "grown.db"
        _seed(db, live_markets=30, resolved_markets=0, prices_per_market=400)
        apply_retention(db)
        con = sqlite3.connect(db)
        try:
            worst = con.execute(
                "SELECT MAX(n) FROM "
                "(SELECT COUNT(*) n FROM price_snapshots GROUP BY market_id)"
            ).fetchone()[0]
        finally:
            con.close()
        assert worst <= DEFAULT_RETENTION.max_price_snapshots_per_market

    def test_resolved_markets_lose_their_free_text_but_not_their_rows(
        self, tmp_path: Path
    ) -> None:
        """The row is outcome history; the prose is 1.1 kB nobody opens.

        Deleting the row would be the cheap version of this and would throw away
        the only record that a settled contract ever existed - which is exactly
        the data a calibration study reads.
        """
        db = tmp_path / "grown.db"
        _seed(db, live_markets=5, resolved_markets=50, recommended_market_ids=())
        before = _count(db, "markets")
        apply_retention(db)
        con = sqlite3.connect(db)
        try:
            assert _count(db, "markets") == before
            texted = con.execute(
                "SELECT COUNT(*) FROM markets WHERE status = 'settled' "
                "AND COALESCE(settlement_rules_raw, '') != ''"
            ).fetchone()[0]
            assert texted == 0
            assert _count(db, "settlements") == 50
        finally:
            con.close()

    def test_a_recommended_resolved_market_keeps_its_text(
        self, tmp_path: Path
    ) -> None:
        """The case study renders that text beside the call published against it."""
        db = tmp_path / "grown.db"
        # Market 6 is the first resolved one when there are five live markets.
        _seed(db, live_markets=5, resolved_markets=50, recommended_market_ids=(6,))
        apply_retention(db)
        con = sqlite3.connect(db)
        try:
            kept = con.execute(
                "SELECT settlement_rules_raw FROM markets WHERE id = 6"
            ).fetchone()[0]
        finally:
            con.close()
        assert kept == _RULE_TEXT


# ---------------------------------------------------------------------------
# The byte budget
# ---------------------------------------------------------------------------


def _tiny_budget(target: int, ceiling: int) -> RetentionPolicy:
    """The real policy with a small byte budget, so the ladder engages fast.

    Building a genuinely 40 MiB fixture would take minutes per test and prove
    nothing extra: the ladder's behaviour is a function of the ratio between the
    artefact and the budget, not of the absolute number.
    """
    return RetentionPolicy(target_bytes=target, max_bytes=ceiling)


class TestTheArtefactFitsItsBudget:
    @pytest.mark.parametrize(
        "live,resolved",
        [(400, 400), (1200, 1200), (3000, 3000)],
        ids=["moderate", "past-the-former-failure", "well-past"],
    )
    def test_growth_along_every_axis_still_fits(
        self, tmp_path: Path, live: int, resolved: int
    ) -> None:
        """The regression. Each of these blew the ceiling under the old builder.

        The old rule was a *filter* - keep markets that traded today - so the
        artefact's size was whatever the venues happened to list. Doubling the
        board doubled the artefact. Here the budget is the thing that decides.
        """
        db = tmp_path / "grown.db"
        _seed(db, live_markets=live, resolved_markets=resolved, backtest_runs=200)
        policy = _tiny_budget(target=8_000_000, ceiling=12_000_000)
        outcome = apply_retention(db, policy=policy)
        assert db.stat().st_size <= policy.max_bytes
        assert outcome.bytes_after == db.stat().st_size

    def test_the_ladder_engages_only_when_it_has_to(self, tmp_path: Path) -> None:
        """Dropping markets is the last resort, not the first.

        On the real database the three cheap steps reclaimed 22 MB and no market
        was dropped at all. A policy that starts by deleting coverage would have
        thrown away the browsable universe for nothing.
        """
        db = tmp_path / "small.db"
        _seed(db, live_markets=20, resolved_markets=20, backtest_runs=60)
        outcome = apply_retention(db, policy=_tiny_budget(target=10**9, ceiling=10**9))
        assert outcome.ladder_engaged is False
        assert outcome.markets_after == outcome.markets_before

    def test_the_ladder_reports_which_rung_it_used(self, tmp_path: Path) -> None:
        db = tmp_path / "grown.db"
        _seed(db, live_markets=2000, resolved_markets=2000)
        outcome = apply_retention(db, policy=_tiny_budget(8_000_000, 12_000_000))
        assert outcome.ladder_engaged is True
        rung = DEFAULT_RETENTION.market_ladder[outcome.ladder_rung_applied]
        recorded = outcome.as_dict()
        assert recorded["live_market_budget"] == rung[0]
        assert recorded["resolved_market_budget"] == rung[1]

    def test_it_fails_closed_rather_than_shipping_an_oversized_artefact(
        self, tmp_path: Path
    ) -> None:
        """The ceiling is a deployment constraint, not a preference.

        An artefact that does not fit the function bundle is not a validation
        problem to be relaxed; raising the number ships something that cannot
        run. So the tightest rung failing is an error, never a warning.
        """
        db = tmp_path / "grown.db"
        _seed(db, live_markets=600, resolved_markets=600)
        with pytest.raises(SnapshotTooLargeError) as caught:
            apply_retention(db, policy=_tiny_budget(target=1_000, ceiling=2_000))
        assert "ceiling" in str(caught.value)

    def test_the_ceiling_is_declared_in_exactly_one_place(self) -> None:
        """The validator's gate and the builder's budget must be one number.

        They were two: a literal in the validator, and nothing at all in the
        builder. That is how a build can be "fixed" and start failing the gate
        again the next time somebody adjusts one of them.
        """
        import scripts.validate_snapshot as validator

        assert validator.MAX_BYTES is MAX_SNAPSHOT_BYTES
        source = (ROOT / "scripts" / "validate_snapshot.py").read_text()
        assert "40 * 1024 * 1024" not in source


# ---------------------------------------------------------------------------
# What may never be traded away
# ---------------------------------------------------------------------------


class TestTheAuditTrailIsNotNegotiable:
    def test_a_recommended_market_survives_the_tightest_budget(
        self, tmp_path: Path
    ) -> None:
        """Guardrail 14, as a test.

        Shrinking the artefact by dropping the market a published call was made
        against would make the track record shorter, which is the one trade this
        repository exists to refuse.
        """
        db = tmp_path / "grown.db"
        # Market 1 is the least-traded live market under the volume ranking used
        # by the ladder, so nothing but the audit exemption can save it.
        _seed(
            db,
            live_markets=800,
            resolved_markets=800,
            recommended_market_ids=(800,),
        )
        con = sqlite3.connect(db)
        con.execute("UPDATE markets SET volume_24h = '0.00' WHERE id = 800")
        con.commit()
        con.close()
        apply_retention(db, policy=_tiny_budget(target=6_000_000, ceiling=12_000_000))
        assert 800 in _market_ids(db)

    def test_recommendation_snapshots_are_never_pruned(self, tmp_path: Path) -> None:
        db = tmp_path / "grown.db"
        _seed(db, live_markets=800, resolved_markets=800)
        before = _count(db, "recommendation_snapshots")
        apply_retention(db, policy=_tiny_budget(target=6_000_000, ceiling=12_000_000))
        assert _count(db, "recommendation_snapshots") == before

    def test_a_settlement_survives_its_market_being_budgeted_out(
        self, tmp_path: Path
    ) -> None:
        """Resolution history is not reconstructible; the market row is.

        ``settlements.market_id`` is a nullable foreign key carrying
        ``(platform, platform_market_id)`` beside it, so the row stays
        identifiable after the market it pointed at is gone.
        """
        db = tmp_path / "grown.db"
        _seed(db, live_markets=100, resolved_markets=900, recommended_market_ids=())
        before = _count(db, "settlements")
        apply_retention(db, policy=_tiny_budget(target=3_600_000, ceiling=12_000_000))
        con = sqlite3.connect(db)
        try:
            assert _count(db, "settlements") == before
            dangling = con.execute(
                "SELECT COUNT(*) FROM settlements WHERE market_id IS NOT NULL "
                "AND market_id NOT IN (SELECT id FROM markets)"
            ).fetchone()[0]
        finally:
            con.close()
        assert dangling == 0


class TestTheCascadeCoversEveryMarketScopedTable:
    @pytest.mark.parametrize("table", MARKET_SCOPED_TABLES)
    def test_no_child_row_outlives_its_market(
        self, tmp_path: Path, table: str
    ) -> None:
        """A table missing from the cascade is an unbounded leak.

        The pruned market never comes back, so nothing ever deletes its rows and
        every run adds another generation of orphans. That is exactly how
        ``market_rule_versions`` grew to 9.75 MB of a 10.15 MB table.
        """
        db = tmp_path / "grown.db"
        _seed(db, live_markets=600, resolved_markets=600)
        apply_retention(db, policy=_tiny_budget(target=6_000_000, ceiling=12_000_000))
        survivors = _market_ids(db)
        assert not (_market_ids(db, table) - survivors)

    def test_no_orderbook_level_outlives_its_snapshot(self, tmp_path: Path) -> None:
        db = tmp_path / "grown.db"
        _seed(db, live_markets=400, resolved_markets=400)
        con = sqlite3.connect(db)
        cur = con.cursor()
        _insert(cur, "orderbook_snapshots", id=1, market_id=1)
        _insert(cur, "orderbook_snapshots", id=2, market_id=2)
        for snapshot_id in (1, 2):
            _insert(cur, "orderbook_levels", snapshot_id=snapshot_id, side="yes")
        con.commit()
        con.close()

        apply_retention(db, policy=_tiny_budget(target=5_000_000, ceiling=12_000_000))
        con = sqlite3.connect(db)
        try:
            orphans = con.execute(
                "SELECT COUNT(*) FROM orderbook_levels WHERE snapshot_id NOT IN "
                "(SELECT id FROM orderbook_snapshots)"
            ).fetchone()[0]
        finally:
            con.close()
        assert orphans == 0


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


class TestRetentionIsDeterministic:
    def test_identical_inputs_produce_identical_bytes(self, tmp_path: Path) -> None:
        """The manifest's content hash only means something if this holds.

        Every ordering that picks survivors ends in a unique ``id`` tiebreak and
        every budget is a fixed integer, so there is nothing left for SQLite to
        decide arbitrarily.
        """
        source = tmp_path / "source.db"
        _seed(source, live_markets=300, resolved_markets=300, backtest_runs=80)

        # Copied rather than seeded twice. The seeder stamps NOT NULL DATETIME
        # columns with the wall clock, so two independent seeds are two different
        # databases and comparing their outputs would test the fixture, not the
        # policy. The claim under test is: same input bytes, same output bytes.
        digests = []
        for name in ("a.db", "b.db"):
            db = tmp_path / name
            db.write_bytes(source.read_bytes())
            apply_retention(
                db, policy=_tiny_budget(target=6_000_000, ceiling=12_000_000)
            )
            digests.append(hashlib.sha256(db.read_bytes()).hexdigest())
        assert digests[0] == digests[1]

    def test_tightening_matches_a_direct_build_at_the_same_rung(
        self, tmp_path: Path
    ) -> None:
        """Walking down the ladder must not differ from starting at the rung.

        The loop keeps deleting from the same database rather than rebuilding,
        which is only valid because each rung's survivor set is a subset of the
        one before it. If that ever stopped holding, this is where it shows.
        """
        walked = tmp_path / "walked.db"
        direct = tmp_path / "direct.db"
        for db in (walked, direct):
            _seed(db, live_markets=900, resolved_markets=900, backtest_runs=60)

        outcome = apply_retention(
            walked, policy=_tiny_budget(target=7_000_000, ceiling=12_000_000)
        )
        assert outcome.ladder_engaged
        rung = DEFAULT_RETENTION.market_ladder[outcome.ladder_rung_applied]

        apply_retention(
            direct,
            policy=RetentionPolicy(
                target_bytes=7_000_000, max_bytes=12_000_000, market_ladder=(rung,)
            ),
        )
        assert _market_ids(walked) == _market_ids(direct)


class TestTheDecisionIsRecorded:
    def test_the_outcome_serialises_every_budget_and_step(
        self, tmp_path: Path
    ) -> None:
        """"Why is this market not in the snapshot?" needs an answer in the file.

        Without it the only way to tell "never renderable" from "lost a size
        budget" is to re-run the build against a database that no longer exists.
        """
        db = tmp_path / "grown.db"
        _seed(db, live_markets=200, resolved_markets=200, backtest_runs=80)
        recorded = apply_retention(
            db, policy=_tiny_budget(target=5_000_000, ceiling=12_000_000)
        ).as_dict()

        assert recorded["policy"]["max_bytes"] == 12_000_000
        assert recorded["bytes_after"] == db.stat().st_size
        assert [step["name"] for step in recorded["steps"]][:3] == [
            "backtest_history",
            "resolved_market_text",
            "per_market_history",
        ]

    def test_each_step_reports_the_size_after_that_step(
        self, tmp_path: Path
    ) -> None:
        """Not the final size three times.

        Building the step list eagerly runs every delete before anything is
        measured, and the per-step numbers then look like evidence while saying
        nothing. They are the diagnostic the failure message points a reader at.
        """
        db = tmp_path / "grown.db"
        _seed(db, live_markets=200, resolved_markets=200, backtest_runs=300)
        outcome = apply_retention(db, policy=_tiny_budget(10**9, 10**9))
        sizes = [step.bytes_after for step in outcome.steps]
        assert sizes == sorted(sizes, reverse=True)
        assert len(set(sizes)) > 1
