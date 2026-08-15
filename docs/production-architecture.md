# Production pipeline architecture

## What exists today

The automated pipeline this document's first draft described as a target has been
live since mid-August 2026. Production is no longer a photograph of a developer's
database refreshed by hand; it is a scheduled GitHub Actions pipeline whose only
human step is the decision to publish.

```
┌──────────────────────── GitHub Actions (public repo) ────────────────────┐
│                                                                          │
│  schedule: hourly (cron 17 * * * *)  ──►  research job (contents: read)  │
│    runs the full pipeline script: ingest -> rank -> arbitrage ->         │
│    snapshot -> settle -> backtest, against public venue endpoints,       │
│    seeded from the parent snapshot                                        │
│                                            │                             │
│                                   validate + checksum (held candidate)   │
│                                            │                             │
│                                            ▼                             │
│                              upload candidate artifact (raw + gzip +     │
│                              manifest + run report)                      │
└────────────────────────────────────────────┬────────────────────────────┘
                                             │  workflow_dispatch, publish=true
                                             ▼
                      publish job (contents: write, main only)
                      revalidates the exact candidate, verifies main has not
                      moved, commits gzip + manifest together, pushes
                                             │
                                             ▼
                                    Vercel (git-triggered deploy)
                                    pmvl-api  ──reads──►  committed gzip snapshot
                                    pmvl-web
```

Every scheduled run is read-only and can never publish; the publish job requires a
manual dispatch, an explicit `publish` input, `PMVL_SCHEDULE_PUBLISH_ENABLED`, a
successful research job, and `refs/heads/main`. The published artifact is a
deterministic gzip SQLite snapshot whose manifest records both the compressed and
uncompressed identities; the API resolves and verifies it into a read-only
`/tmp` copy at boot. See
[Deterministic compressed Snapshot publication](compressed-snapshot-publication.md).

### Local vs CI vs Vercel

| Where | What runs | Writes |
|---|---|---|
| Developer machine | `make setup && make dev`: APScheduler (`pmvl schedule`) with the cadences shown on `/system`, or any `pmvl` command, against local SQLite | data/pmvl.db (gitignored) |
| GitHub Actions (schedule) | `pipeline.yml` research job, hourly; stateless recompute seeded from the parent snapshot | nothing durable; held candidate artifact |
| GitHub Actions (manual publish) | `pipeline.yml` publish job | data/pmvl-snapshot.db.gz + manifest, as a git commit |
| Vercel | `api/index.py` (FastAPI, read-only, frozen snapshot) + `apps/web` (Next.js) | nothing; the bundle is read-only |

### Remaining limitations, stated plainly

- **No live database behind the public site.** The read-only artifact is the
  safety property, not a limitation to remove.
- **No sub-minute arbitrage.** GitHub's minimum practical schedule is several
  minutes; the cadence table reports what is actually configured. Worth
  revisiting only if the product ever claims to act on those windows - which,
  being read-only research, it does not.
- **No queue, no broker, no container orchestrator.** The pipeline is serial
  jobs against a handful of public endpoints.
- **The snapshot is frozen at publication time.** Freshness is hourly-to-daily,
  appropriate for research, not for execution.
- **No paid infrastructure provisioned automatically.** The cost doc prices the
  options; creating any of them is a decision for the repository owner.

## How this architecture was chosen

The comparison below was written when the pipeline was a proposal; it is kept
because the argument is unchanged and the rejected options are still the wrong
ones.

