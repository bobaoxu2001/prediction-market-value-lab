# LIP shadow study: runbook

The study is pre-registered in `lip-preregistration.md`. This page is how it is run. Nothing
here places an order: every command reads public data or local files.

## The day

| UTC | local (UTC+8) | what runs |
|---|---|---|
| 02:10 - about 12:40 | 10:10 - 20:40 | the maker study's recorder (`scripts/record_maker_day.sh`, cron) |
| 12:25 | 20:25 | `scripts/record_lip_day.sh` (cron, once G6 is installed): waits for the maker lock and process to clear, gives up at 13:30 |
| 12:30 - 00:45 | 20:30 - 08:45 | the quoting window: books at 1 Hz, the tape, status, admission |
| 00:45 - 01:50 | 08:45 - 09:50 | recording continues for the one-hour marks; no new admissions |
| 01:50 - 02:00 | 09:50 - 10:00 | final drain, paid_out/closed listings, settlements, verification, gzip, `health.json` |

The recorder refuses to start between 02:00 and 12:25 UTC. **The Mac must be awake at 10:10
and 20:25 local and stay awake through each run**: macOS cron does not run a job the
machine slept through, and a closed lid on battery sleeps. A missed day is a gap, recorded
in `artifacts/forward/lip-daily/skipped.log`; it is never re-run at another hour.

This is the study's largest practical risk. Between 2026-09-25 and 2026-09-30 the maker
study lost two whole days to a closed lid and three more in part: each woke into a DNS
failure that ended its run. The LIP recorder does not end on a failed request - every lane
records the failure as a gap and resumes (tested) - and the cron script runs it under
`caffeinate -i -s`, which stops idle sleep and, on mains power, system sleep. Nothing a
process can do stops a lid-close sleep on battery. So, from 20:25 to 10:00 local: **on
power, lid open** (or closed with an external display attached). A night that sleeps
through more than a fifth of the window is not a counted day, and seven empty days in a
row stop the study.

## What every day leaves behind

| file | written by | answers |
|---|---|---|
| `lip-daily/runs.jsonl` | the cron script | did cron fire, when, and how did the run end: a `start` record at once, an `end` record from the EXIT trap with the exact exit code and one of `SUCCESS`, `INTENTIONAL_SKIP` (exit 6), `RECORDER_FAILURE`, `SIGNALLED`, `UNKNOWN_FAILURE` |
| `lip-daily/run-status.jsonl` | `run-status --record`, at each run | every past day's final status, including `NEVER_LAUNCHED` - the day cron never started, which no wrapper can report |
| `<day>/heartbeat.json` | the recorder, every 30 s | alive or not, per-lane last success and failure, consecutive failures, gap seconds, tape watermark, slept seconds |
| `<day>/ends.json` | the recorder, every 60 s | every stream's verified end, so a killed day is read to that point |
| `<day>/health.json` | the recorder, at close | the counted-day figures, the gap runs, the stream ends |
| `<day>/manifest.json` | `manifest`, run by the cron script at exit | the whole account: identity and provenance, expected vs actual schedule, lifecycle, coverage and gaps, faults by lane and class (dns, connection, timeout, http_429, http_5xx, http_4xx, malformed), sleeps, integrity (verification, duplicates, continuity, bursts), and the pre-registered day status (`COUNTED` / `NOT_COUNTED` / `SKIPPED` / `NOT_RECORDED`) |
| `artifacts/forward/lip-freeze.json` | `freeze-manifest --write`, once before the tag | the exact modules, files, parameters, environment and validation results the counted days ran under; each day's `study.json` carries its hash |

`manifest` and `run-status` read local files only and are deterministic: re-running them
gives the same answer. Neither ever re-runs or creates a day.

## Commands

