"""The Founding Pilot: freshness gate, candidate economics, watchlist, renderers.

Every test here uses a deterministic clock and a temporary database built in
`conftest`-free local fixtures. Nothing touches the network, and nothing depends
on the committed Snapshot being any particular age - a suite that starts failing
because real time passed is a suite that gets disabled.

The committed Snapshot is used in exactly one place, `TestCommittedSnapshot`,
where the point *is* the real artefact: it must be blocked for current research
on a date after its cutoff, and it must be usable as a historical sample.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from pmvl_markets.pilot import digest as digest_mod
from pmvl_markets.pilot.digest import (
    MAX_CANDIDATES,
    Candidate,
    DigestReport,
    WatchlistEntry,
    build_daily_digest,
    select_candidates,
)
from pmvl_markets.pilot.gate import DEFAULT_SLA, GateFailure, PilotSLA, evaluate
from pmvl_markets.pilot.render import (
    HISTORICAL_SUBWARNING,
    to_html_email,
    to_markdown,
    to_text,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_MANIFEST = ROOT / "data" / "pmvl-snapshot.manifest.json"

def _committed_cutoff() -> datetime:
    """The committed Snapshot's own cutoff, read from the artefact.

    Previously a literal 2026-07-31, paired with a literal 2026-08-03 chosen to
    sit three days after it. That encoded the *identity* of one published
    artefact into a test whose subject is the freshness gate, so the first
    successful publication after it - 45835c8, which replaced the July artefact
    with one minutes old - turned a passing suite red without anything being
    wrong. The gate was correct and the artefact was correct; only the literal
    was stale.

    Reading the cutoff from the manifest keeps the assertion the module docstring
    promises: the committed Snapshot must be refused as *current* research once
    it is old enough, whichever Snapshot happens to be committed.
    """
    payload = json.loads(REAL_MANIFEST.read_text())
    return datetime.fromisoformat(
        str(payload["freshest_quote_observed_at"]).replace("Z", "+00:00")
    )


#: The committed Snapshot's cutoff, and an instant far enough after it to be
#: stale under every SLA. The whole point of the gate is that these are far
#: apart, so the distance is asserted rather than assumed.
SNAPSHOT_CUTOFF = _committed_cutoff()
WELL_AFTER_CUTOFF = SNAPSHOT_CUTOFF + timedelta(days=3)

#: A fixed instant, used only where no committed artefact is involved.
AUGUST_3 = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

ALL_JOBS = {
    "arbitrage": "success",
    "backtest": "success",
    "ingest": "success",
    "orderbooks": "success",
    "prune": "success",
    "rank": "success",
    "score": "success",
    "settle": "success",
    "snapshot": "success",
}


# --------------------------------------------------------------- fixtures --


def _make_db(path: Path) -> None:
    """A minimal Snapshot database with the tables the digest reads."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table markets (
            id integer primary key, platform text, platform_market_id text,
            title text, subtitle text, description text, category text,
            status text, accepting_orders integer, strike_type text,
            floor_strike text, cap_strike text, tick_size text,
            min_order_size text, fee_rate text, maker_fee_rate text,
            fee_type text, close_time text, event_occurrence_time text,
            expected_resolution_time text, settlement_source text,
            settlement_rules_raw text, volume_24h text, provenance text
        );
        create table orderbook_snapshots (
            id integer primary key, market_id integer, observed_at text,
            source_timestamp text, provenance text
        );
        create table orderbook_levels (
            id integer primary key, snapshot_id integer, side text,
            is_ask integer, price text, size text, level_index integer
        );
        create table model_predictions (
            id integer primary key, market_id integer, model_version text,
            fair_probability_mean text, fair_probability_low text,
            fair_probability_high text, model_confidence text,
            data_freshness_seconds integer, has_independent_prior integer,
            market_implied_probability text, explanation text, category text,
            provenance text, created_at text, market_informed_probability text,
            independent_probability text, independent_probability_low text,
            independent_probability_high text,
            conservative_decision_probability text
        );
        create table market_rules (
            id integer primary key, market_id integer,
            settlement_source_name text, settlement_source_url text,
            threshold_semantics text, comparator text, cutoff_time text,
            cutoff_timezone text, includes_overtime integer,
            uses_revised_data integer
        );
        create table settlements (
            id integer primary key, market_id integer, result text,
            settled_at text, provenance text
        );
        create table recommendation_snapshots (
            id integer primary key, provenance text,
            recommendation_created_at text, settled_at text, market_title text,
            platform text, side text, final_result text,
            total_cost_at_recommendation text,
            realized_profit_per_contract text
        );
        """
    )
    conn.commit()
    conn.close()


def _add_market(
    conn: sqlite3.Connection,
    market_id: int,
    *,
    observed_at: datetime,
    resolution: datetime,
    ask: str = "0.20",
    ask_size: str = "5000",
    fair_low: str = "0.60",
    fair_mean: str = "0.70",
    fair_high: str = "0.80",
    independent: int = 1,
    fee_rate: str | None = "0.0",
    with_rule: bool = True,
    confidence: str = "0.8",
) -> None:
    """One market rigged so YES at ``ask`` is comfortably profitable by default."""
    conn.execute(
        "insert into markets values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            market_id, "kalshi", f"MKT-{market_id}", f"Market {market_id}", "", "",
            "other", "open", 1, "greater_or_equal", "100", None, "0.01", "1",
            fee_rate, "0.0", "quadratic", None, None, resolution.isoformat(sep=" "),
            "Test source", "rules", "1000", "live",
        ),
    )
    conn.execute(
        "insert into orderbook_snapshots values (?,?,?,?,?)",
        (market_id, market_id, observed_at.isoformat(sep=" "), None, "live"),
    )
    conn.execute(
        "insert into orderbook_levels values (?,?,?,?,?,?,?)",
        (market_id * 10, market_id, "yes", 1, ask, ask_size, 0),
    )
    conn.execute(
        "insert into orderbook_levels values (?,?,?,?,?,?,?)",
        (market_id * 10 + 1, market_id, "no", 1, "0.95", ask_size, 0),
    )
    conn.execute(
        "insert into model_predictions values "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            market_id, market_id, "test-v1", fair_mean, fair_low, fair_high,
            confidence, 60, independent, "0.20", "why", "other", "live",
            observed_at.isoformat(sep=" "), fair_mean, fair_mean, fair_low,
            fair_high, fair_low,
        ),
    )
    if with_rule:
        conn.execute(
            "insert into market_rules values (?,?,?,?,?,?,?,?,?,?)",
            (
                market_id, market_id, "Test source", "https://example.test",
                "closing_value", "greater_or_equal", None, "UTC", 1, 0,
            ),
        )


@pytest.fixture()
def snapshot(tmp_path: Path):
    """A fresh, valid Snapshot plus manifest, with a deterministic cutoff."""
    cutoff = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    data = tmp_path / "data"
    data.mkdir()
    db = data / "pmvl-snapshot.db"
    _make_db(db)

    conn = sqlite3.connect(db)
    _add_market(conn, 1, observed_at=cutoff, resolution=cutoff + timedelta(days=3))
    conn.commit()
    conn.close()

    manifest_path = data / "pmvl-snapshot.manifest.json"

    def write_manifest(**overrides) -> Path:
        payload = {
            "snapshot_id": "test-snapshot",
            "code_commit_sha": "abc123",
            "model_version": "test-v1",
            "schema_version": "1",
            "parser_version": "1",
            "generated_at": cutoff.isoformat(),
            "source_data_cutoff": cutoff.isoformat(),
            "freshest_quote_observed_at": cutoff.isoformat(),
            "validation_status": "passed",
            "release_status": "published",
            "job_statuses": dict(ALL_JOBS),
            "artifact_encoding": "raw",
            "artifact_format": "sqlite",
            "uncompressed_sha256": sha256(db.read_bytes()).hexdigest(),
            "sha256": sha256(db.read_bytes()).hexdigest(),
            "file_size_bytes": db.stat().st_size,
            "uncompressed_size_bytes": db.stat().st_size,
        }
        payload.update(overrides)
        manifest_path.write_text(json.dumps(payload, indent=2))
        return manifest_path

    def edit(*statements) -> None:
        """Apply SQL to the fixture database and re-stamp its manifest.

        Both halves, always. The manifest records the database's hash and
        `open_snapshot` verifies it before handing back a connection, so a test
        that mutates the database and leaves the manifest alone fails on a
        checksum mismatch rather than on the thing it set out to assert. Pairing
        them here means no test can forget the second half.
        """
        def run(conn):
            for statement in statements:
                if isinstance(statement, tuple):
                    conn.execute(*statement)
                else:
                    conn.execute(statement)

        mutate(run)

    def mutate(apply) -> None:
        """`edit` for changes a SQL string cannot express, e.g. adding markets."""
        conn = sqlite3.connect(db)
        try:
            apply(conn)
            conn.commit()
        finally:
            conn.close()
        write_manifest()

    write_manifest()
    return type(
        "Snap",
        (),
        {
            "cutoff": cutoff,
            "db": db,
            "manifest": manifest_path,
            "write_manifest": staticmethod(write_manifest),
            "edit": staticmethod(edit),
            "mutate": staticmethod(mutate),
            "dir": data,
        },
    )


# ------------------------------------------------------------------- gate --


class TestFreshnessGate:
    def test_fresh_snapshot_passes(self, snapshot):
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=5))
        assert result.publication_allowed, result.reasons
        assert result.hashes_verified
        assert result.integrity_ok

    def test_stale_snapshot_fails(self, snapshot):
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(hours=6))
        assert not result.publication_allowed
        assert GateFailure.STALE_SNAPSHOT in result.codes
        assert "stale snapshot" in result.blocked_reason.lower()

    def test_stale_freshest_quote_fails(self, snapshot):
        # Snapshot cut recently, but its newest quote is old. Distinct failure.
        snapshot.write_manifest(
            freshest_quote_observed_at=(
                snapshot.cutoff - timedelta(hours=3)
            ).isoformat()
        )
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=5))
        assert not result.publication_allowed
        assert GateFailure.STALE_QUOTE in result.codes

    def test_missing_manifest_fails(self, tmp_path):
        result = evaluate(tmp_path / "nope.json", as_of=AUGUST_3)
        assert not result.publication_allowed
        assert GateFailure.MANIFEST_MISSING in result.codes

    def test_bad_uncompressed_hash_fails(self, snapshot):
        snapshot.write_manifest(uncompressed_sha256="0" * 64, sha256="0" * 64)
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=5))
        assert not result.publication_allowed
        assert GateFailure.UNCOMPRESSED_HASH_MISMATCH in result.codes

    def test_bad_compressed_hash_fails(self, snapshot):
        gz = snapshot.dir / "pmvl-snapshot.db.gz"
        gz.write_bytes(gzip.compress(snapshot.db.read_bytes()))
        snapshot.write_manifest(
            artifact_encoding="gzip",
            compressed_path="data/pmvl-snapshot.db.gz",
            compressed_sha256="0" * 64,
            compressed_size_bytes=gz.stat().st_size,
        )
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=5))
        assert not result.publication_allowed
        assert GateFailure.COMPRESSED_HASH_MISMATCH in result.codes

    def test_sqlite_integrity_failure_fails(self, snapshot):
        """A file can match its checksum and still not be a database."""
        snapshot.db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
        snapshot.write_manifest(
            uncompressed_sha256=sha256(snapshot.db.read_bytes()).hexdigest(),
            sha256=sha256(snapshot.db.read_bytes()).hexdigest(),
            file_size_bytes=snapshot.db.stat().st_size,
            uncompressed_size_bytes=snapshot.db.stat().st_size,
        )
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=5))
        assert not result.publication_allowed
        assert GateFailure.SQLITE_INTEGRITY in result.codes

    @pytest.mark.parametrize("job", sorted(ALL_JOBS))
    def test_incomplete_pipeline_jobs_fail(self, snapshot, job):
        jobs = dict(ALL_JOBS)
        del jobs[job]
        snapshot.write_manifest(job_statuses=jobs)
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=5))
        assert not result.publication_allowed
        assert GateFailure.JOB_MISSING in result.codes

    def test_failed_job_fails(self, snapshot):
        snapshot.write_manifest(job_statuses={**ALL_JOBS, "score": "failed"})
        result = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=5))
        assert not result.publication_allowed
        assert GateFailure.JOB_FAILED in result.codes

    def test_unvalidated_or_held_fails(self, snapshot):
        snapshot.write_manifest(validation_status="pending")
        assert GateFailure.NOT_VALIDATED in evaluate(
            snapshot.manifest, as_of=snapshot.cutoff
        ).codes
        snapshot.write_manifest(release_status="held")
        assert GateFailure.NOT_PUBLISHED in evaluate(
            snapshot.manifest, as_of=snapshot.cutoff
        ).codes

    def test_historical_mode_does_not_relax_the_gate(self, snapshot):
        """The banner is a label, not a bypass."""
        late = snapshot.cutoff + timedelta(hours=6)
        strict = evaluate(snapshot.manifest, as_of=late)
        historical = evaluate(snapshot.manifest, as_of=late, historical_mode=True)
        assert strict.publication_allowed is historical.publication_allowed is False
        assert historical.historical_mode is True

    def test_sla_values_are_the_published_ones(self):
        assert DEFAULT_SLA.snapshot_max_age_seconds == 4 * 3600
        assert DEFAULT_SLA.quote_max_age_seconds == 2 * 3600
        assert DEFAULT_SLA.candidate_quote_max_age_seconds == 30 * 60


# -------------------------------------------------------------- selection --


def _select(snapshot, *, as_of=None, **kwargs):
    conn = sqlite3.connect(f"file:{snapshot.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return select_candidates(conn, as_of=as_of or snapshot.cutoff, **kwargs)
    finally:
        conn.close()


class TestCandidateSelection:
    def test_a_profitable_market_is_admitted(self, snapshot):
        candidates, _watch, _funnel, _rej, _n = _select(snapshot)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.side == "yes"
        assert c.net_edge_after_costs > 0
        assert c.executable_ask > 0
        assert c.decision_adjusted_probability is not None
        assert c.resolution_date is not None
        assert c.rules_risk and c.invalidation_conditions

    def test_negative_net_edge_is_rejected_to_the_watchlist(self, tmp_path, snapshot):
        # Ask above the conservative probability: buying is negative EV.
        snapshot.edit("update orderbook_levels set price = '0.90' where side = 'yes'")
        candidates, watch, _f, rejections, _n = _select(snapshot)
        assert candidates == []
        assert any("net ev" in r.reason.lower() for r in rejections)
        assert watch and all("Not actionable" in w.blocking_reasons[0] for w in watch)

    def test_inadequate_liquidity_is_rejected(self, snapshot):
        snapshot.edit("update orderbook_levels set size = '1' where side = 'yes'")
        candidates, watch, _f, rejections, _n = _select(snapshot)
        assert candidates == []
        assert any("depth" in r.reason.lower() for r in rejections)
        assert watch

    def test_stale_candidate_quote_cannot_be_actionable(self, snapshot):
        """Economics clear; the book behind them is too old. Watchlist, never actionable."""
        old = (snapshot.cutoff - timedelta(hours=2)).isoformat(sep=" ")
        snapshot.edit(("update orderbook_snapshots set observed_at = ?", (old,)))
        candidates, watch, _f, rejections, _n = _select(snapshot)
        assert candidates == []
        assert any("too old" in r.reason.lower() for r in rejections)
        assert watch
        assert any("minute" in r for w in watch for r in w.blocking_reasons)

    @pytest.mark.parametrize(
        "mutation,fragment,channel",
        [
            ("update markets set fee_rate = null", "fee rate", "watchlist"),
            ("delete from market_rules", "settlement rule", "watchlist"),
            # No resolution time means no horizon to place the market in, so it
            # never reaches the watchlist a horizon-scoped report publishes. It
            # is still counted, in the rejection tally.
            (
                "update markets set expected_resolution_time = null",
                "resolution time",
                "rejections",
            ),
            # An empty book removes the market before any field check - the same
            # safety outcome by a shorter route, so neither channel names it.
            ("delete from orderbook_levels", "", "silent"),
        ],
    )
    def test_missing_economics_fields_block_actionability(
        self, snapshot, mutation, fragment, channel
    ):
        snapshot.edit(mutation)
        candidates, watch, _f, rejections, _n = _select(snapshot)

        # The invariant that matters, and it holds however the market was removed.
        assert candidates == []

        # Beyond that: whatever removed it should have said so somewhere, because
        # a market silently vanishing from a paid report is indistinguishable
        # from a market that was never there.
        if channel == "watchlist":
            assert any(fragment in r for w in watch for r in w.blocking_reasons)
        elif channel == "rejections":
            assert any(fragment in r.reason.lower() for r in rejections)

    def test_no_independent_prior_cannot_be_actionable(self, snapshot):
        snapshot.edit("update model_predictions set has_independent_prior = 0")
        candidates, _w, _f, rejections, _n = _select(snapshot)
        assert candidates == []
        assert any("independent" in r.reason.lower() for r in rejections)

    def test_actionables_are_capped_at_three(self, snapshot):
        snapshot.mutate(
            lambda conn: [
                _add_market(
                    conn, i,
                    observed_at=snapshot.cutoff,
                    resolution=snapshot.cutoff + timedelta(days=3),
                )
                for i in range(2, 9)
            ]
        )
        candidates, _w, funnel, _r, _n = _select(snapshot)
        assert len(candidates) == MAX_CANDIDATES == 3
        published = next(s for s in funnel if s.label == "Published as actionable")
        assert published.count == 3
        admitted = next(s for s in funnel if s.label == "Distinct markets admitted")
        assert admitted.count == 8

    def test_funnel_distinguishes_markets_from_sides(self, snapshot):
        _c, _w, funnel, _r, _n = _select(snapshot)
        sides = next(s for s in funnel if s.label.startswith("Sides priced"))
        markets = next(s for s in funnel if s.label == "Distinct markets admitted")
        assert "sides, not markets" in sides.note
        assert "one side per market" in markets.note

    def test_zero_candidates_is_a_valid_outcome(self, snapshot):
        snapshot.edit("update orderbook_levels set price = '0.99' where side = 'yes'")
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=1))
        report = build_daily_digest(snapshot.manifest, gate)
        assert gate.publication_allowed
        assert report.actionable_count == 0
        assert "No actionable opportunity" in report.headline


# ----------------------------------------------------------------- render --


def _sample_report(historical: bool, snapshot) -> DigestReport:
    gate = evaluate(
        snapshot.manifest,
        as_of=snapshot.cutoff + timedelta(minutes=1),
        historical_mode=historical,
    )
    return build_daily_digest(snapshot.manifest, gate)


class TestRenderers:
    def test_all_formats_carry_snapshot_identity(self, snapshot):
        report = _sample_report(False, snapshot)
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            assert "test-snapshot" in text
            assert "test-v1" in text

    def test_all_formats_carry_the_disclaimer(self, snapshot):
        report = _sample_report(False, snapshot)
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            lowered = text.lower()
            assert "not investment" in lowered
            assert "not personalised" in lowered or "nothing here is personalised" in lowered

    def test_all_formats_carry_the_historical_warning(self, snapshot):
        report = _sample_report(True, snapshot)
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            assert "Historical sample" in text
            assert "Not current market research." in text
            assert HISTORICAL_SUBWARNING.split(".")[0] in text

    def test_non_historical_reports_carry_no_warning(self, snapshot):
        report = _sample_report(False, snapshot)
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            assert "Historical sample" not in text

    def test_all_formats_preserve_a_block_warning(self, snapshot):
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(hours=9))
        report = build_daily_digest(snapshot.manifest, gate)
        assert not gate.publication_allowed
        assert report.actionable_count == 0
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            lowered = text.lower()
            assert "stale" in lowered
            assert "no research issued" in lowered or "refused" in lowered

    def test_rules_risk_is_rendered(self, snapshot):
        report = _sample_report(False, snapshot)
        assert report.candidates
        markdown = to_markdown(report)
        assert "Rules risk" in markdown
        assert "What would invalidate this" in markdown

    def test_watchlist_is_labelled_non_actionable_everywhere(self, snapshot):
        snapshot.edit("update orderbook_levels set price = '0.90' where side = 'yes'")
        report = _sample_report(False, snapshot)
        assert report.watchlist
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            assert "NOT actionable" in text or "NOT ACTIONABLE" in text
            assert "not a recommendation" in text.lower()

    def test_no_guaranteed_return_or_advice_language(self, snapshot):
        report = _sample_report(False, snapshot)
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            lowered = text.lower()
            for phrase in (
                "guaranteed return",
                "guaranteed profit",
                "risk-free",
                "you should buy",
                "we recommend",
                "your portfolio",
            ):
                assert phrase not in lowered


# ------------------------------------------------------- weekly and CLI --


class TestWeeklyReview:
    def test_independent_and_derived_estimates_are_separated(self, snapshot):
        from pmvl_markets.pilot.outcomes import build_weekly_review

        settled_at = (snapshot.cutoff - timedelta(days=1)).isoformat(sep=" ")

        def add(conn):
            for i, independent in enumerate([1, 1, 0, 0], start=50):
                _add_market(
                    conn, i,
                    observed_at=snapshot.cutoff,
                    resolution=snapshot.cutoff + timedelta(days=1),
                    independent=independent,
                )
                conn.execute(
                    "insert into settlements values (?,?,?,?,?)",
                    (i, i, "yes", settled_at, "live"),
                )

        snapshot.mutate(add)

        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=1))
        review = build_weekly_review(snapshot.manifest, gate)
        labels = [b.label for b in review.accuracy]
        assert "Estimates with an independent prior" in labels
        assert "Estimates derived from the market price" in labels
        assert len({b.label for b in review.accuracy}) == len(review.accuracy)

    def test_small_samples_are_disclosed(self, snapshot):
        from pmvl_markets.pilot.outcomes import build_weekly_review

        settled_at = (snapshot.cutoff - timedelta(days=1)).isoformat(sep=" ")

        def add(conn):
            _add_market(
                conn, 60,
                observed_at=snapshot.cutoff,
                resolution=snapshot.cutoff + timedelta(days=1),
            )
            conn.execute(
                "insert into settlements values (?,?,?,?,?)",
                (60, 60, "yes", settled_at, "live"),
            )

        snapshot.mutate(add)
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=1))
        review = build_weekly_review(snapshot.manifest, gate)
        assert any("too small a sample" in limit for limit in review.limitations)

    def test_a_week_with_no_recommendations_says_so(self, snapshot):
        from pmvl_markets.pilot.outcomes import build_weekly_review

        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=1))
        review = build_weekly_review(snapshot.manifest, gate)
        assert review.recommendations_published == 0
        assert any("No recommendations were published" in x for x in review.limitations)


class TestCli:
    def _run(self, argv: list[str]) -> int:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gen_pilot", ROOT / "scripts" / "generate_pilot_digest.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.main(argv)

    def test_cli_blocks_stale_current_generation(self, snapshot, tmp_path):
        out = tmp_path / "out"
        code = self._run(
            [
                "--manifest", str(snapshot.manifest),
                "--out", str(out),
                "--as-of", (snapshot.cutoff + timedelta(hours=9)).isoformat(),
                "--name", "blocked",
            ]
        )
        assert code == 3
        payload = json.loads((out / "blocked.json").read_text())
        assert payload["publication_allowed"] is False
        assert payload["actionable_candidates"] == 0
        assert "stale" in payload["blocked_reason"].lower()

    def test_cli_succeeds_on_a_fresh_snapshot(self, snapshot, tmp_path):
        out = tmp_path / "out"
        code = self._run(
            [
                "--manifest", str(snapshot.manifest),
                "--out", str(out),
                "--as-of", (snapshot.cutoff + timedelta(minutes=5)).isoformat(),
                "--name", "fresh",
            ]
        )
        assert code == 0
        payload = json.loads((out / "fresh.json").read_text())
        assert payload["publication_allowed"] is True
        assert payload["historical_sample"] is False

    def test_historical_mode_is_never_publishable_as_current(self, snapshot, tmp_path):
        """The one thing a retrospective must not buy is permission to publish.

        A historical sample is produced by evaluating the gate at the Snapshot's
        own cutoff - the exact manoeuvre that makes stale data look fresh. So the
        sample renders in full, and `publication_allowed` stays false anyway.
        """
        out = tmp_path / "out"
        code = self._run(
            [
                "--manifest", str(snapshot.manifest),
                "--out", str(out),
                "--historical-sample",
                "--name", "historical",
            ]
        )
        assert code == 0
        payload = json.loads((out / "historical.json").read_text())
        assert payload["historical_sample"] is True
        # Sound artefact, evaluated at its own cutoff - and still not publishable.
        assert payload["publication_allowed"] is False
        # But the report itself was produced, not suppressed.
        assert (out / "historical.md").read_text().strip()

    def test_historical_warning_survives_every_format(self, snapshot, tmp_path):
        out = tmp_path / "out"
        self._run(
            [
                "--manifest", str(snapshot.manifest),
                "--out", str(out),
                "--historical-sample",
                "--name", "historical",
            ]
        )
        expected = (
            "Historical sample — generated from the validated Snapshot dated "
            "2026-07-31. Not current market research."
        )
        for suffix in ("md", "txt", "html"):
            text = (out / f"historical.{suffix}").read_text()
            # The HTML renderer escapes the em dash; the sentence is the same one.
            normalised = " ".join(
                text.replace("&#8212;", "—").replace("&mdash;", "—").split()
            )
            assert expected in normalised, f"{suffix} lost the historical warning"


# ------------------------------------------------- the committed Snapshot --


@pytest.mark.skipif(not REAL_MANIFEST.exists(), reason="snapshot not built here")
class TestCommittedSnapshot:
    """The real artefact. These are the assertions that protect subscribers."""

    def test_the_committed_snapshot_cannot_generate_current_research_once_stale(self):
        gate = evaluate(REAL_MANIFEST, as_of=WELL_AFTER_CUTOFF)
        assert gate.publication_allowed is False
        assert {GateFailure.STALE_SNAPSHOT, GateFailure.STALE_QUOTE} <= set(gate.codes)

        report = build_daily_digest(REAL_MANIFEST, gate)
        assert report.actionable_count == 0
        assert report.candidates == []
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            assert "stale" in text.lower()

    def test_the_committed_snapshot_verifies_both_hashes_and_integrity(self):
        gate = evaluate(REAL_MANIFEST, as_of=SNAPSHOT_CUTOFF + timedelta(minutes=1))
        assert gate.hashes_verified
        assert gate.integrity_ok


class TestCommittedSamples:
    """The samples in the repository must announce what they are."""

    SAMPLES = sorted((ROOT / "docs" / "samples" / "pilot").glob("historical-*"))

    def test_samples_exist_in_all_three_formats(self):
        stems = {p.stem for p in self.SAMPLES}
        assert {
            "historical-no-actionable",
            "historical-watchlist",
            "historical-weekly-review",
        } <= stems
        for stem in stems:
            for suffix in (".md", ".html", ".txt"):
                assert (ROOT / "docs" / "samples" / "pilot" / f"{stem}{suffix}").exists()

    @pytest.mark.parametrize("suffix", [".md", ".html", ".txt"])
    def test_every_sample_carries_the_historical_warning(self, suffix):
        for path in (ROOT / "docs" / "samples" / "pilot").glob(f"historical-*{suffix}"):
            text = path.read_text()
            assert "Historical sample" in text, path.name
            assert "Not current market research." in text, path.name
            assert "2026-07-31" in text, path.name

    @pytest.mark.parametrize("suffix", [".md", ".html", ".txt"])
    def test_every_sample_carries_snapshot_identity_and_limitations(self, suffix):
        for path in (ROOT / "docs" / "samples" / "pilot").glob(f"historical-*{suffix}"):
            text = path.read_text()
            assert "a3487a6fa577" in text, path.name
            assert "not investment" in text.lower(), path.name

    def test_no_sample_claims_a_current_actionable_candidate(self):
        for path in (ROOT / "docs" / "samples" / "pilot").glob("historical-*.json"):
            payload = json.loads(path.read_text())
            assert payload["historical_sample"] is True
            assert payload["actionable_candidates"] == 0
            # A sample is generated at the Snapshot's own cutoff, so the checks
            # pass. Publication as *current* research must still be refused.
            assert payload["publication_allowed"] is False

    def test_sales_page_figures_match_the_committed_samples(self):
        """The sales page quotes the samples, so it can be wrong about them.

        These numbers came out of a real scan and get regenerated whenever the
        selection logic changes; the page's prose does not. Left unchecked, that
        drifts into a marketing page citing results the shipped samples do not
        contain - which is the ordinary way an honest claim becomes a false one.
        """
        page = (
            ROOT / "apps" / "web" / "app" / "(site)" / "founding-pilot" / "page.tsx"
        ).read_text()
        daily = (ROOT / "docs" / "samples" / "pilot" / "historical-no-actionable.md").read_text()

        def funnel_count(label: str) -> str:
            row = next(line for line in daily.splitlines() if line.startswith(f"| {label}"))
            return row.split("|")[2].strip()

        for label in ("With an independent prior", "Sides priced against the ask ladder"):
            count = funnel_count(label)
            assert count in page, (
                f"the sales page does not mention the sample's {label!r} count of {count}"
            )

        weekly = (ROOT / "docs" / "samples" / "pilot" / "historical-weekly-review.md").read_text()
        settled = re.search(r"(\d+) scored markets settled", weekly)
        assert settled and settled.group(1) in page, "settled-market count is out of date"


# ---------------------------------------- report level vs candidate level --


class TestReportGateIsSeparateFromCandidateGate:
    """The two freshness levels, and the boundary between them.

    The pilot was publishable for only about thirty minutes after each
    publication. `TOP_OF_BOOK` hard-stales at 30 minutes in the shared policy,
    the gate measured it against the Snapshot's single freshest quote, and a
    refusal there withheld the whole digest - including a zero-actionable report,
    which contains no recommendation for a stale book to invalidate. A
    subscriber's honest "nothing cleared the bar today" was replaced by silence,
    and the advertised 2-hour quote SLA was silently overridden by a 30-minute
    one.

    A stale book invalidates the candidate resting on it. It does not invalidate
    the report saying so.
    """

    # -- the report level keeps publishing ---------------------------------

    def test_the_report_still_publishes_after_31_minutes(self, snapshot):
        """The exact regression. 31 minutes is one minute past the candidate
        rule and nowhere near either report-level limit."""
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=31))
        assert gate.publication_allowed, gate.reasons

    @pytest.mark.parametrize("minutes", [20, 31, 45, 90, 119])
    def test_the_report_publishes_anywhere_inside_the_report_sla(
        self, snapshot, minutes
    ):
        """Publication must track the published SLA, not a candidate threshold."""
        gate = evaluate(
            snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=minutes)
        )
        assert gate.publication_allowed, f"blocked at {minutes} min: {gate.reasons}"

    def test_ageing_books_are_reported_without_blocking(self, snapshot):
        """The finding must survive; only its consequence changes."""
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=45))
        assert gate.publication_allowed, gate.reasons
        assert "top_of_book" in gate.actionable_inputs_blocked
        assert gate.codes == []

    # -- the report level still blocks hard --------------------------------

    def test_a_snapshot_older_than_four_hours_blocks_the_report(self, snapshot):
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(hours=4, minutes=1))
        assert gate.publication_allowed is False
        assert GateFailure.STALE_SNAPSHOT in gate.codes

    def test_a_quote_older_than_two_hours_blocks_the_report(self, snapshot):
        """Aged quote, fresh cutoff: isolates the quote SLA from the snapshot one."""
        as_of = snapshot.cutoff + timedelta(hours=2, minutes=1)
        snapshot.write_manifest(source_data_cutoff=(as_of - timedelta(minutes=5)).isoformat())
        gate = evaluate(snapshot.manifest, as_of=as_of)
        assert gate.publication_allowed is False
        assert GateFailure.STALE_QUOTE in gate.codes
        assert GateFailure.STALE_SNAPSHOT not in gate.codes

    def test_a_held_snapshot_still_blocks_the_report(self, snapshot):
        snapshot.write_manifest(release_status="held")
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=31))
        assert gate.publication_allowed is False
        assert GateFailure.NOT_PUBLISHED in gate.codes

    def test_a_failed_pipeline_job_still_blocks_the_report(self, snapshot):
        snapshot.write_manifest(job_statuses={**ALL_JOBS, "settle": "failed"})
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=31))
        assert gate.publication_allowed is False
        assert GateFailure.JOB_FAILED in gate.codes

    def test_a_broken_hash_still_blocks_the_report(self, snapshot):
        snapshot.write_manifest(sha256="0" * 64, uncompressed_sha256="0" * 64)
        gate = evaluate(snapshot.manifest, as_of=snapshot.cutoff + timedelta(minutes=31))
        assert gate.publication_allowed is False

    # -- the candidate level still refuses ---------------------------------

    def test_a_31_minute_old_book_is_not_actionable(self, snapshot):
        candidates, _watch, _f, _rej, _n = _select(
            snapshot, as_of=snapshot.cutoff + timedelta(minutes=31)
        )
        assert candidates == []

    def test_that_candidate_is_demoted_to_the_watchlist_with_the_reason(self, snapshot):
        _c, watch, _f, _rej, _n = _select(
            snapshot, as_of=snapshot.cutoff + timedelta(minutes=31)
        )
        assert len(watch) == 1
        reason = " ".join(watch[0].blocking_reasons).lower()
        assert "order book" in reason
        assert "31 minutes ago" in reason
        assert "30-minute limit" in reason

    def test_the_same_market_is_actionable_while_its_book_is_fresh(self, snapshot):
        """Proves the demotion above is caused by age and nothing else."""
        candidates, _w, _f, _rej, _n = _select(
            snapshot, as_of=snapshot.cutoff + timedelta(minutes=29)
        )
        assert len(candidates) == 1

    def test_no_stale_candidate_ever_reaches_the_actionable_list(self, snapshot):
        """Swept across the boundary: nothing past the limit may be actionable."""
        for minutes in (29, 30, 31, 45, 90, 119):
            as_of = snapshot.cutoff + timedelta(minutes=minutes)
            candidates, _w, _f, _rej, _n = _select(snapshot, as_of=as_of)
            for candidate in candidates:
                age = candidate.quote_age_seconds
                assert age is not None and age <= DEFAULT_SLA.candidate_quote_max_age_seconds, (
                    f"a {age / 60:.0f}-minute-old quote was presented as actionable "
                    f"at {minutes} minutes"
                )

    # -- the two levels together -------------------------------------------

    def test_a_zero_actionable_report_is_still_publishable(self, snapshot):
        """The case the old behaviour destroyed: a truthful no-trade report."""
        as_of = snapshot.cutoff + timedelta(minutes=45)
        gate = evaluate(snapshot.manifest, as_of=as_of)
        report = build_daily_digest(snapshot.manifest, gate)
        assert gate.publication_allowed, gate.reasons
        assert report.actionable_allowed is True
        assert report.candidates == []
        assert report.watchlist, "the demoted market must still be visible"

    def test_the_watchlist_entry_is_not_presented_as_a_recommendation(self, snapshot):
        as_of = snapshot.cutoff + timedelta(minutes=45)
        report = build_daily_digest(snapshot.manifest, evaluate(snapshot.manifest, as_of=as_of))
        assert report.candidates == []
        for entry in report.watchlist:
            assert entry.blocking_reasons, "a watchlist entry must say why it is not actionable"


class TestRenderersSeparateTheTwoLevels:
    """Every format must let a reader tell the two apart.

    A reader who sees an empty actionable list next to a 45-minute-old order book
    has to be able to conclude "the books aged out today", not "the report is
    broken" and not "these watchlist entries are the recommendations".
    """

    @staticmethod
    def _report(snapshot, minutes: int) -> DigestReport:
        as_of = snapshot.cutoff + timedelta(minutes=minutes)
        return build_daily_digest(snapshot.manifest, evaluate(snapshot.manifest, as_of=as_of))

    def test_every_format_says_the_report_is_publishable_and_names_the_cause(
        self, snapshot
    ):
        report = self._report(snapshot, 45)
        assert report.actionable_allowed is True
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            lowered = text.lower()
            assert "top of book" in lowered
            assert "candidate-level note" in lowered
            assert "watchlist" in lowered

    def test_every_format_marks_which_inputs_cannot_support_a_candidate(
        self, snapshot
    ):
        report = self._report(snapshot, 45)
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            assert "can support a candidate" in text.lower()

    def test_no_format_presents_the_watchlist_as_actionable(self, snapshot):
        report = self._report(snapshot, 45)
        assert report.candidates == []
        for text in (to_markdown(report), to_text(report), to_html_email(report)):
            assert "not actionable" in text.lower()

    def test_the_json_distinguishes_the_two_gates(self, snapshot, tmp_path):
        """The four keys the consumer needs, with report-level and
        candidate-level failures kept in separate fields."""
        as_of = snapshot.cutoff + timedelta(minutes=45)
        gate = evaluate(snapshot.manifest, as_of=as_of)
        report = build_daily_digest(snapshot.manifest, gate)
        payload = {
            "report_publication_allowed": gate.publication_allowed,
            "actionable_candidate_count": len(report.candidates),
            "watchlist_count": len(report.watchlist),
            "actionable_inputs_blocked": gate.actionable_inputs_blocked,
            "gate": gate.as_dict(),
        }
        assert payload["report_publication_allowed"] is True
        assert payload["actionable_candidate_count"] == 0
        assert payload["watchlist_count"] >= 1
        assert "top_of_book" in payload["actionable_inputs_blocked"]
        # Report-level failures stay empty; the candidate-level fact lives
        # somewhere a reader cannot mistake for a publication blocker.
        assert payload["gate"]["failures"] == []
        assert payload["gate"]["blocked_reason"] == ""
        blocked = {f["data_type"] for f in payload["gate"]["freshness"] if f["blocks_actionable"]}
        assert "top_of_book" in blocked
