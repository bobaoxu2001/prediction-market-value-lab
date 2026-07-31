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
PUBLISHED_GZIP = ROOT / "data" / "pmvl-snapshot.db.gz"
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
    # ``candidate_sha256`` remains the canonical uncompressed checksum for
    # compatibility with existing reports.  The explicit fields prevent a
    # publisher from confusing the validated SQLite bytes with their gzip
    # transport representation.
    candidate_sha256: str | None = None
    candidate_uncompressed_sha256: str | None = None
    candidate_compressed_sha256: str | None = None
    candidate_uncompressed_size_bytes: int | None = None
    candidate_compressed_size_bytes: int | None = None
    parent_resolved_path: str = field(default="", repr=False)
    #: Row counts either side of the run. The incident was invisible because the
    #: report said SUCCESS nine times and never said what the candidate contained
    #: relative to what it started from.
    #: Which database each stage actually used. The Canary A incident was
    #: invisible because nothing recorded that the jobs and the candidate were
    #: looking at different files.
    database_binding: dict[str, str] = field(default_factory=dict)
    finalization: dict[str, Any] = field(default_factory=dict)
    current_run_job_statuses_in_operational: dict[str, str] = field(default_factory=dict)
    current_run_job_statuses_in_candidate: dict[str, str] = field(default_factory=dict)
    non_terminal_jobs_in_operational: list[str] = field(default_factory=list)
    non_terminal_jobs_in_candidate: list[str] = field(default_factory=list)
    job_status_report_candidate_match: bool = False
    parent_row_counts: dict[str, int] = field(default_factory=dict)
    operational_row_counts: dict[str, int] = field(default_factory=dict)
    candidate_row_counts: dict[str, int] = field(default_factory=dict)
    candidate_disposition: str = CandidateDisposition.NOT_BUILT
    published: bool = False
    publication_blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
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
            "candidate_uncompressed_sha256": self.candidate_uncompressed_sha256,
            "candidate_compressed_sha256": self.candidate_compressed_sha256,
            "candidate_uncompressed_size_bytes": self.candidate_uncompressed_size_bytes,
            "candidate_compressed_size_bytes": self.candidate_compressed_size_bytes,
            "database_binding": self.database_binding,
            "finalization": self.finalization,
            "current_run_job_statuses_in_operational":
                self.current_run_job_statuses_in_operational,
            "current_run_job_statuses_in_candidate":
                self.current_run_job_statuses_in_candidate,
            "non_terminal_jobs_in_operational": self.non_terminal_jobs_in_operational,
            "non_terminal_jobs_in_candidate": self.non_terminal_jobs_in_candidate,
            "job_status_report_candidate_match": self.job_status_report_candidate_match,
            "parent_row_counts": self.parent_row_counts,
            "operational_row_counts": self.operational_row_counts,
            "candidate_row_counts": self.candidate_row_counts,
            "candidate_matches_parent": (
                bool(self.parent_row_counts)
                and self.parent_row_counts == self.candidate_row_counts
            ),
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
    # Self-contained: callers other than main() (tests, a future bootstrap tool)
    # must not have to know that this function needs its directory pre-made.
    work_dir.mkdir(parents=True, exist_ok=True)
    operational = work_dir / "pmvl-operational.db"

    if PUBLISHED_MANIFEST.exists():
        from pmvl_shared.snapshot_artifact import (
            SnapshotArtifactError,
            resolve_snapshot_path,
        )

        try:
            published = resolve_snapshot_path(
                PUBLISHED_MANIFEST,
                PUBLISHED_DB,
            )
        except SnapshotArtifactError as exc:
            # A corrupt parent is not something to work around silently: every
            # downstream row would inherit its provenance.
            raise PipelineError(
                "the published snapshot failed verification and cannot seed a run: "
                f"{exc}"
            ) from exc

        manifest = json.loads(PUBLISHED_MANIFEST.read_text())
        outcome.parent_snapshot_id = manifest.get("snapshot_id")
        outcome.parent_snapshot_sha256 = (
            manifest.get("uncompressed_sha256") or manifest.get("sha256")
        )
        outcome.parent_resolved_path = str(published)
        outcome.operational_init_source = "published_snapshot"
        # The resolved cache is deliberately read-only.  A pipeline run needs its
        # own writable operational copy, so do not preserve cache permissions.
        shutil.copyfile(published, operational)
        operational.chmod(0o600)
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


