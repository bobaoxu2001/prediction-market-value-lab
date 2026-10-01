# LIP shadow study: pre-freeze audit (2026-09-30)

> **Status 2026-10-01, after the fifth check: `READY_TO_FREEZE = NO`.** G1-G3 are resolved
> (07fdf8c) and so are three regressions the fifth check found in that resolution (5221289,
> 1070cf6). Six further semantic questions - all but one pre-existing, none introduced by the
> G1-G3 corrections - are open and need a decision; see "The fifth check" and section D. The
> verdict block in A below is the state at the end of the hardening pass (ce24618).

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

### Resolution of G1-G3 (2026-09-30, before day 1)

All three were reproduced again independently before any change, with parameters different
from the fourth check's; each matched the audit. They are **implementation corrections to
semantics Amendment 3 already registered** - no new rule, no Amendment 4, no version bump
(no study day exists). The pre-registration carries a dated correction note under Amendment 3
that says so; the items as registered are unchanged.

**G1 - a crashed day's counted figure.**
1. *Bug.* `health.day_health` judged a day with no `health.json` by the smallest per-lane count
   of successful reads - not the registered rule. Independent reproduction: two lanes failing
   12% independently, killed at 00:44 UTC: 0.8800 counted where the rule gives 0.7756; lane B's
   program ending after 6 h: 0.5715 not counted where the rule gives 0.9984.
2. *Registered.* Amendment 2 item 6, applied to the verified books (Amendment 3.3): a tick counts
   only if every book lane due at it read it; ticks with nothing due, and ticks the recorder
   never reached, do not.
3. *Change.* The recorder's definition of "due" is one generator (`_due_lanes`) used both by the
   closing figure (`_write_health`, behaviour unchanged) and by every checkpoint, which now
   stores the due lanes per tick, run-length encoded, up to its time. `day_health` applies item 6
   to the verified book lines' ticks with those due lanes; ticks after the checkpoint are misses.
   A checkpoint is never written without the record (since 1070cf6: if it cannot be computed,
   the last good checkpoint stands); one written by older code cannot be judged and does not
   count. Cost: about 15 ms of CPU per checkpoint at the end of the day, once a minute - no
   request is added, moved or re-timed by logic.
4. *Evidence.* `tests/test_lip_fourthcheck.py` G1 section, 7 tests: both directions of the
   discrepancy (0.88 -> 0.76 and 0.40 -> 1.00), the 80% boundary, ticks past the checkpoint and
   ticks with nothing due, a missing record, a real recorder killed eight hours in (equals an
   independent item-6 computation), and a closed day's final checkpoint reproducing its closing
   ok_ticks exactly. The re-run reproduction now gives 0.7756 and 0.9984 = the rule.
5. *Re-registration.* None: item 6 and Amendment 3.3 are applied as written.

**G2 - a stale day-end exit reference.**
1. *Bug.* `valuation._exit_charge` took the mid of the last book before the exit as the reference
   however old it was. Independent reproduction: a gap from T0+596 to T0+690 spanning the exit,
   the price moving inside it: LOW's exit charge +$17.51 (a gain), MARK_1H LOW +$43.51.
2. *Registered.* Section 6 prices the flatten against `mid_held(t_e)`, the mid *at* the exit;
   section 5.1 is the registered rule for whether the book at a second is known (bracket span at
   most 2c + 1.5 s, 3.5 s at 1 Hz); Amendment 2 item 11 covers a flatten with "no two-sided mid
   to be measured against": LOW the highest candidate and never below the sale's own price, HIGH
   the lowest. No new price model, no interpolation, and no later observation is used as the
   mid: the walk book's bid is a candidate only because item 11 names it.