| | **A. GitHub Actions + committed artifact (chosen)** | **B. Small VM + managed Postgres** | **C. Vercel Cron + serverless functions** |
|---|---|---|---|
| **Reliability** | High. GitHub's scheduler can delay a run several minutes under load but rarely drops one. No host to patch. | Highest. A long-lived process, exactly what APScheduler was written for. Also the only option that can fail silently while looking healthy. | Low for this workload. Each tick is a cold start with no shared state. |
| **Execution limits** | 6 h/job, 2000 min/month free on private repos. Ingest takes ~2 min. | None. | **60 s** on Hobby, 300 s Pro. Ingest exceeds this at current market counts. |
| **Recurring cost** | **$0** (public repo) or within the free tier | $12-19/mo (see cost doc) | $0-20/mo, plus a database that still has to live elsewhere |
| **Operational complexity** | Low. YAML, no host, no SSH. | Medium-high. A host to patch, a process to supervise, backups to verify. | Medium. Work must be split into sub-60 s chunks, which means inventing a queue. |
| **Persistence** | Artifact + git. No concurrent writer. | Real database, concurrent readers and writers. | None; requires an external store regardless. |
| **Observability** | Run logs, retained 90 days, one page per run. | Whatever is installed. | Vercel logs, short retention. |
| **Failure recovery** | Re-run the workflow from the UI. Previous artifacts remain. | Restart the process; restore from backup. | Retry the function, if the failure was even noticed. |
| **Scale headroom** | Fine to ~10^4 markets. Runs are serial. | Fine well beyond that. | Poor: the 60 s limit binds first. |
| **Secrets** | Repository/environment secrets, scoped per environment. | Whatever the host provides; easiest to get wrong. | Vercel environment variables, already in use. |
| **Rollback** | Redeploy a previous validated artifact. Trivial. | Restore a database backup. Slower, and the artifact still has to be regenerated. | Same as B. |

The decisive argument is that **A preserves the property that already makes this
deployment trustworthy**: the public site serves an immutable, validated,
checksummed artifact that cannot be half-written and can be rolled back by
redeploying a previous commit. B replaces that with a live database the public
site reads directly - strictly *more* ways to serve wrong data, in exchange for
freshness the research use case does not need.

C is rejected on a hard technical fact rather than preference: ingest does not
fit in 60 seconds, and splitting it into a queue of sub-minute chunks would add
a queue, a state machine and a whole class of partial-failure bugs to avoid
provisioning a $12 VM.

## Workflow control plane

Two jobs, two tokens. GitHub Actions permissions are **job-scoped**, so a step
cannot elevate a `contents: read` token by setting `GH_TOKEN`; a single job that
computed and then pushed would have failed on its first real publication.

```
research  (contents: read)   → computes, validates, uploads raw+gzip HELD candidate
   │                            never commits, never pushes
   ▼  artifact: pmvl-candidate-<run_id>-<attempt>-<sha>
publish   (contents: write)  → revalidates, commits gzip+manifest together, pushes
   ├─ verify CI (contents: read)          → checks out the exact published SHA
   └─ verify production (contents: read)  → waits for that SHA on API and Web
```

The publish job runs only when **all** of these hold: manual dispatch, the
`publish` input, `PMVL_SCHEDULE_PUBLISH_ENABLED`, `refs/heads/main`, a successful
research job, and `publication_eligible`. There is no `||` in that condition, so
there is no alternative branch a scheduled or preview run could satisfy.

The two verification calls are explicit because GitHub does not start another
Actions run for a push made by the workflow's own `GITHUB_TOKEN`. They reuse the
normal CI and post-deploy workflows with the commit SHA emitted by the publish
step; neither verifier has write permission, and neither can start a publication.

`PMVL_SCHEDULE_ENABLED` gates scheduled **computation** at the job level, so a
disabled schedule produces a *skipped* job. It was previously a shell `exit 78`,
which Actions treats as a failure - merging with the variable unset would have
painted the repository red every hour, and the `if: always()` report step would
then have failed again on the report the skipped run never wrote.

The two variables are deliberately uncoupled: enabling scheduled research must not
require enabling publication.

## What is deliberately not being built

- **No live database behind the public site.** The read-only artifact is the safety
  property, not a limitation to remove.
- **No queue, no broker, no container orchestrator.** Serial jobs against public
  endpoints.
- **No sub-minute arbitrage.** Not achievable under this architecture, and not
  needed by a read-only research product.
- **No paid infrastructure provisioned automatically.** The cost doc prices the
  options; creating any of them is a decision for the repository owner.
