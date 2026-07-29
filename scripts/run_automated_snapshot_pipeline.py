"""One run of the automated snapshot pipeline, start to finish.

This is the whole job. The GitHub workflow is a thin caller so that the logic is
version-controlled, reviewable, and - the point - runnable in PR CI before it is
ever merged. `workflow_dispatch` and `schedule` only fire from the default branch,
so a workflow that carries its own logic cannot be tested until after it lands,
which is precisely when a mistake is most expensive.

Four failures this replaces, each of which produced a plausible-looking artefact:

**Scanning 50 markets and calling it the universe.** The workflow passed
``--market-limit "${{ inputs.market_limit || '50' }}"``. A scheduled event has no
``inputs``, so every scheduled run silently capped at 50 of ~2,300 markets. Scope
is now resolved from the event name explicitly and printed before ingestion.

**Starting from an empty database.** Each run migrated a fresh
``pmvl-operational.db`` and uploaded it as an artefact that the next run never
restored. Every scheduled run therefore began with no recommendations, no
settlements, no job history, no rule versions and no idempotency record - and
would have published an artefact whose track record started that morning. The
operational store is now seeded from the last validated published snapshot.

**Running three of nine jobs.** ingest, rank and arbitrage - no orderbook refresh,
no settlement sync, no scoring, no immutable daily record, no backtest, no
retention. The DAG in ``pmvl_shared.pipeline_dag`` decides what runs and what a
degraded upstream means for each downstream job.

**Publishing non-atomically.** The candidate is built in a temporary directory and
only renamed into place after it validates, so a failed run leaves the previously
published pair untouched rather than half-written.

**The execution model, stated plainly.** Every run is a *stateless recomputation
from the latest validated published snapshot*. A run with ``publish=false``
computes a candidate and then lets it go; the next run starts from the same
published parent, not from that candidate. Two such runs therefore demonstrate
deterministic recomputation and stable idempotent output - they do NOT demonstrate
that the second inherited the first's job history, rule versions, settlements or
idempotency keys, because it did not. The only thing that carries state across
runs is a snapshot somebody published.

That is a real constraint of the $0 architecture and it is written into the run
report rather than left for a reader to infer. A stateless recomputation described
as continuity is the same class of overclaim as a configured cadence described as
a running one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "packages/shared/src"),
    str(ROOT / "packages/market-normalization/src"),
    str(ROOT / "services/api/src"),
    str(ROOT / "services/worker/src"),
]

from pmvl_shared.enums import JobStatus  # noqa: E402
from pmvl_shared.pipeline_dag import (  # noqa: E402
    JOB_BY_NAME,
    blocking_reason,
    execution_order,
    publication_blockers,
)

PUBLISHED_DB = ROOT / "data" / "pmvl-snapshot.db"
PUBLISHED_MANIFEST = ROOT / "data" / "pmvl-snapshot.manifest.json"

#: Manual smoke runs stay small so a human waiting on a dispatch gets an answer.
#: A scheduled research run has no cap: capping it is how the pipeline came to
#: describe 50 markets as the market universe.
SMOKE_MARKET_LIMIT = 50
CI_SMOKE_MARKET_LIMIT = 20


class PipelineError(RuntimeError):
    """A condition that must stop the run rather than degrade it."""


class CandidateDisposition:
    """What became of the artefact this run produced.

    Three outcomes, and only one of them persists anything. Naming them keeps the
    report from implying that a computed-then-dropped candidate left a trace.
    """

    #: Computed, validated, then let go. Nothing outside this run changed.
    DISCARDED = "discarded"
    #: Written to a durable path for a separate publish job to collect. Still not
    #: published: the repository is unchanged until that job commits.
    UPLOADED_FOR_PUBLISH = "uploaded_for_publish"
    #: Promoted into the published pair.
    PUBLISHED = "published"
    #: Never built, because a required job failed or did not run.
    NOT_BUILT = "not_built"


#: Every run is a fresh computation from the last published snapshot. Recorded in
#: the report so nobody has to infer it from the absence of a contrary statement.
EXECUTION_MODEL = "stateless_recompute_from_published_snapshot"


@dataclass
class Scope:
    """What this run is allowed to do, resolved from the triggering event."""

    event: str
    market_limit: int | None
    publish: bool
    bootstrap_allowed: bool

    @property
    def label(self) -> str:
        limit = "unlimited" if self.market_limit is None else str(self.market_limit)
        return (
            f"event={self.event} scope={'smoke' if self.market_limit else 'full'} "
            f"market_limit={limit} publish={self.publish} "
            f"bootstrap_allowed={self.bootstrap_allowed}"
        )


def resolve_scope(
    *,
    event_name: str,
    input_scope: str | None,
    input_market_limit: str | None,
    input_publish: str | None,
) -> Scope:
    """Decide the run's scope from the event, never from a truthy expression.

    ``${{ inputs.x || 'default' }}`` reads as "use the default when absent". On a
    scheduled event every input is absent, so that expression silently applied the
    manual smoke default to production runs. Branching on the event name makes the
    scheduled case a deliberate decision instead of a fallback.
    """
    if event_name == "workflow_dispatch":
        scope = (input_scope or "smoke").strip().lower()
        if scope not in ("smoke", "full"):
            raise PipelineError(f"unknown scope {scope!r}; expected 'smoke' or 'full'")
        if scope == "full":
            limit = None
        else:
            limit = int(input_market_limit) if input_market_limit else SMOKE_MARKET_LIMIT
        return Scope(
            event=event_name,
            market_limit=limit,
            publish=(input_publish or "").strip().lower() == "true",
            # Only a human dispatch may start from nothing. A scheduled run that
            # cannot find its parent must fail rather than quietly rebuild history
            # from an empty database.
            bootstrap_allowed=True,
        )

    if event_name == "schedule":
        return Scope(
            event=event_name,
            market_limit=None,
            # Publication is a separate, lower-frequency decision. A research run
            # never publishes on its own.
            publish=False,
            bootstrap_allowed=False,
        )

    # pull_request and anything else: the smallest safe run, never publishing.
    return Scope(
        event=event_name,
        market_limit=CI_SMOKE_MARKET_LIMIT,
        publish=False,
        bootstrap_allowed=True,
    )


@dataclass
class RunOutcome:
    run_id: str
    scope: Scope
    statuses: dict[str, JobStatus] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    parent_snapshot_id: str | None = None
    parent_snapshot_sha256: str | None = None
    operational_init_source: str = ""
    migration_start_revision: str | None = None
    migration_end_revision: str | None = None
    candidate_path: Path | None = None
    candidate_snapshot_id: str | None = None
    candidate_sha256: str | None = None
    candidate_disposition: str = CandidateDisposition.NOT_BUILT
    published: bool = False
    publication_blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scope": self.scope.label,
            "statuses": {k: v.value for k, v in self.statuses.items()},
            "skipped": self.skipped,
            "parent_snapshot_id": self.parent_snapshot_id,
            "parent_snapshot_sha256": self.parent_snapshot_sha256,
            "operational_init_source": self.operational_init_source,
            "migration_start_revision": self.migration_start_revision,
            "migration_end_revision": self.migration_end_revision,
            # The execution model is stated, not implied. A reader must not have to
            # deduce from an absence that this run inherited nothing.
            "execution_model": EXECUTION_MODEL,
            "state_persisted_across_runs": False,
            "candidate_snapshot_id": self.candidate_snapshot_id,
            "candidate_sha256": self.candidate_sha256,
            "candidate_disposition": self.candidate_disposition,
            "published": self.published,
            "publication_eligible": self.publication_eligible,
            "publication_blockers": self.publication_blockers,
        }

    @property
    def publication_eligible(self) -> bool:
        """Whether a separate publish job would be permitted to promote this.

        Eligibility is a property of the candidate, not of the run's intent: a
        research run that was never asked to publish can still produce a candidate
        that WOULD be publishable, and the publish job needs to know that.
        """
        return (
            not self.publication_blockers
            and self.candidate_disposition
            in (CandidateDisposition.UPLOADED_FOR_PUBLISH, CandidateDisposition.PUBLISHED)
        )


# --------------------------------------------------------------- initialisation
def _alembic_revision(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def initialise_operational_db(work_dir: Path, outcome: RunOutcome) -> Path:
    """Seed the run's read-write database from the last validated snapshot.

    Without this every scheduled run started empty, so the published artefact's
    track record, settlement history, job history and rule versions all began that
    morning - and nothing on the page would have said so.

    The published file is never opened read-write; it is copied first.
    """
    from pmvl_shared.manifest import verify_artifact

    # Self-contained: callers other than main() (tests, a future bootstrap tool)
    # must not have to know that this function needs its directory pre-made.
    work_dir.mkdir(parents=True, exist_ok=True)
    operational = work_dir / "pmvl-operational.db"

    if PUBLISHED_DB.exists() and PUBLISHED_MANIFEST.exists():
        problems = verify_artifact(PUBLISHED_DB, PUBLISHED_MANIFEST)
        if problems:
            # A corrupt parent is not something to work around silently: every
            # downstream row would inherit its provenance.
            raise PipelineError(
                "the published snapshot failed verification and cannot seed a run: "
                + "; ".join(problems)
            )
        manifest = json.loads(PUBLISHED_MANIFEST.read_text())
        outcome.parent_snapshot_id = manifest.get("snapshot_id")
        outcome.parent_snapshot_sha256 = manifest.get("sha256")
        outcome.operational_init_source = "published_snapshot"
        shutil.copy2(PUBLISHED_DB, operational)
        return operational

    if not outcome.scope.bootstrap_allowed:
        raise PipelineError(
            "no validated published snapshot to initialise from. A scheduled run "
            "fails closed here rather than starting empty and publishing an "
            "artefact whose history begins today. Dispatch a manual bootstrap run "
            "if this is genuinely the first one."
        )
    outcome.operational_init_source = "bootstrap_empty"
    return operational


def migrate(db_path: Path, outcome: RunOutcome) -> None:
    from alembic import command
    from alembic.config import Config

    outcome.migration_start_revision = _alembic_revision(db_path)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "packages/shared/src/pmvl_shared/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    command.upgrade(cfg, "head")
    outcome.migration_end_revision = _alembic_revision(db_path)


# ------------------------------------------------------------------- job running
async def _invoke(name: str, scope: Scope) -> None:
    from pmvl_worker import jobs

    if name == "ingest":
        await jobs.job_ingest(market_limit=scope.market_limit)
    elif name == "orderbooks":
        await jobs.job_orderbooks(limit=scope.market_limit)
    elif name == "score":
        await jobs.job_score(limit=scope.market_limit)
    elif name == "rank":
        await jobs.job_rank(limit=scope.market_limit)
    elif name == "settle":
        await jobs.job_settle()
    elif name == "arbitrage":
        jobs.job_arbitrage()
    elif name == "snapshot":
        jobs.job_snapshot()
    elif name == "backtest":
        jobs.job_backtest()
    elif name == "prune":
        jobs.job_prune()
    else:  # pragma: no cover - the DAG and this function are asserted equal
        raise PipelineError(f"no implementation for job {name!r}")


def _recorded_status(name: str) -> JobStatus:
    """Read back what the job runner recorded, rather than assuming success.

    The runner derives PARTIAL_SUCCESS from provider outcomes, so the only way to
    learn a run was degraded is to ask it.
    """
    from sqlalchemy import select

    from pmvl_shared.db import session_scope

    from pmvl_markets.db_models import JobRun

    with session_scope() as db:
        row = db.scalar(
            select(JobRun).where(JobRun.job_name == name).order_by(JobRun.started_at.desc()).limit(1)
        )
        return JobStatus(row.status) if row else JobStatus.FAILED


async def run_jobs(scope: Scope, outcome: RunOutcome) -> None:
    for name in execution_order():
        spec = JOB_BY_NAME[name]
        reason = blocking_reason(spec, outcome.statuses)
        if reason:
            # Skipped WITH a reason. A silent skip is how a job stops running and
            # nobody notices for a week.
            outcome.skipped[name] = reason
            print(f"  SKIP {name:11} {reason}")
            continue
        try:
            await _invoke(name, scope)
            outcome.statuses[name] = _recorded_status(name)
        except Exception as exc:  # noqa: BLE001 - one job must not kill the run
            outcome.statuses[name] = JobStatus.FAILED
            print(f"  FAIL {name:11} {type(exc).__name__}: {exc}")
            continue
        print(f"  {outcome.statuses[name].value.upper():16} {name}")


# -------------------------------------------------------------------- publication
def build_and_validate_candidate(work_dir: Path, operational: Path, outcome: RunOutcome) -> Path:
    """Build the candidate artefact in a temporary path and validate it there.

    Nothing touches the published pair until validation has passed, so a failed
    run cannot leave a half-written database behind a manifest that describes a
    different one.
    """
    import subprocess

    candidate = work_dir / "candidate.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+pysqlite:///{operational}"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_snapshot.py"),
         "--source", str(operational), "--target", str(candidate)],
        env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise PipelineError(f"snapshot build failed: {result.stderr[-800:]}")

    validation = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_snapshot.py"), "--snapshot", str(candidate)],
        env=env, capture_output=True, text=True,
    )
    if validation.returncode != 0:
        raise PipelineError(f"candidate failed validation: {validation.stdout[-800:]}")

    outcome.candidate_path = candidate
    return candidate


def _record_candidate_identity(candidate: Path, outcome: RunOutcome) -> None:
    """Read the candidate's own manifest so the run report can name it."""
    from pmvl_shared.manifest import sha256_of

    manifest_path = candidate.with_suffix(".manifest.json")
    if manifest_path.exists():
        outcome.candidate_snapshot_id = json.loads(manifest_path.read_text()).get(
            "snapshot_id"
        )
    outcome.candidate_sha256 = sha256_of(candidate)


