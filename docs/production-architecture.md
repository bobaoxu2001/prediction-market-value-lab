# Production pipeline architecture

## What exists today

```
┌─────────────────────────────────────────────────────────────┐
│  Developer machine (the only place the pipeline ever runs)  │
│                                                             │
│  APScheduler ──► ingest ──► score ──► rank ──► arbitrage    │
│       │                                                     │
│       └────────────► local SQLite (read-write)              │
│                            │                                │
│                            ▼                                │
│                   make snapshot-build                       │
│                            │                                │
└────────────────────────────┼────────────────────────────────┘
                             │  git commit (8.1 MB binary)
                             ▼
        ┌────────────────────────────────────────┐
        │  Vercel                                │
        │                                        │
        │  pmvl-api  ──reads──► bundled SQLite   │  read-only
        │      ▲                 (rollback jrnl) │
        │      │                                 │
        │  pmvl-web ─────────────────────────────┘
        └────────────────────────────────────────┘
```

The scheduler is real and correct. It has simply never run anywhere but a laptop.
Production is a photograph of one developer's database, taken by hand and committed
to git. Everything downstream of that photograph is honest about being frozen; the
problem is that refreshing it requires a human.

## Three architectures considered

| | **A. GitHub Actions + committed artifact** | **B. Small VM + managed Postgres** | **C. Vercel Cron + serverless functions** |
|---|---|---|---|
| **Reliability** | High. GitHub's scheduler can delay a run several minutes under load but rarely drops one. No host to patch. | Highest. A long-lived process, exactly what APScheduler was written for. Also the only option that can fail silently while looking healthy. | Low for this workload. Each tick is a cold start with no shared state. |
| **Execution limits** | 6 h/job, 2000 min/month free on private repos. Ingest takes ~2 min. | None. | **60 s** on Hobby, 300 s Pro. Ingest exceeds this at current market counts. |
| **Recurring cost** | **$0** (public repo) or within the free tier | $12–19/mo (see cost doc) | $0–20/mo, plus a database that still has to live elsewhere |
| **Operational complexity** | Low. YAML, no host, no SSH. | Medium-high. A host to patch, a process to supervise, backups to verify. | Medium. Work must be split into sub-60 s chunks, which means inventing a queue. |
| **Persistence** | Artifact + git. No concurrent writer. | Real database, concurrent readers and writers. | None; requires an external store regardless. |
| **Observability** | Run logs, retained 90 days, one page per run. | Whatever is installed. | Vercel logs, short retention. |
| **Failure recovery** | Re-run the workflow from the UI. Previous artifacts remain. | Restart the process; restore from backup. | Retry the function, if the failure was even noticed. |
| **Scale headroom** | Fine to ~10⁴ markets. Runs are serial. | Fine well beyond that. | Poor: the 60 s limit binds first. |
| **Secrets** | Repository/environment secrets, scoped per environment. | Whatever the host provides; easiest to get wrong. | Vercel environment variables, already in use. |
| **Rollback** | Redeploy a previous validated artifact. Trivial. | Restore a database backup. Slower, and the artifact still has to be regenerated. | Same as B. |

### Recommendation: **A — GitHub Actions, with the committed artifact retained**

The workload is nine periodic jobs against roughly 1,400 markets, the longest of
which takes about two minutes. That is not a distributed-systems problem, and the
cost of pretending it is falls entirely on the person who has to operate it.

The decisive argument is that **A preserves the property that already makes this
deployment trustworthy**: the public site serves an immutable, validated,
checksummed artifact that cannot be half-written and can be rolled back by
redeploying a previous commit. B replaces that with a live database the public site
reads directly — which is strictly *more* ways to serve wrong data, in exchange for
freshness the research use case does not need. A daily-to-hourly refresh is
appropriate for a platform whose output is research, not execution.

C is rejected on a hard technical fact rather than preference: ingest does not fit
in 60 seconds, and splitting it into a queue of sub-minute chunks would add a queue,
a state machine and a whole class of partial-failure bugs to avoid provisioning a
$12 VM.

What A gives up is honest to state: the arbitrage scan cannot run every minute.
GitHub's minimum practical schedule is around 5 minutes and contended runners make
even that unreliable. **The 1-minute arbitrage cadence is not achievable under this
architecture**, and the cadence table now reports what is actually configured rather
than an aspiration. Sub-minute arbitrage detection needs option B, and is worth
revisiting only if the product ever claims to act on those windows — which,
being read-only research, it does not.

## Target architecture

```
┌──────────────────────── GitHub Actions ────────────────────────┐
│                                                                │
│  schedule: */15  ──► ingest + orderbooks ──┐                   │
│  schedule: hourly ─► score + rank ─────────┤                   │
│  schedule: daily ──► settle + backtest ────┤                   │
│                                            ▼                   │
│                              operational SQLite (job artifact) │
│                                            │                   │
│                                   validate + checksum          │
│                                            │                   │
│                              ┌─────────────┴──────────────┐    │
│                          FAIL │                           │ PASS│
│                              ▼                            ▼    │
│                     keep previous artifact      commit artifact │
│                     alert, do not publish       + manifest      │
└────────────────────────────────────────────────┬───────────────┘
                                                 │
                                                 ▼
                                    Vercel (unchanged)
                                    pmvl-api / pmvl-web
```

The publication gate is the point. A run that fails validation leaves the previous
artifact in place, so the worst outcome of a broken pipeline is **stale data that is
labelled stale**, never corrupt data that looks fresh.

## What is deliberately not being built

- **No live database behind the public site.** The read-only artifact is the safety
  property, not a limitation to remove.
- **No queue, no broker, no container orchestrator.** Nine serial jobs.
- **No sub-minute arbitrage.** Stated above; not achievable here, and not needed by
  a read-only research product.
- **No paid infrastructure provisioned automatically.** The cost doc prices the
  options; creating any of them is a decision for the repository owner.
