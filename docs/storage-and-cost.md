# Storage strategy and running costs

## Two stores, different jobs

| | **Operational store** | **Research snapshot** |
|---|---|---|
| Written by | ingest, orderbooks, score, rank, settle | the publication job, once |
| Read by | the pipeline | the public site, backtests, audits |
| Mutability | read-write | immutable once published |
| Lifetime | rolling; pruned at 30 days | kept indefinitely, several retained hot |
| Consistency need | transactional | none — nothing writes to it |

Conflating these is what makes prediction-market sites untrustworthy: if the page
reads the same rows the scanner is mid-update on, a reader can see a recommendation
whose inputs no longer exist. Keeping publication one-directional means the public
artifact is always a coherent point-in-time view of a completed run.

## Why SQLite is retained for both, for now

Postgres is the reflexive answer and is not yet justified. The operational store has
**one writer** — a serial job runner — and readers that are themselves jobs. SQLite's
weakness is concurrent writers, which this workload does not have. Adopting Postgres
now buys concurrency nobody needs and adds a network dependency, a credential to
rotate, a backup to verify and a local-development divergence.

The problems SQLite *does* create here, and how each is handled:

| Problem | Handling |
|---|---|
| **Write concurrency** | One writer by construction; the job runner is serial. WAL mode on the operational store; jobs take the write lock for bounded transactions. |
| **File durability** | The operational DB lives in the CI workspace and is destroyed with it. It is scratch space, not a store — the record is the published snapshot in git. |
| **Deployment persistence** | Solved by not needing it: each run is a **stateless recomputation** seeded from the last published snapshot. See below. |
| **Lock handling** | `busy_timeout` plus serial jobs. A lock contention here indicates a bug, not load. |
| **Backups** | Every published snapshot is a full backup, with compressed and uncompressed identities recorded in git history. Recovery is a normal revert. |
| **Artifact handoff** | Research hands raw+deterministic-gzip candidates to a separate publisher. One exact Git commit changes gzip+manifest together; the public bundle never sees a partial file. |

### The execution model: stateless recomputation

Worth stating plainly, because the alternative reading is flattering and wrong.

A run seeds a scratch database from the last **published** snapshot, computes, and
then either hands a candidate to the publish job or drops it. A `publish=false` run
leaves nothing behind. The next run seeds from the same published snapshot, not from
the previous run's candidate.

```
Published snapshot N
  ├─ run A (publish=false) → candidate A → discarded
  ├─ run B (publish=false) → candidate B → discarded
  └─ run C (publish=true)  → candidate C → committed → Published snapshot N+1
```

So two `publish=false` runs do **not** demonstrate cross-run continuity. Run B does
not inherit run A's job history, rule versions, idempotency keys, settlement updates
or backtest state, because run A wrote none of them anywhere durable. What two such
runs do demonstrate is **deterministic recomputation from the same parent**: the same
parent id, stable rule-version content hashes, stable recommendation identity, no
duplicate rows within a run, and differences explained only by newly observed market
data.

**The only persistence boundary is a published snapshot.** That is a real constraint
of the $0 architecture, not a temporary gap, and it is recorded in every run report
as `execution_model` and `state_persisted_across_runs` rather than left for a reader
to infer from silence.

A persistent operational store would change this, and is deliberately not built in
this pass: it is the Postgres decision below, and it should be made on the triggers
listed there rather than because a stateless model was mistaken for a broken one.

### When to move to Postgres

Written down now so the decision is not made by drift. Move when **any** holds:

1. More than one writer is genuinely required (parallel per-venue ingest).
2. The operational DB exceeds ~2 GB, where artifact upload/download stops being cheap.
3. A run needs to resume mid-flight rather than restart.
4. Point-in-time recovery to an arbitrary instant becomes a requirement.

Migration plan when that day comes: the schema is already SQLAlchemy + Alembic and
avoids SQLite-specific types except `Money`, a `TypeDecorator` that stores Decimal as
`TEXT` and would map to `NUMERIC`. The work is (a) a `Money` variant for Postgres,
(b) `JSONColumn` → `JSONB`, (c) a data copy, (d) keeping SQLite as the local-dev and
snapshot format. Steps (a) and (b) are the only code changes; nothing else in the
application knows which database it is talking to.

## Monthly cost, US dollars

### A — recommended: GitHub Actions + committed artifact

| Item | Low (daily) | Moderate (every 15 min) | High-frequency (every 5 min) |
|---|---|---|---|
| Compute | $0 public repo · ~60 min/mo | $0 public · ~2,900 min/mo | $0 public · ~8,700 min/mo |
| — if private repo | $0 (within 2,000 free) | **~$7.20** (900 min over) | **~$53.60** (6,700 min over) |
| Database | $0 (SQLite in repo) | $0 | $0 |
| Storage | $0 (artifact ~8 MB, git) | ~$0 | ~$0 (watch repo growth) |
| Bandwidth | $0 Vercel Hobby | $0 | $0 |
| Scheduled jobs | $0 | $0 | $0 |
| Monitoring | $0 (Actions logs) | $0 | $0 |
| Backups | $0 (git history) | $0 | $0 |
| **Total** | **$0** | **$0 public / ~$7 private** | **$0 public / ~$54 private** |

Assumes ~2 min per run at $0.008/min for Linux runners over the free 2,000 minutes.

**A caveat that matters at high frequency:** gzip reduces each committed object but
does not make Git an object store. Frequent immutable binaries still grow repository
history without bound. Before materially increasing publication frequency or
Snapshot size, move blobs to content-addressed object storage or release assets
under the integrity and rollback contract in
[the compressed publication backlog](compressed-snapshot-publication.md#backlog-move-snapshot-blobs-out-of-git).

### B — small VM + managed Postgres

| Item | Low | Moderate | High-frequency |
|---|---|---|---|
| Compute | $6 (1 GB VPS) | $12 (2 GB) | $12 |
| Database | $0 (local PG) | $15 (managed, 1 GB) | $15 |
| Storage | $0 (included) | $0 | ~$2 |
| Bandwidth | $0 | $0 | $0 |
| Monitoring | $0 (self-hosted) | $0 | $0 |
| Backups | $1.20 (snapshots) | $3 (managed) | $3 |
| **Total** | **~$7/mo** | **~$30/mo** | **~$32/mo** |

### C — Vercel Cron + serverless

Not priced in detail: ingest does not fit the 60 s Hobby limit, so the architecture
does not work at any price without being restructured into a queue.

### Recommendation

Start on **A at the 15-minute cadence, $0/month** on a public repository. It buys the
freshness the product actually claims while keeping the artifact-based safety
property and provisioning nothing.

**No paid resource is created by this branch.** The workflows commit nothing but the
schedule; moving to B is a decision for the repository owner, and the numbers above
exist so that decision can be made on evidence.

## Rollback

| Failure | Recovery | Time |
|---|---|---|
| Bad snapshot published | `git revert` the artifact commit, redeploy | ~2 min |
| Pipeline broken, data stale | Nothing to do — the previous artifact is still served and labelled stale | 0 |
| Migration wrong | `alembic downgrade -1`, re-run | ~1 min |
| Both Vercel projects wrong | Vercel dashboard rollback to a previous production deployment | ~1 min |
| Total loss of operational DB | Rebuild from the last published snapshot; lose only rows since it | one run |

The frozen snapshot committed at `e2b7515` remains the disaster-recovery path and is
not removed by this work.