3. *Change.* The mid branch (Amendment 3.2) applies only when the exit's second is OBSERVED by
   section 5.1 (`_observed`, the same comparison as the credit's `_BookIndex.bracket`, at the
   day's recorded cadence). Otherwise item 11 - whose candidate list now takes the reference
   book's mid where it has one, as item 11 says ("the reference book's mid or bid").
4. *Evidence.* G2 section, 8 tests: the stale-gap gain (LOW now -$3.49 = minus the fee, the sale's
   own price; HIGH +$17.51, its lowest candidate), an observed exit unchanged (-$8.36 both), the
   boundary at exactly 3.5 s (observed: section 6 in both envelopes) and 3.51 s (unobserved: item
   11), `_observed` equal to the credit's bracket on random books, and a random-gap property:
   LOW's charge is never a gain on an unobserved exit and MARK_1H LOW <= HIGH throughout.
5. *Re-registration.* None: section 6, section 5.1 and item 11 are applied as written.

*What LOW and HIGH do when the registered exit cannot be verified* (the exit's second is
UNOBSERVED, or its reference book has no two-sided mid): the charge is proceeds - |N| x reference
- fee, with the reference chosen from the reference book's mid (or its bid), the walk book's bid
and the last two-sided mid. LOW takes the highest of them and never less than the sale's own
average price, so the flatten is never booked as a gain; HIGH takes the lowest, the favourable
reading, so LOW <= HIGH. When the second is observed and the book two-sided, both use that mid as
section 6 states - where the price moves in the fraction of a second before the walk, section 6
itself can show a small gain in both envelopes; that is the registered rule, unchanged.

**G3 - an empty first checkpoint.**
1. *Bug.* `store.read_checked` short-circuited on an empty set of ends, so the checkpoint written
   before any stream existed let every stream be read in full as if verified. Independent
   reproduction, recorder killed 45 s in: 45 book lines, 23 tape polls, 3 universe rows and 23
   trade batches read as verified. With the read fixed alone, the replay then failed on the first
   pair with no verified close time - which would have blocked every verdict.
2. *Registered.* Amendment 3.3: a crashed day is read up to its last checkpoint, each stream to
   exactly that entry, the rest left out and the rest of the window unobserved; a day with
   neither file is an integrity failure. Amendment 2 item 8 and section 6: a pair with nothing
   recorded enters at its admitted hours and $0; no admitted hour is dropped.
3. *Change.* A checkpoint certifies exactly the streams it lists (an empty one certifies nothing);
   closing ends that omit a stream with files on disk are an integrity failure, empty or not;
   with neither file the optional readers behave as before. On a crashed day, a pair whose close
   time is not in the verified record gets the same zero record as a day never recorded; on a day
   the recorder closed, a missing close time still raises.
4. *Evidence.* G3 section, 9 tests: an empty checkpoint certifies nothing; a day killed before
   its first non-empty checkpoint replays exactly as a never-recorded day (all zeros); a valid
   first checkpoint is read to exactly its ends; neither file still fails to load; health.json
   wins over a checkpoint and is read whole; empty closing ends do not certify; the optional
   readers are unchanged; a carried pair with nothing verified enters at zero on a crashed day and
   still raises on a closed one.
5. *Re-registration.* None: Amendment 3.3, item 8 and section 6 are applied as written.

Measured on the code at ce24618 (the six changed modules swapped back): of the 25 tests, 15
fail - 14 reproduce a G1-G3 defect for the reason they test (G1: 6, both directions among them;
G2: 4, the stale gain among them; G3: 4) and 1 checks the new `_observed` against the credit's
bracket - and 10 pass on both versions because they guard behaviour that must not change (the 80%
boundary with one lane, an observed exit, the 3.5 s observed boundary, F4's neither-file rule, a
valid checkpoint's prefix, health.json winning, a closed day's missing close time, the optional
readers).

**No automatic restart** was added (Phase I): a restart would change tape recovery (above), the
closing tick count, and so the counted-day figure. A failed run is observable instead.

## The fifth check (2026-10-01)

Read-only and narrower than the fourth: one verifier each for G1, G2, G3 and regressions, each
building fresh reproductions rather than relying on the unit tests, then a follow-up verifier for
the corrections the check itself prompted. Three independent skeptics adjudicated G1-1 (three of
three confirmed); session limits interrupted the other skeptic runs, so every finding listed as
open below was reproduced again independently - F1, F2, G1-2, G3-2 and G3-3 by me, the G1-3
regression by both the regression and the follow-up verifier.

**Verified - every check held.**
- *G1 (9 of 9).* The crashed-day figure equals an independent item-6 implementation at 90 kill
  points across 33 scenarios (independent lane failures, a lane due for part of the window, late
  admissions, sleeps of 20 min to 7 h, 2 s cadence, one lane); restored SIGKILL checkpoints and
  torn tails give the same; closed-day `health.json` is identical to ce24618; the books request
  stream is identical on six identical runs; the 80% boundary is inclusive; both directions of
  the old discrepancy are gone.
- *G2 (10 of 10).* About 100,000 paths (random gaps, moves inside gaps, 1 s and 2 s cadence, all
  policies, scenarios and regimes, the replay's REG_LOW budget): no LOW gain on any of 27,000+
  unobserved or no-mid exits (ce24618 booked one 12,371 times); observed exits and DAY_EXIT
  bitwise identical to ce24618; LOW <= HIGH on every path; the span-limit boundary exact; no
  observation later than the exit used as its mid; the day's cadence reaches the valuation.
- *G3 (9 of 9).* No line beyond a checkpoint reaches any study computation; an empty checkpoint
  replays exactly as a never-recorded day; a day with neither file fails to load; a closed day's
  validation is not weakened; tampering before the checkpoint is detected; the optional readers
  are unchanged.
- *Regression (12 of 12).* The only behavioural changes ce24618..5221289 are G1-G3, G1-1 and G1-3;
  `scripts/` is untouched; the third and fourth checks' reproductions show F1-F6 and G4-G6 still
  fixed; requests, streams, heartbeats and wrapper run records are identical across ce24618,
  07fdf8c and 5221289 on identical runs; the day manifest differs only in a crashed day's share.
- *Shakedown replay at HEAD:* labelled `INCOMPLETE_COVERAGE_NOT_PERFORMANCE_RESULT`; 173
  market-days, 4,152 LOW/HIGH pairs, 0 LOW > HIGH in any field, 0 non-finite - byte-identical to
  the pre-fix replay, because with recording stopped at 13:37 no exit has a walk book after it.

**Fixed in this round - regressions in the G1 correction itself (in scope).**

| # | commit | what was wrong | fix |
|---|---|---|---|
| G1-1 | 5221289 | a crashed day reported `due_ticks` for its verified prefix only, so one killed before the window counted toward the seven-empty-days stop and could end the study early | a crashed day is not known to be empty: it resets the run, like a day never recorded |
| G1-3 | 5221289 | a failure computing the checkpoint's due record (an unparseable close time) escaped the recorder's `finally`: lock held, segments open, the next days lost | caught |
| G1-3' | 1070cf6 | the first G1-3 fix still wrote that checkpoint, without the record: it replaced the last good one, so the day was never counted - and a carried pair's unparseable close time became verified, so replay-day raised and `study --final` refused the whole study | no checkpoint is written; the last good one stands, as if killed there (verified through the real CLI); the manifest passes `rule_inputs_missing` through |

**Open - semantic; not fixed under the stop rule. Each needs a decision before day 1.**

| # | where | demonstrated | origin | options (recommended first) |
|---|---|---|---|---|
| F1 | `valuation._fallback` (1-hour mark, no two-sided book in [t+3600, t+3900]) | a gap covering a fill's whole mark window - a sleep of 5 minutes or more - makes LOW mark at the last bid before the gap: MARK_1H LOW +$32.76 instead of -$42.24. The close branch does the same across a gap spanning the close | pre-existing (identical at ce24618); section 6 says only "the held side's best bid", not when | (a) LOW takes a bid only from a book in the window or an OBSERVED second (section 5.1), else 0 - LOW stays pessimistic, as G2 made it for the exit; (b) ratify the stale-bid reading in the correction note |
| F2 | `valuation._exit_charge` | section 6 charges the flatten against `mid_held(t_e)` at participation end; the code reads it at the flatten's arrival t_e + queue delay + L - 42 s later under the REG_LOW budget: charge -$7.94 where the text gives -$27.94. Either direction, depending on the drift | since the first implementation (681c4c3); the G2 test follows the code's `at` | (a) read the reference and its section 5.1 test at t_e, the walk still at t_e + delay + L, as section 6 is written; (b) register the arrival-time reading |
| G1-2 | `recorder._due_lanes` | one span per market across its pairs, so a market re-admitted after a gap is "due" in the gap with nothing to read: closed day 0.8707 where per-pair item 6 gives 0.99998; a crashed day not counted (0.7279) where the rule counts it (0.8571). Only ever lowers the figure; with one book lane (TAPE_PT) it needs the re-admitted market to be the only live one | since 7a6f6f6 | (a) per-pair liveness, exactly what `recorded_markets` reads; (b) register the per-market span |
| G3-2 | `final.payment_status` | on a crashed day a verified within-day withdrawal is ignored (`registry.json` is written only at close): a withdrawn, never-paid program pays in HIGH (up to $25.57 in the reproduction) | since e41da4f; HIGH only | (a) also read withdrawals from the verified programs rows; (b) leave |
| G3-3 | `replay.replay_day` vs the correction note | the note zeroes a pair "of which the verified record holds nothing"; the code zeroes any crashed-day pair without a verified close time, even one with 900 verified book rows (a closed day still raises) | 07fdf8c (this round) | (a) reword the note to the code: such a pair enters at zero whatever else it holds, since the replay cannot run without a close time; (b) zero only pairs with no verified data and raise otherwise - which can block the study |
| E1 | `lip_cli._calendar` | a day the recorder closed but on which it could see no admission - the machine asleep through the window, the venue unreachable all evening, close times that never parse - has `due_ticks` 0 and counts toward the seven-empty-days stop, which can end the study early (never flatters a result) | pre-existing, the same reasoning as G1-1 | (a) count a closed day as empty only if it obtained a complete active listing in the admission period, logged no control errors and had no wall jump over the window; (b) register that such days count |

All six are decisions about what the registration means, or code that does not match it. Decided
now, each is one more line in the dated correction note under Amendment 3 - before day 1, so no
version bump.

**Operational, pre-existing - not semantic, outside G1-G3, recommended before the freeze on your
go-ahead** (after it, any change to these modules shows as drift in `freeze-manifest --check`):
- the hourly gzip of a segment is not atomic: a kill in its 1-2 s window leaves a truncated `.gz`
  beside the intact `.jsonl`, and every reader raises `EOFError` - which stops `study` until the
  day is repaired by hand (reproduced with a real SIGKILL);
- `health.json` is written in place, not atomically (`ends.json` and the heartbeat are);
- under TAPE_PT - the mode the pre-flight selected - the day manifest's tape section counts only
  global-tape polls and reports 0 polls and 0 covered seconds, and the heartbeat's `watermark` is
  `None`; the reader's per-ticker coverage, which the replay uses, is correct;
- `replay._params_for`'s docstring says "2 s fallback" where the code falls back to 1 s;
- not reproduced, from code reading: after one lane raises, sibling lanes are not cancelled and,
  with real httpx providers whose close yields, could rewrite the heartbeat after cleanup.

**The pre-flight ran.** The 12:26 UTC waiter I reported on 2026-09-30 as gone was not: it ran the
G5 pre-flight at 12:26:21 UTC - one read-only request, logged in `lip-offday-requests.jsonl` -
and wrote `artifacts/forward/lip-cadence.json`: **TAPE_PT** (the global tape's p95 cost 6.5
requests/s against the 1.5 threshold), tape limit 1000, `min_ts` honoured, book cadence 1 s. The
file is untracked and left in place; delete it to run G5 yourself.

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

### After the fifth check (2026-10-01)

The blockers above are resolved: G1-G3 (07fdf8c, 5221289, 1070cf6), and G5 (the pre-flight ran).
The commit at HEAD is **not yet safe to tag**. The remaining blockers are the six open semantic
decisions of the fifth check - F1, F2, G1-2, G3-2, G3-3 and E1 - and your acceptance of the
semantic commits made since e41da4f. Once decided and implemented, the freeze needs a short check
of those lines only, the suite, `freeze-manifest --write` twice with identical digests, and then
the operator steps in E. Day 1 moves to the first window after that; `close-gates` accepts any
day whose window has not started.

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
