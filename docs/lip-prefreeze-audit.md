# LIP shadow study: pre-freeze audit (2026-09-30)

An operational reliability and auditability pass before `lip-sim-1.0.0` is tagged, so that
every study day can be reconstructed and diagnosed after the fact. Study semantics were not
to change in this pass; where a correctness bug turned up, it is documented here and left for
a decision rather than fixed.

## A. Verdict

```
LIVE_REPOSITORY_CONFIRMED        = YES  /Users/xuao/Projects/prediction-market-value-lab, branch measure-before-selling
CORE_STUDY_SEMANTICS_CHANGED     = YES in e41da4f (third-check fixes F1-F6, Amendment 3, made before this pass began)
                                   NO  in f22acd8 (this pass: operational only; one added field per book line)
WRAPPER_EXIT_LOGGING_ROBUST      = YES
HEARTBEAT_IMPLEMENTED            = YES
MISSED_RUN_SENTINEL_IMPLEMENTED  = YES
DAILY_MANIFEST_IMPLEMENTED       = YES
PREREG_DAY_STATUS_AUTOMATED      = YES
FREEZE_MANIFEST_READY            = YES (the command; the file is generated after the G5 pre-flight)
FAILURE_RECOVERY_TESTS_PASS      = YES (one strict xfail documents the known restart issue below)
FULL_SUITE_PASS                  = YES (see the final commit's suite log); ruff clean; compileall clean
READY_TO_FREEZE                  = NO - three semantic defects found by the fourth check (G1-G3) are
                                   documented below and not fixed, per the stop rule; see D
```

## Repository and freeze safety (Phase A)

| item | value |
|---|---|
| authoritative repository | `/Users/xuao/Projects/prediction-market-value-lab` |
| branch / HEAD | `measure-before-selling` / see `git log` (f22acd8 at the end of this pass) |
| other clone | `/Users/xuao/Documents/2025 找工作/AI Projects/prediction-market-value-lab` at 4500481 - stale, never modified |
| remote | `origin` (GitHub); 146+ commits ahead, nothing pushed |
| pre-registration | `docs/lip-preregistration.md` (Amendments 1-3) |
| runbook | `docs/lip-runbook.md` |
| cron wrapper | `scripts/record_lip_day.sh`; line `25 20 * * *` (20:25 local = 12:25 UTC), not yet installed |
| raw study data | `artifacts/forward/lip-daily/<YYYY-MM-DD>/` (none yet); shakedown `artifacts/forward/lip-shakedown/2026-09-25/` |
| credentials | none needed: every LIP request is a public market-data read |
| lint / checks in CI | `ruff check packages services scripts tests`, `pytest`, `python -m compileall` |

## B. Exact changes

### Semantic (commit e41da4f) - made before this pass's instructions, documented as required

Each was reproduced by an independent third check and has a regression test that fails on
the previous code. All are recorded in `docs/lip-preregistration.md` Amendment 3, written
before day 1 and before any study data exists, so no version bump is needed (the versions
were never frozen or used for study data).

| # | location | demonstrated failure | changes semantics | needs a new version? |
|---|---|---|---|---|
| F1 | `recorder.py` `_book_lane`, `_read_books`, `_write_health` | reads credited to the grid second of their jittered send: a day with every read successful scored 0.58-0.76 and did not count, for about a third of start phases | yes: which days count | no - Amendment 3.1, before day 1 |
| F2 | `valuation.py` `_fallback` | HIGH's fallback mark (last mid 42.5) below LOW's (bid 60): MARK_1H LOW > HIGH by $30 | yes: HIGH's valuation | no - Amendment 3.2 |
| F3 | `valuation.py` `_exit_charge` | HIGH charged a two-sided exit against the bid, not the mid: +half a spread per contract in HIGH | yes: HIGH's valuation | no - Amendment 3.2 |
| F4 | `store.py`, `reader.py`, `health.py`, `lip_cli.py` | a day whose recorder died had no `health.json` and could never be read; one such day refused every verdict | yes: such a day is read to its checkpoint | no - Amendment 3.3 |
| F5 | `simulator.py` post-end loop | a placement landing after the end was dropped with its fills | yes: fills | no - Amendment 3.5 |
| F6 | `simulator.py` `_known_open` | an unknown status poll closed the gate for the hour | yes: quoting | no - Amendment 3.4 |
| - | `lip_cli.py` `close-gates` | would accept a day 1 already in the past when run before 12:00 UTC | no (operations) | no |
| - | `study.py` bootstrap | a study with nothing admitted crashed instead of INSUFFICIENT_EVIDENCE | no (a crash) | no |

