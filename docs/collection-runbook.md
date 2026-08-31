# Running the collector

Operational companion to [collection-operations.md](collection-operations.md)
(schedules, timestamp contract) and [pit-collection.md](pit-collection.md)
(schema).

---

## The two execution paths, and why only one satisfies the window gate

| Path | Command | Cadence achievable |
|---|---|---|
| **Resident worker** | `pmvl schedule` | Every configured cadence, including 30 s inside an issuance window |
| **Actions fallback** | hourly cron in `pipeline.yml` | One pass per hour |

An hourly workflow that wakes, runs and exits cannot poll every 30 seconds for an
hour. **The ≥40-captures-per-window gate is reachable only on the resident
path.** The fallback exists so that a worker outage costs cadence rather than all
collection.

### Starting the resident worker

```bash
pmvl schedule
```

It registers 16 jobs on one `BlockingScheduler` — the canonical one. A test
asserts no second scheduler exists anywhere in `services/worker`.

Every job is `max_instances=1, coalesce=True`. That matters most for
`research_books`, which holds its slot for the length of a window: a second
concurrent invocation would double venue load and write duplicate captures at the
same instant.

### Keeping it alive

The worker is a foreground process with SIGINT/SIGTERM handlers and a graceful
`scheduler.shutdown(wait=False)`. It has no supervisor of its own — supervision
belongs to the host:

- **systemd**: `Restart=always`, `RestartSec=30`
- **launchd** (macOS): `KeepAlive=true`
- **Docker**: `restart: unless-stopped`

Restart is safe. Every collector is idempotent on natural keys, so a restart
mid-window resumes rather than re-dating anything (see below).

#### When `KeepAlive` does not restart it

`KeepAlive=true` only applies while the job is *loaded*. A job can be removed
from the user domain and left in a persistent disabled list, and then nothing
restarts it — not `KeepAlive`, and not the next login.

That is what ended the 2026-08-29 run. The collector logged a clean
`shutting down scheduler` at 23:13:37Z, which is only reachable from its
SIGINT/SIGTERM handler, and never came back. 33 hours later:

```bash
launchctl print-disabled gui/$(id -u) | grep pmvl
```

```text
"com.pmvl.collector" => disabled
```

`launchctl bootstrap` fails against a disabled service with the unhelpful
`Bootstrap failed: 5: Input/output error`. Clearing the override is what the
legacy `-w` flag does:

```bash
launchctl load -w ~/Library/LaunchAgents/com.pmvl.collector.plist
```

Then confirm the job is actually supervised, not merely present:

```bash
launchctl print gui/$(id -u)/com.pmvl.collector | grep -E "state|pid|runs"
```

Check `print-disabled` before concluding the plist is at fault. A valid plist,
a valid interpreter path and a registered-looking job can all coexist with a
service that launchd has been told never to run.

### Host prerequisites for an unattended run

Supervision restarts a process. It cannot run one on a sleeping machine, and no
amount of configuration changes that. An unattended collection run has host
prerequisites, and they are prerequisites rather than recommendations:

| Requirement | Why |
| --- | --- |
| Continuous AC power | On 2026-08-28 the host reached 1% and entered Low Power Sleep for 2.45 h, losing three station-days. |
| Lid open, or a dock that suppresses clamshell sleep | `caffeinate -i` blocks *idle* sleep only. It does not block Clamshell Sleep, and two occurred on 2026-08-29. |
| System sleep disabled (`sudo pmset -a sleep 0 disablesleep 1`) | macOS Maintenance Sleep ran for 900-1000 s at a time and discarded three daily cron jobs. |

`caffeinate -i` in the launchd plist is worth keeping, but it is the weakest of
the three and covers only the first failure mode. It is not a substitute for the
other two.

When the host cannot meet these, say so in the report rather than presenting the
run as unattended. A window collected across a sleeping laptop is not a weaker
version of a continuous one; its decision instants are simply absent, and that
is not recoverable after the fact.

**There is no software fix for a sleeping host.** The scheduler's misfire grace
(`_CRON_MISFIRE_GRACE`, 1800 s) lets a *daily* job be recovered on wake, which
is why a short Maintenance Sleep no longer deletes a validation opportunity. It
does nothing for order-book capture: a 30-second cadence across a 15-minute
sleep loses 30 captures, permanently. Decision-window evidence is only ever
collected live.