def _bind_database(db_path: Path) -> dict[str, str]:
    """Make every later import agree on which database this run uses.

    Setting the environment variable is not enough on its own: `get_settings()`
    is `@lru_cache(maxsize=1)`, so whichever call materialises it first wins for
    the whole process. Alembic's env.py calls it, which is why migrating before
    binding silently sent every job to the default database.

    The cache is cleared as well as the variable set, so a caller that has
    already read settings for some other reason cannot leave a stale binding
    behind.
    """
    from pmvl_shared.config import get_settings
    from pmvl_shared.db import reset_engine

    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db_path}"
    os.environ["PMVL_PIPELINE_RUN"] = "1"
    get_settings.cache_clear()
    # The engine is a module-level global built once from whatever settings said
    # at the time, so clearing the settings cache alone leaves a connection
    # pointing at the old database. Both layers have to be reset or the fix only
    # looks like it worked.
    reset_engine()

    bound = get_settings().database_url
    if str(db_path) not in bound:
        raise PipelineError(
            f"settings bound to {bound!r} rather than the operational database "
            f"{db_path}. Jobs would write somewhere the candidate is not built "
            "from, and the run would look successful."
        )

    # The engine is what the jobs will actually connect through. Verifying only
    # settings would pass while a stale engine still pointed elsewhere - which is
    # precisely how the first version of this fix looked correct and was not.
    from pmvl_shared.db import get_engine

    engine_url = str(get_engine().url)
    if str(db_path) not in engine_url:
        raise PipelineError(
            f"engine bound to {engine_url!r} rather than {db_path}. Settings were "
            "corrected but the connection was not."
        )

    return {
        "requested_operational_db": str(db_path),
        "settings_database_url": bound,
        "engine_url": engine_url,
    }


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

    # Quiesce and checkpoint BEFORE anything reads the file. The builder runs as a
    # subprocess; while this process still holds a WAL connection, whatever it
    # copies is missing every transaction since the last automatic checkpoint.
    from pmvl_shared.db.finalize import FinalizationError, finalize_operational_database

    run_jobs_seen = set(outcome.statuses)
    try:
        finalization = finalize_operational_database(
            operational, run_job_names=run_jobs_seen
        )
    except FinalizationError as exc:
        raise PipelineError(f"operational database not safe to build from: {exc}") from exc

    outcome.finalization = finalization.as_dict()
    outcome.current_run_job_statuses_in_operational = {
        name: status
        for name, status in finalization.job_statuses.items()
        if name in run_jobs_seen
    }
    outcome.non_terminal_jobs_in_operational = finalization.non_terminal_jobs
    print(
        f"finalised: journal {finalization.journal_mode_before} -> "
        f"{finalization.journal_mode_after}, integrity {finalization.integrity}, "
        f"wal removed {finalization.wal_removed}"
    )

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
        [
            sys.executable,
            str(ROOT / "scripts/validate_snapshot.py"),
            "--snapshot",
            str(candidate),
            "--finalize-candidate",
        ],
        env=env, capture_output=True, text=True,
    )
    if validation.returncode != 0:
        raise PipelineError(f"candidate failed validation: {validation.stdout[-800:]}")
    compressed = candidate.with_name(candidate.name + ".gz")
    if not compressed.exists():
        raise PipelineError(
            "candidate validation passed without producing its deterministic gzip "
            "representation"
        )

    # The candidate must agree with the report about this run's own jobs. A
    # disagreement means the artefact describes a run that did not happen.
    from pmvl_shared.db.finalize import job_states_in

    candidate_states, candidate_non_terminal = job_states_in(candidate, run_jobs_seen)
    outcome.current_run_job_statuses_in_candidate = {
        name: status for name, status in candidate_states.items() if name in run_jobs_seen
    }
    outcome.non_terminal_jobs_in_candidate = candidate_non_terminal
    outcome.job_status_report_candidate_match = all(
        outcome.current_run_job_statuses_in_candidate.get(name) == status.value
        for name, status in outcome.statuses.items()
    )

    if candidate_non_terminal:
        raise PipelineError(
            "the candidate contains jobs from this run that are still "
            f"non-terminal: {', '.join(candidate_non_terminal)}. This is the "
            "stale-copy failure; refusing to treat it as publishable."
        )
    if not outcome.job_status_report_candidate_match:
        raise PipelineError(
            "the candidate's job statuses disagree with the run report. "
            f"report={ {k: v.value for k, v in outcome.statuses.items()} } "
            f"candidate={outcome.current_run_job_statuses_in_candidate}"
        )

    outcome.candidate_path = candidate
    outcome.database_binding["candidate_source_db"] = str(operational)
    outcome.parent_row_counts = _row_counts(
        Path(outcome.parent_resolved_path)
        if outcome.parent_resolved_path
        else PUBLISHED_DB
    )
    outcome.operational_row_counts = _row_counts(operational)
    outcome.candidate_row_counts = _row_counts(candidate)
    if outcome.parent_row_counts and outcome.parent_row_counts == outcome.candidate_row_counts:
        # Not fatal on its own - a genuinely quiet interval could produce this -
        # but it is exactly what a mis-bound database looks like, and it went
        # unnoticed for a full production run precisely because nothing said it.
        print(
            "WARNING: the candidate has identical row counts to its parent in "
            "every table. If jobs ran, they may have written to a different "
            "database than the candidate was built from."
        )
    return candidate


