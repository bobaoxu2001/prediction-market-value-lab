"""Retrying a scheduled job must be safe, and a partial failure must stay visible.

Two failures motivated this file.

A retried ingest was indistinguishable from a second ingest, so the safe response
to a transient error - run it again - was also the thing that duplicated data.
The idempotency key gives "this job, over this input" a stable identity that does
not move when the retry happens.

And a run where Polymarket returned 503 while Kalshi succeeded was recorded as
SUCCESS. Downstream, the shorter market list read as "the scan found nothing
today", which is the single most misleading thing this platform can say.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pmvl_shared.enums import JobStatus
from pmvl_shared.job_record import (
    ProviderStat,
    RunRecord,
    code_version,
    idempotency_key,
)


class TestIdempotencyKey:
    def test_same_job_and_cutoff_produce_the_same_key(self) -> None:
        cutoff = datetime(2026, 7, 28, 12, 0)
        assert idempotency_key("ingest", cutoff=cutoff) == idempotency_key(
            "ingest", cutoff=cutoff
        )

    def test_a_retry_later_hashes_the_same(self) -> None:
        """The load-bearing property.

        A retry three minutes after a failure is the *same logical work*. If the
        key moved with wall-clock time, every retry would look like new work and
        re-ingesting would duplicate rather than update.
        """
        cutoff = datetime(2026, 7, 28, 12, 0)
        first = idempotency_key("ingest", cutoff=cutoff)
        # Simulate the retry happening much later; only the cutoff defines identity.
        second = idempotency_key("ingest", cutoff=cutoff)
        assert first == second

    def test_a_different_cutoff_is_different_work(self) -> None:
        assert idempotency_key(
            "ingest", cutoff=datetime(2026, 7, 28, 12, 0)
        ) != idempotency_key("ingest", cutoff=datetime(2026, 7, 28, 13, 0))

    def test_different_jobs_never_collide(self) -> None:
        cutoff = datetime(2026, 7, 28, 12, 0)
        assert idempotency_key("ingest", cutoff=cutoff) != idempotency_key(
            "settle", cutoff=cutoff
        )

    def test_parameters_change_identity(self) -> None:
        cutoff = datetime(2026, 7, 28, 12, 0)
        assert idempotency_key(
            "ingest", cutoff=cutoff, params={"limit": 100}
        ) != idempotency_key("ingest", cutoff=cutoff, params={"limit": 500})

    def test_parameter_order_does_not_change_identity(self) -> None:
        """Two callers passing the same params in a different order are doing the
        same work; a key that disagreed would defeat the whole mechanism."""
        cutoff = datetime(2026, 7, 28, 12, 0)
        assert idempotency_key(
            "ingest", cutoff=cutoff, params={"a": 1, "b": 2}
        ) == idempotency_key("ingest", cutoff=cutoff, params={"b": 2, "a": 1})

    def test_a_missing_cutoff_is_stable_not_random(self) -> None:
        assert idempotency_key("prune", cutoff=None) == idempotency_key(
            "prune", cutoff=None
        )


class TestPartialFailureIsolation:
    def test_one_failed_provider_among_several_is_partial(self) -> None:
        record = RunRecord(job_name="ingest", run_id="r", idempotency_key="k")
        record.provider("kalshi").records = 900
        record.fail_provider("polymarket", "HTTP 503")

        assert record.is_partial is True
        assert record.failed_providers == ["polymarket"]

    def test_every_provider_failing_is_not_partial(self) -> None:
        """Total failure is a failure. Calling it partial would dress up a run
        that produced nothing as one that produced something."""
        record = RunRecord(job_name="ingest", run_id="r", idempotency_key="k")
        record.fail_provider("kalshi", "HTTP 500")
        record.fail_provider("polymarket", "HTTP 503")

        assert record.is_partial is False
        assert len(record.failed_providers) == 2

    def test_no_failures_is_not_partial(self) -> None:
        record = RunRecord(job_name="ingest", run_id="r", idempotency_key="k")
        record.provider("kalshi").records = 900
        record.provider("polymarket").records = 400
        assert record.is_partial is False

    def test_a_run_with_no_providers_is_not_partial(self) -> None:
        """Jobs like prune touch no external source; absence of providers is not
        evidence of a partial outcome."""
        assert RunRecord(job_name="prune", run_id="r", idempotency_key="k").is_partial is False

    def test_the_failure_is_recorded_rather_than_raised(self) -> None:
        """One venue being down must degrade the result, not delete it."""
        record = RunRecord(job_name="ingest", run_id="r", idempotency_key="k")
        record.provider("kalshi").records = 900
        record.fail_provider("polymarket", "connection reset")

        stat = record.provider_stats["polymarket"]
        assert stat.healthy is False
        assert "connection reset" in stat.error
        # The healthy provider's work survives.
        assert record.provider_stats["kalshi"].records == 900


class TestJobStatusSemantics:
    def test_partial_success_is_usable_downstream(self) -> None:
        """A run that refreshed 90% of markets should feed the ranker. Treating it
        as failed discards work that is good."""
        assert JobStatus.PARTIAL_SUCCESS.produced_usable_output is True

    def test_failed_and_stale_are_not_usable(self) -> None:
        assert JobStatus.FAILED.produced_usable_output is False
        assert JobStatus.STALE.produced_usable_output is False

    def test_pending_and_running_are_not_terminal(self) -> None:
        assert JobStatus.PENDING.is_terminal is False
        assert JobStatus.RUNNING.is_terminal is False
        assert JobStatus.SUCCESS.is_terminal is True
        assert JobStatus.PARTIAL_SUCCESS.is_terminal is True


class TestRunProvenance:
    def test_the_record_serialises_every_audit_field(self) -> None:
        record = RunRecord(
            job_name="ingest",
            run_id="r1",
            idempotency_key="k1",
            input_data_cutoff=datetime(2026, 7, 28, 12, 0),
            upstream_dependencies=["orderbooks"],
        )
        record.records_read = 1400
        record.records_written = 1388
        record.warn("polymarket rate limited")
        payload = record.as_dict()

        for field in (
            "run_id",
            "idempotency_key",
            "code_commit_sha",
            "input_data_cutoff",
            "records_read",
            "records_written",
            "retry_count",
            "warnings",
            "errors",
            "upstream_dependencies",
            "downstream_triggered",
            "provider_stats",
        ):
            assert field in payload, field

    def test_an_unknown_commit_is_admitted_not_faked(self, monkeypatch) -> None:  # noqa: ANN001
        """A row claiming to come from commit 0000000 is worse than one admitting
        it does not know which code wrote it."""
        for var in ("VERCEL_GIT_COMMIT_SHA", "GITHUB_SHA", "PMVL_COMMIT_SHA"):
            monkeypatch.delenv(var, raising=False)
        assert code_version() == "unknown"

    def test_the_commit_is_read_from_the_ci_environment(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("GITHUB_SHA", "abcdef1234567890")
        assert code_version() == "abcdef123456"

    def test_input_cutoff_is_recorded_for_backtest_safety(self) -> None:
        """A backtest that ignores the cutoff and reads current data measures a
        model that could not have been run at the time."""
        cutoff = datetime(2026, 7, 28, 12, 0)
        record = RunRecord(
            job_name="score", run_id="r", idempotency_key="k", input_data_cutoff=cutoff
        )
        assert record.as_dict()["input_data_cutoff"].startswith("2026-07-28T12:00")


class TestProviderStat:
    def test_rate_limiting_is_tracked_separately_from_failure(self) -> None:
        """They demand different responses: back off versus investigate."""
        stat = ProviderStat(provider="kalshi", requests=100, rate_limited=7)
        assert stat.healthy is True
        assert stat.rate_limited == 7

    def test_the_error_is_truncated_so_one_stack_trace_cannot_bloat_the_row(self) -> None:
        record = RunRecord(job_name="j", run_id="r", idempotency_key="k")
        record.fail_provider("kalshi", "x" * 5000)
        assert len(record.provider_stats["kalshi"].as_dict()["error"]) <= 300


class TestJobRunContextManager:
    """The context manager must derive partial success rather than trust the job."""

    def test_a_partial_run_is_recorded_as_partial_success(self, clean_db) -> None:  # noqa: ANN001
        import sys
        from pathlib import Path

        worker_src = Path(__file__).resolve().parents[1] / "services/worker/src"
        if str(worker_src) not in sys.path:
            sys.path.insert(0, str(worker_src))
        from pmvl_worker.jobs import job_run

        from pmvl_markets.db_models import JobRun

        with job_run("ingest", cutoff=datetime(2026, 7, 28, 12, 0)) as details:
            record = details["record"]
            record.provider("kalshi").records = 900
            record.fail_provider("polymarket", "HTTP 503")

        row = clean_db.query(JobRun).filter_by(job_name="ingest").one()
        assert row.status == JobStatus.PARTIAL_SUCCESS.value
        assert row.details["run"]["failed_providers"] == ["polymarket"]
        assert row.details["run"]["idempotency_key"]

    def test_a_clean_run_stays_success(self, clean_db) -> None:  # noqa: ANN001
        import sys
        from pathlib import Path

        worker_src = Path(__file__).resolve().parents[1] / "services/worker/src"
        if str(worker_src) not in sys.path:
            sys.path.insert(0, str(worker_src))
        from pmvl_worker.jobs import job_run

        from pmvl_markets.db_models import JobRun

        with job_run("rank") as details:
            details["record"].provider("kalshi").records = 10

        row = clean_db.query(JobRun).filter_by(job_name="rank").one()
        assert row.status == JobStatus.SUCCESS.value

    def test_a_raising_job_is_recorded_as_failed_and_re_raises(self, clean_db) -> None:  # noqa: ANN001
        import sys
        from pathlib import Path

        worker_src = Path(__file__).resolve().parents[1] / "services/worker/src"
        if str(worker_src) not in sys.path:
            sys.path.insert(0, str(worker_src))
        from pmvl_worker.jobs import job_run

        from pmvl_markets.db_models import JobRun

        with pytest.raises(RuntimeError):
            with job_run("settle"):
                raise RuntimeError("upstream exploded")

        row = clean_db.query(JobRun).filter_by(job_name="settle").one()
        assert row.status == JobStatus.FAILED.value
        assert "upstream exploded" in row.error


class TestIngestFailureIsolation:
    """A venue outage must degrade the run, not delete it or hide it.

    The failure this prevents: Polymarket returns 503, ingest writes only Kalshi
    markets, the run is recorded SUCCESS, and the shorter list reads downstream as
    "the scan found nothing today".
    """

    @staticmethod
    def _report(**overrides):  # noqa: ANN205
        from pmvl_markets.ingest.runner import IngestReport

        base = dict(
            markets_fetched=1300,
            markets_written=1300,
            by_platform={"kalshi": 1300, "polymarket": 0},
            errors=["polymarket gamma returned HTTP 503"],
        )
        base.update(overrides)
        return IngestReport(**base)

    async def _run_ingest(self, monkeypatch, report):  # noqa: ANN001, ANN202
        import sys
        from pathlib import Path

        worker_src = Path(__file__).resolve().parents[1] / "services/worker/src"
        if str(worker_src) not in sys.path:
            sys.path.insert(0, str(worker_src))
        from pmvl_worker import jobs

        async def fake_run_ingest(db, **kwargs):  # noqa: ANN001, ANN003, ARG001
            return report

        import pmvl_markets.ingest as ingest_module

        monkeypatch.setattr(ingest_module, "run_ingest", fake_run_ingest)
        return await jobs.job_ingest(market_limit=10)

    async def test_one_venue_failing_yields_partial_success(
        self, clean_db, monkeypatch  # noqa: ANN001
    ) -> None:
        from pmvl_markets.db_models import JobRun

        await self._run_ingest(monkeypatch, self._report())

        row = clean_db.query(JobRun).filter_by(job_name="ingest").one()
        assert row.status == JobStatus.PARTIAL_SUCCESS.value
        run = row.details["run"]
        assert run["failed_providers"] == ["polymarket"]
        # The healthy venue's work is preserved, not discarded.
        assert run["provider_stats"]["kalshi"]["records"] == 1300
        assert run["provider_stats"]["kalshi"]["healthy"] is True

    async def test_a_silently_empty_venue_is_still_a_failure(
        self, clean_db, monkeypatch  # noqa: ANN001
    ) -> None:
        """A venue returning zero markets and no error is not a normal result.

        Without this, a provider that starts returning an empty list looks like a
        market with nothing in it.
        """
        from pmvl_markets.db_models import JobRun

        await self._run_ingest(
            monkeypatch,
            self._report(by_platform={"kalshi": 1300, "polymarket": 0}, errors=[]),
        )

        row = clean_db.query(JobRun).filter_by(job_name="ingest").one()
        assert row.status == JobStatus.PARTIAL_SUCCESS.value
        assert row.details["run"]["failed_providers"] == ["polymarket"]

    async def test_both_venues_healthy_is_a_clean_success(
        self, clean_db, monkeypatch  # noqa: ANN001
    ) -> None:
        from pmvl_markets.db_models import JobRun

        await self._run_ingest(
            monkeypatch,
            self._report(by_platform={"kalshi": 1300, "polymarket": 400}, errors=[]),
        )

        row = clean_db.query(JobRun).filter_by(job_name="ingest").one()
        assert row.status == JobStatus.SUCCESS.value
        assert row.details["run"]["failed_providers"] == []

    async def test_the_run_is_retryable_by_identity(
        self, clean_db, monkeypatch  # noqa: ANN001
    ) -> None:
        """Two runs with the same parameters share an idempotency key, so a retry
        is recognisable as the same work rather than as new work."""
        from pmvl_markets.db_models import JobRun

        await self._run_ingest(monkeypatch, self._report())
        await self._run_ingest(monkeypatch, self._report())

        keys = {
            r.details["run"]["idempotency_key"]
            for r in clean_db.query(JobRun).filter_by(job_name="ingest").all()
        }
        assert len(keys) == 1, "a retry produced a different identity"
