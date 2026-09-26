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