def _emit_candidate(candidate: Path, out_dir: Path, outcome: RunOutcome) -> Path:
    """Copy the validated candidate pair somewhere a later job can pick it up.

    The manifest is written with ``release_status: held``. It becomes ``published``
    only after the git commit succeeds - marking it published here would produce an
    artefact asserting a publication that had not happened, and a publish job that
    later failed would leave that assertion standing.
    """
    from pmvl_shared.manifest import ReleaseStatus

    out_dir.mkdir(parents=True, exist_ok=True)
    db_out = out_dir / "pmvl-snapshot.db"
    manifest_out = out_dir / "pmvl-snapshot.manifest.json"

    shutil.copy2(candidate, db_out)
    manifest = json.loads(candidate.with_suffix(".manifest.json").read_text())
    manifest["release_status"] = ReleaseStatus.HELD
    manifest["parent_snapshot_id"] = outcome.parent_snapshot_id
    manifest["parent_snapshot_sha256"] = outcome.parent_snapshot_sha256
    manifest["pipeline_run_id"] = outcome.run_id
    manifest["workflow_run_id"] = os.environ.get("GITHUB_RUN_ID", "")
    manifest_out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    _record_candidate_identity(db_out, outcome)
    outcome.candidate_disposition = CandidateDisposition.UPLOADED_FOR_PUBLISH
    return db_out


