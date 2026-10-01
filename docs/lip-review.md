# LIP study: the adversarial review before the freeze

Three independent reviewers read the code against `lip-preregistration.md`, each with one lens,
and reported only problems they had reproduced with a script. 35 findings; all were accepted
and fixed in `ee9dc69`, with a regression test for each in `tests/test_lip_review.py` (`9a47344`).
Writing those tests found two further gaps in the fixes, fixed in the same commit.

| # | lens | severity | where | what was wrong |
|---|---|---|---|---|
| 1 | scoring | high | `simulator.py:500` | score_path ignores inventory decreases, so both envelopes (LOW included) credit contracts that are no longer resting. |
| 2 | scoring | medium | `replay.py:60` | Each study day's payout is capped at the full per-account cap, not at the cap minus what earlier days already paid (pre-registration 5.8 step 2). |
| 3 | scoring | low | `simulator.py:559` | The LOW counterfactual removes the quantity of a HEAVY cross fill from every opposite-side level at or through our price. |
| 4 | scoring | low | `simulator.py:130` | LOW's open test treats a missing bracketing status poll as open. |
| 5 | scoring | low | `simulator.py:637` | HIGH uses the same N_total as LOW, so closed seconds stay in HIGH's payout denominator. |
| 6 | fills | high | `valuation.py:122` | MARK_1H exit charge becomes +proceeds when others' book has no two-sided mid at the exit. |
| 7 | fills | high | `simulator.py:374` | HEAVY never runs the cross/orphan checks on an order's first book after landing. |
| 8 | fills | medium | `simulator.py:423` | The event loop `break`s at the participation-end event. |
| 9 | fills | medium | `simulator.py:500` | score_path ignores inventory decreases. |
| 10 | fills | medium | `simulator.py:237` | A replenishment child's queue is initialised from others' size at the price only. |
| 11 | fills | medium | `simulator.py:302` | Look-ahead through inventory. |
| 12 | fills | medium | `simulator.py:397` | The segment gate (a decision) calls _is_open(md, t, True). |
| 13 | fills | medium | `reader.py:106` | A pair carried into a later day of a multi-day program loses its close_time. |
| 14 | fills | medium | `simulator.py:365` | Each print is applied independently to every live order (lines 357-367; fills.apply_print). |
| 15 | fills | low | `fills.py:69` | The clock uncertainty of section 4.2 is not implemented. |
| 16 | fills | low | `simulator.py:275` | A placement already in flight when a stop fires is landed through the cancel_on_land path and returns before being added to pending_first_book. |
| 17 | fills | low | `simulator.py:376` | FIFO's queue initialisation, max(decision-book level, first-book level), can leave more queue ahead of us than others have at the price when the level shrank while we were in flight. |
| 18 | recorder / statistics | high | `lip_cli.py:193` | `study --final` pools whatever lip-daily/<day> directories happen to exist. |
| 19 | recorder / statistics | high | `lip_cli.py:91` | The pre-registered TAPE_PT fallback can never take effect. |
| 20 | recorder / statistics | medium | `reader.py:182` | A pair admitted on an earlier day is carried into day D only if its program appears in day D's programs stream (`k in registry.programs`). |
| 21 | recorder / statistics | medium | `recorder.py:183` | A skipped day (maker still running at 13:30, or an early exit such as LockHeld) writes no day directory, study.json or health.json. |
| 22 | recorder / statistics | medium | `simulator.py:397` | Look-ahead: the segment gate calls _is_open(md, t, True), whose LOW branch (line 128) also requires the NEXT status poll to be open. |
| 23 | recorder / statistics | medium | `reader.py:231` | A status gap reads as open. |
| 24 | recorder / statistics | medium | `store.py:215` | _raw_rows returns silently at the first undecodable line; the pinned read_log raises instead. |
| 25 | recorder / statistics | medium | `store.py:222` | A recorder restarted within the same UTC hour (after a clean exit or an exception, both of which gzip the open segment through writer.close()) writes a new <stream>.jsonl next to <stream>.jsonl.gz. |
| 26 | recorder / statistics | medium | `recorder.py:593` | The counted-day share divides by a fixed 44,100 one-second ticks and takes the minimum over every book lane that made any request (the same logic is in health.py:47). |
| 27 | recorder / statistics | medium | `recorder.py:172` | The 02:00-12:25 UTC refusal is checked only once, at start, and window.hard_stop is never enforced. |
| 28 | recorder / statistics | medium | `lip_cli.py:199` | `programs-paid` (and preflight's live probe at line 127) sends Kalshi requests with no 02:00-12:25 refusal and no wait on the maker lock. |
| 29 | recorder / statistics | medium | `final.py:44` | isolation_breaches reads request times only from books, tape_polls and status. |
| 30 | recorder / statistics | medium | `http.py:50` | On the tape, status and control lanes (attempts=2), a 429 followed by a successful retry is never recorded. |
| 31 | recorder / statistics | low | `recorder.py:275` | The decision to pause book lanes uses the day's cumulative 429 ratio (rate_limited / requests), not the 10-minute window of section 8. |
| 32 | recorder / statistics | low | `simulator.py:65` | The known REG_LOW deviation (replay.py:92, a static share of 10/peak writes per second) is documented as 'strictly more restrictive' than the shared W=10 bucket, but it is not. |
| 33 | recorder / statistics | low | `replay.py:60` | The per-account cap is applied afresh each study day (cap_left = the full cap), so a multi-day program can pay cap × days. |
| 34 | recorder / statistics | low | `lip_cli.py:181` | replay-day's dollar guard only checks that lip-final.flag exists. |
| 35 | recorder / statistics | low | `final.py:91` | payment_status verifies a program when its paid row matches the terms first seen. |

## The ones that would have flattered the strategy

- A missing mid at the day-end exit turned a $3.36 round-trip loss into +$76 in MARK_1H (#6).
- HEAVY skipped its cross and orphan checks on an order's first book - the fast-vanishing bids
  it exists to count (#7); in one replay about 40% of HEAVY's fills.
- One print filled a parent and its replenishment child for more than it carried (#14), and
  both claimed the same room under the target in LOW credit (#10).
- A decreased size kept earning credit (#1, #9).
- Inventory and the gate read information a latency, or a status poll, before it existed (#11, #12, #22).
- Fills after the day's end but before the cancels landed were dropped (#8).
- The cap was applied afresh each day (#2, #33).
- The pre-registered per-ticker tape fallback could never take effect (#19).
- `study` pooled whatever directories existed and silently dropped a day without a replay (#18).

## The re-review of the fixes

Two reviewers: one re-ran every original reproduction against the fixed code, one hunted for
problems the fixes introduced. 19 findings, all accepted, with a regression test for each in
`tests/test_lip_rereview.py`. Two were regressions of mine:

- the exit-charge fix still let LOW take the favourable reference when the reference book's held
  side was empty but the walk book's was bid - the +$76 bug in a new form (R11, R13, R2);
- the counted-day rewrite counted only ticks the recorder happened to reach, so a day the machine
  slept through could count (R12); the pre-registered rule - successful reads on 80% of the
  window's ticks - is restored.

| # | lens | severity | where | what was wrong |
|---|---|---|---|---|
| R1 | fixes | medium | `reader.py:224` | Original #24 is only partly fixed. |
| R2 | fixes | medium | `valuation.py:100` | The fix for original #6 is inconsistent and breaks LOW <= HIGH for MARK_1H. |
| R3 | fixes | medium | `recorder.py:387` | The fix for original #26 lets book-lane pauses vanish from the counted-day test. |
| R4 | fixes | low | `recorder.py:416` | The fix for original #31 is wrong, so the book lanes can never pause. |
| R5 | fixes | low | `simulator.py:127` | The #23 fix (a failed or unanswered status poll becomes a StatusPoint with market_status '') interacts with the #4 fix in two ways the pre-registration does not allow. |
| R6 | fixes | low | `simulator.py:504` | Original #8 is only partly fixed. |
| R7 | fixes | low | `simulator.py:501` | Original #32 is fixed only for gate openings. |
| R8 | fixes | low | `recorder.py:530` | Residual look-ahead from original #12/#22. |
| R9 | fixes | low | `simulator.py:349` | Residual look-ahead from original #11. |
| R10 | fixes | low | `lip_cli.py:349` | A gap in the original #18 fix ('a recorded day without its replay stops the analysis'). |
| R11 | regressions | high | `valuation.py:145` | The MARK_1H exit-charge fallback still books the full sale proceeds as profit in LOW, which is the original +$76 bug in a new form. |
| R12 | regressions | high | `recorder.py:396` | The new counted-day share lets a day count when most of the window was never recorded. |
| R13 | regressions | medium | `valuation.py:141` | The missing-mid fallback in the exit charge is in the wrong direction for each envelope. |
| R14 | regressions | medium | `recorder.py:350` | The trailing-window rewrite of the 429 rule means the book-lane pause (section 8) can never fire. |
| R15 | regressions | medium | `recorder.py:392` | A study day on which nothing is admitted is now a counted day. |
| R16 | regressions | medium | `recorder.py:530` | There is still a look-ahead in the status path the fix was meant to clean up. |
| R17 | regressions | low | `simulator.py:504` | `stop_after` is fixed when the end event is processed, as the latest landing then queued. |
| R18 | regressions | low | `simulator.py:461` | THROUGH and FIFO are supposed to narrow every fillable window by eps (Amendment 2 item 3), but this only works at the start of the window. |
| R19 | regressions | low | `replay.py:163` | The cross-day cap is tracked separately per envelope. |

## The third check (2026-09-30)

One more pass over the re-review's fixes, re-running each reproduction against the code and
then reading the lines those fixes changed. Six findings, each reproduced, all accepted, with a
regression test for each (`tests/test_lip_thirdcheck.py`, and `tests/test_lip_recorder.py` for
F1 and F4). All six came from the 19 re-review fixes; four of them would have misled the
analysis in the strategy's favour or blocked it outright:

- F1: the re-review's counted-day rule (R12) credited reads to the grid second their jittered
  send landed in, so a day on which every read succeeded scored 0.58-0.76 and did not count for
  a third of possible process start phases;
- F3: the exit-charge fix (R11/R13) applied its no-mid rule to every exit, letting HIGH value an
  ordinary flatten at the bid instead of the mid - half a spread per contract in HIGH's favour.
  It is likely most of the 7-10% rise in HIGH's MARK_1H between the shakedown's replays of
  2026-09-26 and 2026-09-30;
- F4: requiring `health.json` (R-integrity) made a day whose recorder died unreadable, and one
  such day would have refused every verdict.

| # | severity | where | what was wrong | fix |
|---|---|---|---|---|
| F1 | high | `recorder.py` | Reads credited to the grid second of their jittered send; a perfect day could fail the 80% test | Ticks on the window's grid; a read credited to its scheduled tick; a late wake rejoins the grid (Amendment 3.1) |
| F2 | medium | `valuation.py` | HIGH's fallback mark (last mid) could sit below LOW's (current bid): LOW > HIGH on MARK_1H | HIGH takes the higher of the last mid and the bid (3.2) |
| F3 | medium | `valuation.py` | HIGH charged a two-sided exit against the bid, not the mid | The mid in both envelopes when there is one; candidates only without (3.2) |
| F4 | medium | `reader.py` / `store.py` | A day without `health.json` could never be read, and blocked the final analysis | Stream ends checkpointed each minute; such a day read to its checkpoint, the rest unobserved (3.3) |
| F5 | low | `simulator.py` | The post-end loop could stop before a late placement landed, dropping its fills | Drain due landings before testing for the end (3.5) |
| F6 | low | `simulator.py` | The gate read an unknown latest poll as closed and sat out the hour | The latest known status only; unknown stops nothing (3.4) |

Found while fixing them: `close-gates` would have accepted a day 1 already in the past when run
before 12:00 UTC, and a study with nothing admitted crashed the bootstrap. Both fixed and tested.

## The fourth check (2026-09-30)

A read-only check of the third check's fixes (e41da4f) and of the pre-freeze hardening
(f22acd8). Six findings, each reproduced; the three in study-semantic code are **documented,
not fixed**, pending a decision (see `docs/lip-prefreeze-audit.md`); the three operational
ones are fixed.

| # | commit | severity | where | what is wrong | status |
|---|---|---|---|---|---|
| G1 | e41da4f | medium | `health.py` `day_health`, via `lip_cli._day_status` and `daymanifest.qualify` | a crashed day's counted figure uses min-over-lanes of ok lines, not Amendment 2 item 6 (every due lane at each tick): 0.877 counted where the rule gives 0.774; 0.572 not counted where it gives 0.999 | **open - semantic** |
| G2 | e41da4f | medium | `valuation.py` `_exit_charge` | the reference mid has no staleness bound: across a book gap spanning the exit, LOW books the move across the gap as a gain (+$11.50 exit charge; MARK_1H +$31.50 vs +$16.50 before e41da4f) | **open - semantic** |
| G3 | e41da4f | low | `store.py` `read_checked` | the first checkpoint is empty; a kill before the next one makes every stream read in full as if verified | **open - semantic** |
| G4 | f22acd8 | low | `lip_cli.py` `run_until_signalled`, `recorder.py` finally | a second signal a millisecond later cancelled the cleanup: stale lock, open segment, the next day lost | fixed: cancel once; synchronous cleanup before any await; no write after close |
| G5 | f22acd8 | low | `scripts/record_lip_day.sh` | SIGQUIT (and USR1/USR2/ALRM) killed the wrapper with no end record; caffeinate ended with the wrapper | fixed: all catchable signals trapped; caffeinate waits on the recorder |
| G6 | f22acd8 | low | `recorder.py` `_note_wall_jump` | the commit said only a `tick` field was added; `wall_jump` rows also enter the chained `lanes` stream | documented: they stay (tamper-evident), ignored by every study computation |

## The fifth check (2026-10-01)

A read-only check of the G1-G3 corrections (07fdf8c): verifiers for G1, G2, G3 and regressions,
each with fresh reproductions, then a follow-up verifier for the corrections it prompted. Every
check the verifiers ran held (G1 9/9, G2 10/10, G3 9/9, regression 12/12, follow-up 9/9). Their
findings, with the full write-up and options in `docs/lip-prefreeze-audit.md`:

| # | severity | where | what is wrong | status |
|---|---|---|---|---|
| G1-1 | medium | `lip_cli._calendar` | a crashed day counted toward the empty-day stop (introduced by 07fdf8c) | fixed, 5221289 |
| G1-3 | medium | `recorder._checkpoint` | a failing due record skipped cleanup (07fdf8c), then replaced the last good checkpoint (5221289) | fixed, 5221289 and 1070cf6 |
| F1 | medium | `valuation._fallback` | LOW's 1-hour mark can use a stale bid across a gap covering the mark window | **open - decision** |
| F2 | medium | `valuation._exit_charge` | the exit's mid is read at the flatten's arrival, not at t_e as section 6 says | **open - decision** |
| G1-2 | medium | `recorder._due_lanes` | per-market span: a re-admitted market is "due" in the gap between its pairs | **open - decision** |
| G3-2 | low | `final.payment_status` | a crashed day ignores verified within-day withdrawals (HIGH only) | **open - decision** |
| G3-3 | low | `replay.replay_day` | zeroes a crashed-day pair without a close time even when it has verified books; the note says "holds nothing" | **open - decision** |
| E1 | low | `lip_cli._calendar` | a closed day on which no admission could be seen counts as empty | **open - decision** |
| G3-4 | low | `store.gzip_file` | not atomic: a kill during rotation leaves a truncated `.gz` that every reader rejects | open - operational |
| G3-5 | low | `recorder._write_health` | `health.json` not written atomically | open - operational |
| F3 | low | `replay._params_for` | docstring says 2 s fallback; the code uses 1 s | open - documentation |

## The sixth check (2026-10-01)

A read-only check of the author's 2026-10-01 decisions (2afe10b: F1, F2, G1-2) and the approved
operational fixes (75a5356), in three tracks - valuation; recorder and operations; regression,
separability and test genuineness - with two independent skeptics per finding. Every check of the
code held (details in `docs/lip-prefreeze-audit.md`). Findings:

| # | severity | where | what is wrong | status |
|---|---|---|---|---|
| T1 | low | `tests/test_lip_ops_fixes.py` (for `store.gzip_file`) | no test detected a reversion of the atomic gzip: reverted alone, it passed every test while a kill mid-copy left a truncated `.gz` that no reader could open | fixed, 0ee7d83 (test only) |
| T2 | low | the audit's test counts | "6 fail at ba2fbab, 3 guard" needed its interpretation: 4 of the 6 detect real defects, 1 concerns a file only the new code creates, 1 is a guard that fails only on a new key | documented |
