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

## Commands

```bash
.venv/bin/python -m pmvl_worker.lip_cli record-day                  # a study day (cron runs this)
.venv/bin/python -m pmvl_worker.lip_cli record-day --shakedown --until 2026-09-25T13:40:00+00:00
.venv/bin/python -m pmvl_worker.lip_cli health artifacts/forward/lip-daily/2026-09-26
.venv/bin/python -m pmvl_worker.lip_cli preflight artifacts/forward/lip-shakedown/2026-09-25 --write
.venv/bin/python -m pmvl_worker.lip_cli replay-day <day> --health-only   # allowed on study days
.venv/bin/python -m pmvl_worker.lip_cli study                            # interim: counts only
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
| G4 | code merged, tests green, a synthetic replay passing, adversarial review, tag `lip-sim-1.0.0` | open |
| G5 | a shakedown of 60+ minutes inside the window, then `preflight --write` | open - shakedown 2026-09-25 12:25 UTC |
| G6 | the cron line, approved by the user | approved; installed after G4 and G5 |

When every gate is closed, `artifacts/forward/lip-gates.json` records the commit and the
time, and day 1 is the next 12:30 UTC window.

## The cron line

```
25 20 * * * /Users/xuao/Projects/prediction-market-value-lab/scripts/record_lip_day.sh
```

Remove it with `crontab -e` and delete that line; the maker study's own line is separate.

## When something fails

- **`refused: ... inside the maker study's window`** - started too early; nothing was recorded.
- **`/tmp/pmvl-lip-day.lock.d exists`** - a previous run is still going or crashed. Check
  `pgrep -fl lip_cli`; if nothing is running, remove the directory by hand.
- **A day with many 429s** - the recorder halves the lane's rate for ten minutes and, past 1%
  of a window's requests, pauses books to the end of the hour. The day still counts toward
  nothing it did not observe: unobserved seconds are zeros in the denominator.
- **A code fix after day 1** - bump `LIP_SIM_VERSION` (or `LIP_RECORDER_VERSION`), report both
  results, and never pool across versions.