def promote(candidate: Path, outcome: RunOutcome) -> None:
    """Atomically replace the published pair.

    The database is renamed FIRST and the manifest second. If the process dies
    between the two, the manifest still describes the previous database and
    `verify_artifact` reports a checksum mismatch - a loud, detectable state. The
    reverse order would leave a manifest asserting a validated artefact that is
    not the one on disk, which verifies clean and is wrong.
    """
    candidate_manifest = candidate.with_suffix(".manifest.json")
    if not candidate_manifest.exists():
        raise PipelineError("candidate has no manifest; refusing to promote")

    # os.replace is atomic within a filesystem; the temporary directory is placed
    # alongside the target for that reason.
    os.replace(candidate, PUBLISHED_DB)
    os.replace(candidate_manifest, PUBLISHED_MANIFEST)
    outcome.published = True
    outcome.candidate_disposition = CandidateDisposition.PUBLISHED
    _record_candidate_identity(PUBLISHED_DB, outcome)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "manual"))
    parser.add_argument("--scope", default=os.environ.get("INPUT_SCOPE"))
    parser.add_argument("--market-limit", default=os.environ.get("INPUT_MARKET_LIMIT"))
    parser.add_argument("--publish", default=os.environ.get("INPUT_PUBLISH"))
    parser.add_argument("--work-dir", default=None)
    parser.add_argument(
        "--report", default=None, help="write the run outcome as JSON to this path"
    )
    parser.add_argument(
        "--candidate-out",
        default=None,
        help=(
            "emit the validated candidate pair here for a separate publish job. "
            "Implies the candidate is HELD, not published."
        ),
    )
    args = parser.parse_args(argv)

    scope = resolve_scope(
        event_name=args.event,
        input_scope=args.scope,
        input_market_limit=args.market_limit,
        input_publish=args.publish,
    )
    outcome = RunOutcome(run_id=uuid.uuid4().hex[:16], scope=scope)

    # Printed BEFORE ingestion, so a run that scanned the wrong universe is
    # visible in the log at the top rather than inferred from row counts later.
    print(f"pipeline scope: {scope.label}")

    owned_dir = args.work_dir is None
    work_dir = Path(args.work_dir) if args.work_dir else Path(
        tempfile.mkdtemp(prefix="pmvl-pipeline-", dir=str(ROOT / "data"))
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        operational = initialise_operational_db(work_dir, outcome)
        print(f"operational store: {outcome.operational_init_source}")
        migrate(operational, outcome)
        print(
            f"migrations: {outcome.migration_start_revision} -> "
            f"{outcome.migration_end_revision}"
        )

        os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{operational}"
        os.environ["PMVL_PIPELINE_RUN"] = "1"
        asyncio.run(run_jobs(scope, outcome))

        outcome.publication_blockers = publication_blockers(outcome.statuses)
        for name in JOB_BY_NAME:
            if name not in outcome.statuses and name not in outcome.skipped:
                outcome.publication_blockers.append(f"{name} did not run at all")

        if outcome.publication_blockers:
            outcome.candidate_disposition = CandidateDisposition.NOT_BUILT
            print("\npublication blocked:")
            for blocker in outcome.publication_blockers:
                print(f"   {blocker}")
        else:
            candidate = build_and_validate_candidate(work_dir, operational, outcome)
            print(f"candidate validated: {candidate.name}")

            if args.candidate_out:
                # Copied to a durable path so a SEPARATE publish job can collect
                # it. The research job holds a read-only token and must not be the
                # thing that writes to the repository; handing the artefact over is
                # what makes that separation possible.
                destination = _emit_candidate(candidate, Path(args.candidate_out), outcome)
                print(f"candidate emitted for publication: {destination}")
            elif scope.publish:
                promote(candidate, outcome)
                print("published")
            else:
                # Not "discarded" as an aside - this is the execution model. The
                # next run starts from the published parent, not from this.
                outcome.candidate_disposition = CandidateDisposition.DISCARDED
                print(
                    "candidate discarded: this run is a stateless recomputation "
                    "and persists nothing. The next run starts from the same "
                    "published snapshot."
                )

        if args.report:
            Path(args.report).write_text(json.dumps(outcome.as_dict(), indent=2) + "\n")

        failed = [n for n, s in outcome.statuses.items() if s is JobStatus.FAILED]
        return 1 if failed else 0
    finally:
        if owned_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