### Liveness, and why age was not enough

Each job holds an *active-run slot* keyed by its idempotency key, so a scheduler
and a manual CLI cannot both run the same work. A slot is released when the run
finishes — and a killed process never finishes.

The reclaimer originally treated any `running` row older than two hours as dead.
That is right for short jobs and wrong for `research_books`, which holds its slot
for an entire issuance window *by design*: no age bound wide enough to spare a
healthy hour-long run is tight enough to reclaim a dead one before the window it
is blocking has passed.

This was observed, not predicted. A worker restarted at 19:45 UTC on 2026-08-25,
mid-window, left run 281 marked `running`; every following tick logged
`research_books skipped: an identical run (id 281) is already active`, and
collection would not have resumed until 21:30 — the rest of that window and most
of the next. It took a manual `UPDATE job_runs` to clear.

Long-running loop jobs now stamp `job_runs.heartbeat_at` every tick:

| Row | Judged by | Reclaimed after |
|---|---|---|
| Has a heartbeat | the heartbeat | 5 minutes (10 missed 30 s ticks) |
| Never heartbeats | `started_at`, as before | 2 hours |

So a legitimately long run is safe however old it gets, and a dead one is
reclaimed in minutes. If you ever need to clear a slot by hand:

```bash
sqlite3 data/pmvl.db "UPDATE job_runs SET status='failed', finished_at=datetime('now'), error='abandoned: worker terminated mid-run' WHERE status='running' AND job_name='research_books';"
```

---

## Crash and restart semantics

| Failure | Behaviour |
|---|---|
| Crash between request and persistence | The observation is lost, and the `poll_attempts` row records the attempt. A gap with a cause, not a silent one. |
| Restart mid-window | The next `research_books` tick re-enters the window and resumes at cadence, once the dead run's slot is reclaimed — see *Liveness* below. |
| Duplicate poll after restart | Deduplicated on `(station, observation_time, available_at)` / `(platform, market, observed_at)`. The **first** availability is kept — a re-read of unchanged data must not make old data look newly available. |
| One station unreachable | That station's poll is recorded as failed; the other six continue. |
| One market erroring | That market is skipped and counted; the rest of the tape continues. |
| Archive unavailable | Product collection continues; the research gap is reported, never converted into a total outage. |
| A research job failing | Cannot block product publication — `required_for_publication=False` on all six. |

**Retries** are the HTTP client's existing exponential backoff with jitter, and a
retried poll's `available_at` is the *successful* attempt's completion. Carrying
the first attempt's start time forward would credit data as knowable before the
request that returned it.

---

## SQLite write contention

The worker holds an operational-DB session across each capture (~30 s for the
research universe). WAL and a 30 s `busy_timeout` are configured, but an ad-hoc
CLI write issued while the worker is mid-capture can still time out with
`database is locked`.

This is inherent to SQLite plus a busy writer, not a defect in the collectors.

- **Read-only CLI commands** (`collection-health`, `station-audit`) are safe at any time.
- **Write commands** (`collection-health --persist`, `map-stations`, `collect-*`) should be run when the worker is idle, or retried.
- **`DATABASE_URL` pointed at PostgreSQL removes the constraint entirely** and is already supported.

---

## Verifying a window

```bash
pmvl collection-health --days 1
```

The gate that matters is **books per issuance window (best market)** — the
per-market capture count, not the row total. One capture of 252 markets is 252
rows and zero evidence of frequency; the question is whether *a* market was
captured repeatedly *through* the window.

It reports the **best completed** window. Two things follow, both deliberate:

- **A window still in progress never counts** — neither to pass the gate nor to
  fail it. It has not had its chance yet, and including it would report a healthy
  collector as failing for thirty minutes out of every six hours, or let a
  half-finished window certify an acceptance it has not earned.
- **Windows that predate the collector do not fail it.** This gate asks whether
  cadence *can* be sustained. Whether it *has been* sustained is a different
  question, reported on the adjacent `continuity:` line as
  "N/M completed windows met the bar" and preserved in the append-only history.

The two were previously the same number — the gate took a `min` across every
window in the lookback while labelled "best market", so four windows from before
the collector existed held it at zero and no amount of healthy collection could
clear it. Read both lines: the gate for capability, `continuity:` for uptime.

```bash
pmvl collection-health --persist
```

```bash
pmvl collection-health --history
```