```bash
.venv/bin/python -m pmvl_worker.lip_cli record-day                  # a study day (cron runs this)
.venv/bin/python -m pmvl_worker.lip_cli record-day --shakedown --until 2026-09-25T13:40:00+00:00
.venv/bin/python -m pmvl_worker.lip_cli health artifacts/forward/lip-daily/2026-09-26
.venv/bin/python -m pmvl_worker.lip_cli preflight artifacts/forward/lip-shakedown/2026-09-25 --write
.venv/bin/python -m pmvl_worker.lip_cli replay-day <day> --health-only   # allowed on study days
.venv/bin/python -m pmvl_worker.lip_cli study                            # interim: counts only
.venv/bin/python -m pmvl_worker.lip_cli run-status                       # today's run: due, running, done, missed?
.venv/bin/python -m pmvl_worker.lip_cli manifest artifacts/forward/lip-daily/<day>   # the day's full account
.venv/bin/python -m pmvl_worker.lip_cli freeze-manifest --check          # has anything changed since the freeze?
```

At the verdict date (last study day + 14 days), in this order:

```bash
.venv/bin/python -m pmvl_worker.lip_cli programs-paid
touch artifacts/forward/lip-final.flag
.venv/bin/python -m pmvl_worker.lip_cli replay-day artifacts/forward/lip-daily/<each day>
.venv/bin/python -m pmvl_worker.lip_cli study --final
```

`replay-day` refuses to produce dollars for a study day until `lip-final.flag` exists, and
`study` prints `INTERIM_NOT_A_DECISION` with counts only until the verdict date.

## Gates before day 1

| gate | what | status |
|---|---|---|
| G1 | the rule text in force (CFTC filing of 2026-07-15) diffed against section 1 | closed - Amendment 1 |
| G2 | documented rate limits: 10 write actions/s on the Basic tier | closed - Amendment 1 |
| G3 | the account is US, holds no Market Maker Agreement, is not an IB/FCM customer, not an affiliate | closed - user, 2026-09-24 |
| G4 | code merged, tests green, a synthetic replay passing, adversarial review, `freeze-manifest --write`, tag `lip-sim-1.0.0` | open |
| G5 | a shakedown of 60+ minutes inside the window, then `preflight --write` | open - shakedown 2026-09-25 12:25 UTC |
| G6 | the cron line, approved by the user | approved; installed after G4 and G5 |

When every gate is closed, `artifacts/forward/lip-gates.json` records the commit, the freeze
manifest's hash and the time, and day 1 is the next 12:30 UTC window. `close-gates` refuses
unless the freeze manifest exists, names the tagged commit, and still matches every file.

## The cron line

```
25 20 * * * /Users/xuao/Projects/prediction-market-value-lab/scripts/record_lip_day.sh
```

Remove it with `crontab -e` and delete that line; the maker study's own line is separate.

## When something fails

- **`refused: ... inside the maker study's window`** - started too early; nothing was recorded.
- **`/tmp/pmvl-lip-day.lock.d exists`** - a previous run is still going or was killed (SIGKILL,
  power loss: the lock, the last minute's `ends.json` and every line written survive; the
  day is read to that checkpoint). Check `pgrep -fl lip_cli`. If nothing is running, remove
  the directory by hand - **but only after that day's window has ended (02:00 UTC)**.
- **Never restart a study day by hand.** A restarted day re-reads the tape and writes prints a
  second time, and the reader does not de-duplicate them (known, documented in the pre-freeze
  audit, not fixed before the freeze); its closing `health.json` would also know only the
  second process's ticks. A day that failed stays failed: its verified prefix is its record,
  and the manifest flags any restart (`lifecycle.restarts`) and any duplicate
  (`integrity.duplicate_trade_ids`). There is no automatic restart, for the same reasons.
- **A run that never started** (the machine asleep at 20:25 local) leaves no record in
  `runs.jsonl`; the next run records it in `run-status.jsonl` as `NEVER_LAUNCHED`, and
  `run-status` reports it at any time.
- **A day with many 429s** - the recorder halves the lane's rate for ten minutes and, past 1%
  of a window's requests, pauses books to the end of the hour. The day still counts toward
  nothing it did not observe: unobserved seconds are zeros in the denominator.
- **A code fix after day 1** - bump `LIP_SIM_VERSION` (or `LIP_RECORDER_VERSION`), report both
  results, and never pool across versions.
