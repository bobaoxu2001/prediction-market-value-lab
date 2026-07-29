"""Which jobs run, in what order, and what a failure upstream means downstream.

The workflow ran ``ingest``, ``rank``, ``arbitrage`` and stopped. That is three of
nine jobs: no orderbook refresh, no settlement sync, no scoring, no recommendation
snapshot, no backtest, no retention. A publication built from that is missing the
settlement results the track record grades against and the daily immutable record
the backtest reads, so the artefact would look complete and quietly not be.

Two decisions live here, and both have to be explicit rather than implied by call
order in a shell script:

**What each job needs.** ``rank`` cannot run before ``score``; ``backtest`` cannot
run before ``snapshot``. Encoding this as a DAG means a missing upstream is a
detected condition rather than a subtly wrong output.

**Whether a degraded upstream is good enough.** This is not uniform. ``rank`` can
work from a PARTIAL_SUCCESS ingest - fewer markets ranked is a smaller list, which
is honest. ``settle`` cannot: grading a recommendation against a settlement feed
that half-failed produces a track record that is wrong in a direction nobody can
see. Each edge therefore declares its own tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import JobStatus


@dataclass(frozen=True)
class JobSpec:
    name: str
    description: str
    #: Jobs that must have produced usable output before this one may run.
    depends_on: tuple[str, ...] = ()
    #: Upstreams whose PARTIAL_SUCCESS is acceptable. An upstream not listed here
    #: must have fully succeeded.
    accepts_partial_from: frozenset[str] = field(default_factory=frozenset)
    #: Whether this job may itself end in PARTIAL_SUCCESS and still count as done.
    may_partially_succeed: bool = False
    #: A job whose failure must stop publication outright.
    required_for_publication: bool = True


#: Execution order is a topological sort of this DAG, not the order written here,
#: but the two agree and a test asserts it.
PIPELINE: tuple[JobSpec, ...] = (
    JobSpec(
        "ingest",
        "Market and event discovery, rules capture, orderbooks",
        may_partially_succeed=True,
    ),
    JobSpec(
        "orderbooks",
        "Refresh books for markets that can rank",
        depends_on=("ingest",),
        accepts_partial_from=frozenset({"ingest"}),
        may_partially_succeed=True,
    ),
    JobSpec(
        "settle",
        "Settlement synchronisation and grading",
        depends_on=("ingest",),
        # Deliberately NOT tolerant of a partial ingest. Grading against a feed
        # that half-failed writes a track record that is wrong in a direction
        # nobody can see afterwards, and the track record is the product's
        # central honesty claim.
        accepts_partial_from=frozenset(),
    ),
    JobSpec(
        "score",
        "Independent probability estimation",
        depends_on=("ingest", "orderbooks"),
        accepts_partial_from=frozenset({"ingest", "orderbooks"}),
        may_partially_succeed=True,
    ),
    JobSpec(
        "rank",
        "Opportunity ranking and recommendation publication",
        depends_on=("score",),
        accepts_partial_from=frozenset({"score"}),
    ),
    JobSpec(
        "arbitrage",
        "Cross-platform and logical arbitrage scan",
        depends_on=("orderbooks",),
        accepts_partial_from=frozenset({"orderbooks"}),
    ),
    JobSpec(
        "snapshot",
        "Immutable daily recommendation record",
        depends_on=("rank",),
        # A snapshot of a partially ranked day is a permanent record of an
        # incomplete day, and it is never revised. Requires a clean rank.
        accepts_partial_from=frozenset(),
    ),
    JobSpec(
        "backtest",
        "Walk-forward backtest over the immutable record",
        depends_on=("snapshot", "settle"),
        accepts_partial_from=frozenset(),
        # A stale backtest is a stale research figure, not a corrupt artefact.
        required_for_publication=False,
    ),
    JobSpec(
        "prune",
        "Orderbook retention",
        depends_on=(),
        required_for_publication=False,
    ),
)

JOB_BY_NAME: dict[str, JobSpec] = {j.name: j for j in PIPELINE}


def execution_order() -> list[str]:
    """Topological order. Raises on a cycle rather than looping forever."""
    resolved: list[str] = []
    pending = {j.name: set(j.depends_on) for j in PIPELINE}
    while pending:
        ready = sorted(n for n, deps in pending.items() if not deps - set(resolved))
        if not ready:
            raise ValueError(f"cycle or missing dependency among: {sorted(pending)}")
        for name in ready:
            resolved.append(name)
            del pending[name]
    return resolved


def blocking_reason(job: JobSpec, upstream_status: dict[str, JobStatus]) -> str | None:
    """Why ``job`` may not run, or None when it may.

    Returns a sentence rather than a boolean because "skipped" with no reason is
    how a job silently stops running and nobody notices for a week.
    """
    for dependency in job.depends_on:
        status = upstream_status.get(dependency)
        if status is None:
            return f"{dependency} did not run"
        if status is JobStatus.PARTIAL_SUCCESS:
            if dependency not in job.accepts_partial_from:
                return (
                    f"{dependency} only partially succeeded, and {job.name} "
                    "requires complete upstream output"
                )
            continue
        if not status.produced_usable_output:
            return f"{dependency} ended {status.value}"
    return None


def publication_blockers(statuses: dict[str, JobStatus]) -> list[str]:
    """Reasons the run must not publish. Empty means it may.

    A job that never ran counts as a blocker. Publishing an artefact whose
    settlement sync silently did not execute produces a track record that grades
    yesterday's calls against nothing.
    """
    problems: list[str] = []
    for spec in PIPELINE:
        if not spec.required_for_publication:
            continue
        status = statuses.get(spec.name)
        if status is None:
            problems.append(f"{spec.name} did not run")
        elif status is JobStatus.PARTIAL_SUCCESS and not spec.may_partially_succeed:
            problems.append(f"{spec.name} only partially succeeded")
        elif not status.produced_usable_output:
            problems.append(f"{spec.name} ended {status.value}")
    return problems