History is append-only in the research archive. A later good day cannot erase an
earlier bad one, and two snapshots for one date are both kept — two rows for a
day is a fact about that day.

---

## Diagnosing a window shortfall

Work down this list; each item has a distinct signature.

| Cause | Signature | Fix |
|---|---|---|
| Worker not running | no `job_runs` rows for `research_books` | start it |
| Window never fired | `in_issuance_window: false` in job details | check `derivation`; see below |
| Capture overran its slot | `ticks_skipped > 0` | narrow the universe (see *Window universe*) or raise the client rate limit |
| Every tick skipped | `research_books skipped: ... already active` in the log | a dead run holds the slot; see *Liveness* |
| Rate limiting | `error_type: rate_limited` in `poll_attempts` | lower concurrency |
| Sleep drift | captures not on a 30 s grid | the loop sleeps to boundaries; a regression here is a bug |
| `max_seconds` too small | captures stop before window end | resident path passes 0 (run to window end) |
| Restart mid-window | gap then resumption in `observed_at` | expected; supervision reduces it |
| Duplicate suppression | `books_duplicate` high, `books_archived` low | the venue is not updating; not a collector fault |

### Window universe

Inside a window the loop polls only research markets resolving within
`WINDOW_HORIZON_HOURS` (48 h) — today's and tomorrow's contracts, about 84 of the
252 open weather markets.

This is a declared scoping decision, forced by arithmetic. Kalshi's batch
orderbook endpoint is validated rather than trusted, and a rejected batch falls
back to per-market fetches; at the client's 9 req/s, 252 markets is 28 s of a
30 s period. That fits on paper, which is exactly why the regression was not
obvious — sharing the limiter with the `orderbooks` and `arbitrage` jobs turned
the remaining two seconds negative, every capture overran its tick, the loop
skipped to the next boundary and the *measured* cadence was 58 s, not 31 s.

Measured before and after, same window, 2026-08-25:

| Universe | Markets/capture | Cadence (min/median/max) |
|---|---|---|
| Full research board | 252 | 47.7 / 58.2 / 92.0 s |
| Near-dated (≤48 h) | 84 | 28.7 / 29.0 / 29.3 s |

Nothing is dropped. The far-dated markets are still captured every baseline
cycle; they lose window *frequency*, not coverage. Capturing all 252 at 58 s
would be worse for the research question than capturing the forecast-sensitive
84 at 29 s, because a revision to tomorrow's forecast moves tomorrow's contracts
and cadence is the entire reason the window exists.

`tests/test_window_capture_budget.py` asserts the arithmetic, so shrinking the
rate limit or growing the universe fails a test rather than quietly halving the
sample rate.

### Issuance-window derivation

Windows come from **observed `published_at`** once there are ≥12 distinct
publication instants spanning ≥3 days; otherwise from the **documented NDFD
cycle** (02/08/14/20 UTC). Every window records which.

Both thresholds exist because of a real failure. One poll of seven stations wrote
84 rows — one forecast is recorded once per market it is relevant to — and the
first implementation counted the 84, concluding NWS publishes at seven different
hours a day from one afternoon's snapshot. Deduplicating to distinct instants and
requiring a multi-day span is what stops market fan-out being read as publication
frequency.

---

## Backup

`data/research-archive.db` and `data/payloads/` are gitignored, unbounded, and
**not recoverable**. Back them up on their own schedule:

```bash
sqlite3 data/research-archive.db ".backup data/backups/research-archive-$(date -u +%Y%m%dT%H%M).db"
```

The payload store is content-addressed, so an incremental `rsync` never rewrites
an existing object.

---

## Deploying the Actions fallback

The workflow is gated on the repository variable `PMVL_SCHEDULE_ENABLED`:

```bash
gh variable set PMVL_SCHEDULE_ENABLED --body true --repo <owner>/<repo>
```

Requires admin or the fine-grained *repository variables* permission.

**Enabling the variable is not sufficient on its own.** The workflow runs the
snapshot pipeline, and a run whose candidate fails validation exits non-zero
before the research jobs matter. Check:

```bash
gh run list --workflow=pipeline.yml --limit 20
```

```bash
gh run view <id> --log-failed
```

A run failing with `snapshot is NN MB, above the ceiling` means the deployed
branch predates the deterministic retention policy — the code is fixed but has
not been shipped. See [snapshot-retention.md](snapshot-retention.md).