### Operational (commit f22acd8) - this pass

| file | purpose | why study semantics are unchanged | tests |
|---|---|---|---|
| `scripts/record_lip_day.sh` | start record at once; EXIT-trap end record with exact exit code and status; recorder's code preserved (6 stays 6); every catchable signal (TERM, INT, HUP, QUIT, USR1, USR2, ALRM) recorded and forwarded (G5); no `set -e`; `caffeinate -w <recorder>`; day label as the recorder names it; sentinel and manifest run beside it | launches the same command at the same time; adds records only | `test_lip_wrapper.py` (9) |
| `lip_cli.py` `record-day` | SIGTERM/HUP/INT cancel the run once so its finally-block checkpoints and releases the lock (a second signal cannot cut it short - G4); exit 128+signal; run id from the wrapper | only the handling of a termination that would otherwise leave no checkpoint | `test_lip_ops.py` SIGTERM and double-signal tests |
| `recorder.py` | `heartbeat.json` every 30 s (atomic, best effort, no requests); per-lane last ok/failure and consecutive failures; tape counters; wall-clock jumps logged as sleeps; gap runs, run id, tape counters in `health.json`; each book line carries its scheduled tick; a failed checkpoint or heartbeat can never stop a recording; cleanup is synchronous before any await, and nothing is written after the writer closes | no request is added, removed or moved; the counted-day figure is computed exactly as before. What is recorded gains a `tick` field per book line and a `wall_jump` row kind in the chained `lanes` stream (G6) - both ignored by every study computation (`final` keeps only `requests` rows) | `test_lip_ops.py` heartbeat, sleep, two-lane, best-effort tests; all recorder tests unchanged |
| `daymanifest.py` (new) | `run_status` (sentinel), `record_final_statuses` (ledger), `qualify` (the pre-registered day status), `build_manifest` (the per-day account) | reads files only; cannot import the modules that compute money (tested) | `test_lip_ops.py` sections D, E, G |
| `lip_cli.py` `manifest`, `run-status`, `freeze-manifest` | the commands; `close-gates` now requires a freeze manifest that names the tagged commit and still matches every file | read-only except their own output files | `test_lip_ops.py` section F |
| `replay.py` `write_results`, `lip_cli.py` `replay_presentation` | `replay.json` carries `presentation`: `INCOMPLETE_COVERAGE_NOT_PERFORMANCE_RESULT` for any non-study day, with the recorded share of the window; study days are `STUDY_DAY_POOLED_ONLY` | the results array is written unchanged beside the label (tested) | `test_lip_ops.py` section H |
| `docs/lip-runbook.md` | what each file answers; new commands; never restart a study day by hand; stale-lock procedure; no automatic restart | documentation | - |

### Per-day status and qualification

`qualify` returns exactly one of `COUNTED`, `NOT_COUNTED`, `SKIPPED`, `NOT_RECORDED`, using the
recorder's own figure (`health.json`: ok ticks >= 0.80 x window ticks at the recorded cadence,
Amendment 2 item 6 and 3.1), or for a day whose recorder died, the outcome-blind health of its
verified prefix (Amendment 3.3). There is no `COUNTED_WITH_GAPS`: the pre-registration has no
such category, and the manifest reports gaps separately. The sentinel returns one of
`NOT_YET_DUE`, `RUNNING`, `COMPLETED`, `INTENTIONAL_SKIP`, `FAILED`, `DIED`, `NEVER_LAUNCHED`,
plus `launch` = `ON_TIME` / `LATE` (started after 12:30 UTC) / `NONE`.

## Found in this pass and NOT fixed (per the stop rule)

**A same-day restart duplicates tape prints in the replay.**

1. *Location.* `recorder.py` `_tape_poll`: the de-duplication set `seen_trades` lives in memory
   only, and a new process starts its watermark at now - 60 s. `reader.py` `load_day`, trades
   loop: prints are not de-duplicated by `trade_id`.
