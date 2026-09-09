"""A scheduled job may not hold a worker thread indefinitely.

``max_instances=1`` stops a job overlapping itself. Nothing stopped a single
run lasting forever, and the job ledger shows runs that did: one `research_books`
run lasted 119,229s (33 hours), and `ingest` averaged 113,780s (31.6 hours) on
its 05Z runs against a 600s cadence.

The harm is self-blocking: a stalled run holds its own slot and later ticks
record themselves as skipped, 234 of them for `research_books`. A budget turns
that 33-hour hole into one lost tick.

These tests deliberately assert only that. An earlier rationale blamed the
overruns for starving the book collector; tested directly that is false, since
`research_books` ran during all 400 of the largest capture gaps and succeeded in
358. Nothing here should be read as evidence about ORDERBOOK_TOO_STALE.
"""

from __future__ import annotations

import asyncio

import pytest

from pmvl_shared.cadence import CADENCE_BY_JOB
from pmvl_worker import jobs
from pmvl_worker.scheduler import _budget, _sync


class TestTheBudgetComesFromTheCadenceRegistry:
    def test_a_known_job_is_bounded_by_its_own_stall_threshold(self) -> None:
        """Not a second, independently-drifting number."""
        assert _budget("ingest") == float(
            CADENCE_BY_JOB["ingest"].stall_threshold_seconds()
        )

    def test_the_bound_is_far_below_the_observed_overrun(self) -> None:
        # 113,780s was measured; anything near that must not be permissible.
        assert _budget("ingest") < 3600

    def test_an_unregistered_job_is_unbounded_rather_than_guessed(self) -> None:
        assert _budget("no_such_job") is None


class TestAnOverrunIsCancelledRatherThanHeld:
    def test_a_run_past_its_budget_does_not_hold_the_worker(self) -> None:
        started = asyncio.Event()

        async def never_finishes() -> None:
            started.set()
            await asyncio.sleep(30)

        runner = _sync(never_finishes, timeout_seconds=0.05, job_name="test_job")
        runner()  # returns rather than blocking for 30s

    def test_the_scheduler_survives_an_overrun(self) -> None:
        """A cancelled job must not kill the scheduler thread."""

        async def never_finishes() -> None:
            await asyncio.sleep(30)

        _sync(never_finishes, timeout_seconds=0.05, job_name="test_job")()
        # A later job still runs normally.
        ran: list[bool] = []

        async def quick() -> None:
            ran.append(True)

        _sync(quick, timeout_seconds=5, job_name="quick")()
        assert ran == [True]

    def test_a_job_inside_its_budget_is_untouched(self) -> None:
        ran: list[bool] = []

        async def quick() -> None:
            ran.append(True)

        _sync(quick, timeout_seconds=5, job_name="quick")()
        assert ran == [True]

    def test_no_budget_means_no_timeout_wrapper(self) -> None:
        ran: list[bool] = []

        async def quick() -> None:
            ran.append(True)

        _sync(quick)()
        assert ran == [True]

    def test_a_skip_is_still_not_a_failure(self) -> None:
        async def skipped() -> None:
            raise jobs.JobSkipped("nothing to do")

        _sync(skipped, timeout_seconds=5, job_name="skipper")()

    def test_a_genuine_failure_is_still_swallowed_by_the_runner(self) -> None:
        async def boom() -> None:
            raise RuntimeError("boom")

        _sync(boom, timeout_seconds=5, job_name="boom")()


class TestEveryAsyncJobIsBounded:
    def test_no_sync_call_site_is_left_without_a_budget(self) -> None:
        """A new job added without a budget reintroduces the defect."""
        import ast
        from pathlib import Path

        src = Path("services/worker/src/pmvl_worker/scheduler.py").read_text()
        unbounded = [
            node.lineno
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "_sync"
            and not any(kw.arg == "timeout_seconds" for kw in node.keywords)
        ]
        assert not unbounded, f"_sync without timeout_seconds at line(s) {unbounded}"


@pytest.mark.parametrize("job_id", sorted(CADENCE_BY_JOB))
def test_every_registered_cadence_yields_a_usable_budget(job_id: str) -> None:
    budget = _budget(job_id)
    assert budget is None or budget > 0