def _row_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def _record_candidate_identity(candidate: Path, outcome: RunOutcome) -> None:
    """Read the candidate's own manifest so the run report can name it."""
    from pmvl_shared.manifest import sha256_of

    manifest_path = candidate.with_suffix(".manifest.json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        outcome.candidate_snapshot_id = manifest.get("snapshot_id")
        outcome.candidate_uncompressed_sha256 = (
            manifest.get("uncompressed_sha256") or manifest.get("sha256")
        )
        outcome.candidate_compressed_sha256 = manifest.get("compressed_sha256")
        outcome.candidate_uncompressed_size_bytes = (
            manifest.get("uncompressed_size_bytes")
            or manifest.get("file_size_bytes")
        )
        outcome.candidate_compressed_size_bytes = manifest.get(
            "compressed_size_bytes"
        )
    outcome.candidate_sha256 = sha256_of(candidate)
    outcome.candidate_uncompressed_sha256 = (
        outcome.candidate_uncompressed_sha256 or outcome.candidate_sha256
    )


def _emit_candidate(candidate: Path, out_dir: Path, outcome: RunOutcome) -> Path:
    """Copy the validated candidate bundle somewhere a later job can pick it up.

    The manifest is written with ``release_status: held``. It becomes ``published``
    only after the git commit succeeds - marking it published here would produce an
    artefact asserting a publication that had not happened, and a publish job that
    later failed would leave that assertion standing.
    """
    from pmvl_shared.manifest import ReleaseStatus

    out_dir.mkdir(parents=True, exist_ok=True)
    db_out = out_dir / "pmvl-snapshot.db"
    gzip_out = out_dir / "pmvl-snapshot.db.gz"
    manifest_out = out_dir / "pmvl-snapshot.manifest.json"

    shutil.copy2(candidate, db_out)
    candidate_gzip = candidate.with_name(candidate.name + ".gz")
    if not candidate_gzip.exists():
        raise PipelineError("validated candidate has no gzip representation")
    shutil.copy2(candidate_gzip, gzip_out)
    manifest = json.loads(candidate.with_suffix(".manifest.json").read_text())
    manifest["release_status"] = ReleaseStatus.HELD
    manifest["parent_snapshot_id"] = outcome.parent_snapshot_id
    manifest["parent_snapshot_sha256"] = outcome.parent_snapshot_sha256
    manifest["pipeline_run_id"] = outcome.run_id
    manifest["workflow_run_id"] = os.environ.get("GITHUB_RUN_ID", "")
    manifest["workflow_run_attempt"] = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    manifest_out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    from pmvl_shared.snapshot_artifact import verify_compressed_snapshot

    problems = verify_compressed_snapshot(db_out, gzip_out, manifest_out)
    if problems:
        raise PipelineError(
            "emitted candidate bundle failed verification: " + "; ".join(problems)
        )
    _record_candidate_identity(db_out, outcome)
    outcome.candidate_disposition = CandidateDisposition.UPLOADED_FOR_PUBLISH
    return db_out


def promote(candidate: Path, outcome: RunOutcome) -> None:
    """Promote a validated candidate into the local published representation.

    Production publication is the single Git commit in ``pipeline.yml``.  This
    helper remains for local/manual runs and mirrors the same format transition.
    A legacy raw candidate is still supported for backward-compatible tests and
    rollback tooling; every newly built candidate carries deterministic gzip.
    """
    from pmvl_shared.manifest import ReleaseStatus

    candidate_manifest = candidate.with_suffix(".manifest.json")
    if not candidate_manifest.exists():
        raise PipelineError("candidate has no manifest; refusing to promote")

    manifest = json.loads(candidate_manifest.read_text())
    candidate_gzip = candidate.with_name(candidate.name + ".gz")
    _record_candidate_identity(candidate, outcome)
    if manifest.get("artifact_encoding") == "gzip":
        if not candidate_gzip.exists():
            raise PipelineError("gzip candidate manifest has no compressed artefact")
        from pmvl_shared.snapshot_artifact import verify_compressed_snapshot

        problems = verify_compressed_snapshot(
            candidate, candidate_gzip, candidate_manifest
        )
        if problems:
            raise PipelineError(
                "candidate bundle failed verification: " + "; ".join(problems)
            )
        # The validator deliberately leaves every candidate HELD. Local/manual
        # promotion has no later Git commit step to flip that boundary, so do it
        # only after the exact bundle has passed its final verification and
        # immediately before installing the pair.
        manifest["release_status"] = ReleaseStatus.PUBLISHED
        candidate_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        os.replace(candidate_gzip, PUBLISHED_GZIP)
        os.replace(candidate_manifest, PUBLISHED_MANIFEST)
        PUBLISHED_DB.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)
    else:
        # Legacy rollback path.
        os.replace(candidate, PUBLISHED_DB)
        os.replace(candidate_manifest, PUBLISHED_MANIFEST)
    outcome.published = True
    outcome.candidate_disposition = CandidateDisposition.PUBLISHED


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

        # Point the process at the operational database BEFORE anything reads
        # settings. `get_settings()` is lru_cached, and alembic's env.py calls it,
        # so migrating first froze the cached Settings on the default path. Every
        # job then wrote to that default database while the candidate was built
        # from the untouched copy - a run that ingested 7,666 markets and produced
        # a candidate byte-identical to its parent, reporting success throughout.
        outcome.database_binding = _bind_database(operational)
        migrate(operational, outcome)
        outcome.database_binding["migration_target_db"] = str(operational)
        print(
            "database binding: "
            + json.dumps(outcome.database_binding, sort_keys=True)
        )
        print(
            f"migrations: {outcome.migration_start_revision} -> "
            f"{outcome.migration_end_revision}"
        )

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