2. *Demonstrated.* A recorder run for 5 minutes, then restarted by hand 20 s later against a
   tape that keeps its history (as Kalshi's does): 740 prints written, 690 distinct, 50 written
   twice, and all 740 reach the simulator. `tests/test_lip_ops.py::
   test_a_same_day_restart_does_not_duplicate_prints_in_the_replay` is a strict xfail that
   reproduces it and will fail loudly once it is fixed.
3. *Semantics.* Only on a day recorded by more than one process; a day recorded by one process
   has no duplicates, so its results are unaffected. Also on restart, the closing `health.json`
   knows only the second process's book ticks - an undercount, never an overcount (tested).
4. *Version.* A reader-side de-duplication before day 1 would be one more pre-day-1 amendment;
   after day 1 it needs a `LIP_SIM_VERSION` bump.

Mitigation in place (operational): cron starts each day once; the lock refuses a second
process; the runbook forbids restarting a study day by hand and allows removing a stale lock
only after the day's window has ended; the manifest flags any restart (`lifecycle.restarts`)
and any duplicate (`integrity.duplicate_trade_ids`).

**Fourth check, G1 - a crashed day's counted figure is not the pre-registered rule.**

1. *Location.* `health.py` `day_health` (share = min over lanes of ok lines / window ticks),
   which `lip_cli._day_status` (hence `_calendar` and `study`) and `daymanifest.qualify` use
   for a day with no `health.json`.
2. *Demonstrated.* A full virtual day, two lanes, killed at 00:44 UTC: with independent 12%
   failures per lane it reports 0.877 and COUNTED where the Amendment 2 item 6 rule (every due
   lane read at the tick) gives 0.774 and not counted; with lane B due for only 6 hours it
   reports 0.572, not counted, where the rule gives 0.999, counted.
3. *Semantics.* Yes - which days count, hence the calendar's end and the 20-day precondition -
   but only for days whose recorder died.
4. *Version.* Before day 1: a note to Amendment 3.3. After day 1: a version bump.
   *Recommended fix:* each checkpoint also carries the recorder's own item-6 ok-tick count up
   to its time (the same sweep `_write_health` runs); a crashed day uses that, and every tick
   after the checkpoint is a miss.

**Fourth check, G2 - LOW can book a gain from a stale exit reference.**

1. *Location.* `valuation.py` `_exit_charge`: when the book before the exit is two-sided its mid
   is the reference in both envelopes (Amendment 3.2), with no bound on how old that book is.
2. *Demonstrated.* One 200-lot YES fill at 40; no books from T0+590 to T0+700, so the gap spans
   the exit; before 40/55, after 50/45. LOW's exit charge is +$11.50 (a gain) and its MARK_1H
   +$31.50, against +$16.50 before e41da4f and -$8.36 with no gap. The move across the gap is
   counted twice.
3. *Semantics.* Yes - LOW, the envelope that alone can support a positive verdict, is
   overstated on any day where a gap (a sleep, an outage) spans a day-end flatten.
4. *Version.* Before day 1: a note to Amendment 3.2. After day 1: a `LIP_SIM_VERSION` bump.
   *Recommended fix:* use the mid only if its book was received within `span_limit_s` of the
   exit; otherwise the candidate rule; and in LOW never a reference below the sale price, so a
   flatten is never a gain in LOW (the pre-e41da4f guarantee).

**Fourth check, G3 - an empty first checkpoint lets unverified data be read.**

1. *Location.* `store.py` `read_checked`: with prefix ends that are empty (the checkpoint
   written before any stream exists) the `and ends` test is false and every stream is read in
   full with no expected end.
2. *Demonstrated.* Recorder killed 45 s in: `ends.json` = `{"streams": {}}`, and books, tape
   polls, universe, programs and status are all read as verified.
3. *Semantics.* Only for a day killed within its first minute or two. The fix needs a rule
   for what such a day contributes (its admitted hours at zero, as a skipped day), because a
   carried pair then has no recorded close time.
4. *Version.* Before day 1: a note to Amendment 3.3.

**No automatic restart** was added (Phase I): a restart would change tape recovery (above), the
closing tick count, and so the counted-day figure. A failed run is observable instead.

## C. Remaining risks software cannot solve

- **Lid-close (clamshell) sleep on battery.** No process can prevent it. The maker study lost
  2026-09-25 and -27 outright and 09-28/29/30 in part to it. The LIP window is 20:25-10:00
  local: the Mac must be on power with the lid open (or closed with an external display).
  A sleep inside the window is a recorded gap (`wall_jump`); a night that loses more than a
  fifth of the window does not count; seven empty days in a row stop the study.
- **Missed launch while asleep at 20:25.** cron does not run missed jobs; the day is
  `NEVER_LAUNCHED` and stays missed (the sentinel records it at the next run).
- **Power loss / SIGKILL.** Everything written before survives (each line fsynced, checkpoint
  each minute); the stale lock blocks that day's re-run (by design) and must be removed after
  02:00 UTC.
- **Home network, router, ISP or DNS outage; Kalshi outage or API change.** Recorded as gaps
  with their fault class; nothing can recover data the venue did not serve.
- **The sentinel cannot report a missed day in real time** if the machine itself is off; it
  reports it at the next run or on demand.

## D. Freeze recommendation

**Not safe to tag `lip-sim-1.0.0` yet.** The true blockers:

1. **G2** - LOW can book a gain from a stale price. This breaks the one property LOW exists
   for. It must be fixed (or F3 reverted to the pre-e41da4f exit rule) before any study day.
2. **G1** - the counted status of a day whose recorder died can be wrong in both directions.
3. **G3** - low, but in the same mechanism as G1; fix together.
4. **Your acceptance of e41da4f** (F1-F6, Amendment 3), made before the stop rule arrived.
5. **G5 (gate)** - the pre-flight writes `artifacts/forward/lip-cadence.json`; it can run only
   12:26-01:59 UTC (scheduled for 2026-09-30 12:26 UTC).

Suggested path: approve fixes for G1-G3 as notes to Amendment 3 (before day 1, so no version
bump), a focused fifth check of those lines only, then the freeze. Day 1 = 2026-10-01 is still
reachable (its window opens 12:25 UTC); if not, 2026-10-02 - `close-gates` accepts any day
whose window has not started. The operational hardening itself (f22acd8 and the fixes to
G4-G6) is complete and needs no further change.

The restart duplicate is not a blocker as long as study days are never restarted by hand.

## E. Commands (run from the repository root)

```bash
cd /Users/xuao/Projects/prediction-market-value-lab
# 1. G5 pre-flight - only between 12:26 and 01:59 UTC (refused inside the maker window)
.venv/bin/python -m pmvl_worker.lip_cli preflight artifacts/forward/lip-shakedown/2026-09-25 --write
# 2. the freeze manifest (refuses if tracked code or docs are uncommitted)
.venv/bin/python -m pmvl_worker.lip_cli freeze-manifest --write --suite-log artifacts/forward/lip-suite-<commit>.log
# 3. tag exactly the commit the manifest names
git tag -a lip-sim-1.0.0 -m "LIP shadow study: code frozen for day 1" "$(python3 -c 'import json;print(json.load(open("artifacts/forward/lip-freeze.json"))["code"]["git"]["commit"])')"
# 4. push (optional)
git push origin measure-before-selling && git push origin lip-sim-1.0.0
# 5. install the cron line (keeps the maker study's line)
(crontab -l; echo "25 20 * * * /Users/xuao/Projects/prediction-market-value-lab/scripts/record_lip_day.sh") | crontab -
# 6. close the gates - day 1 must be a window that has not started
.venv/bin/python -m pmvl_worker.lip_cli close-gates --day1 2026-10-01
# 7. keep the artifacts that bind the study
git add artifacts/forward/lip-cadence.json artifacts/forward/lip-freeze.json artifacts/forward/lip-gates.json
git commit -m "chore(lip): gates closed - cadence, freeze manifest, day 1"
# 8. verify the first scheduled run (after 2026-10-01 12:26 UTC)
.venv/bin/python -m pmvl_worker.lip_cli run-status
tail -n 2 artifacts/forward/lip-daily/runs.jsonl
cat artifacts/forward/lip-daily/2026-10-01/heartbeat.json
.venv/bin/python -m pmvl_worker.lip_cli freeze-manifest --check
```

To remove the LIP cron line later: `crontab -e` and delete that one line.
