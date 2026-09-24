# Pre-registration: the day-level study, under recorder 1.1.0

Committed **before the first session of day 3**. Days 2 onward accumulate here.

## Why a third document

Study 1 (2026-09-19) answered the session-level question and said in its own verdict
text that the day factor was unsampled. Study 2 (2026-09-20) was a replication, compared
against study 1 rather than pooled with it, because the recorder changed underneath
them - a 1.0.0 skew bound is inflated relative to a 1.1.0 one, and a 1.0.0 session's
snapshot count depended on how busy the tape was.

From day 2 the recorder is stable at 1.1.0. So days 2, 3, ... **can** be pooled, and
pooling them is the only thing that ever produces a date-level interval. This document
fixes how, before day 3 exists.

## Accumulation rule

- Each day contributes **20 sessions of 30 minutes**, same frozen universe
  (`artifacts/forward/universe-study-frozen.json`), same selection rule, same four
  policies, same `QUEUE_MODEL` at 60 seconds.
- Only days recorded under `STRATEGY_VERSION` 1.1.0 are eligible. **Day 1 is excluded**,
  permanently, and not because of its result.
- A day that falls short of 20 sessions is included with the count it reached, and the
  shortfall is reported. It is not re-run.
- **Target: 20 eligible days.** The decision rule below is evaluated once, at 20.

## Primary analysis

Cluster bootstrap resampling **whole calendar dates**, 10,000 iterations, seed 20260919,
on `E[PnL per quote placed]`. The session-level interval is reported beside it as the
optimistic bound, exactly as before.

| outcome | condition |
|---|---|
| `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY` | some policy's family-wise (98.75%) **date-level** lower bound > 0 |
| `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION` | every policy's 95% **date-level** upper bound < 0 |
| `INSUFFICIENT_EVIDENCE` | neither |

## Stated in advance

- Every report before day 20 is an interim and is labelled `INTERIM_NOT_A_DECISION`. The
  study does not stop early on a favourable interim and does not run past 20 hunting for
  one.
- Two dates is the bare minimum the bootstrap accepts and produces an interval so wide
  it decides nothing. That is not a reason to withhold it; it is a reason to label it.
- Days are consecutive where the machine is available. Gaps are recorded as gaps. A
  missing day is a missing draw, not a deleted one - and the dates that were recorded
  are listed in every report so a gap cannot be mistaken for continuity.
- If the accumulating result turns positive, that is the finding. The first two days do
  not get defended.

## Amendment, 2026-09-24: a secondary metric - holding the fill to settlement

Added after days 2 and 3 were recorded and while day 4 was recording, and **before any
hold-to-settlement number had been computed for any day**. The commit history shows the
order.

**Why.** Every number above marks or flattens a fill within five minutes; the analysis
code has always listed `settlement_holding` as not measured. The calibration study
(`calibration-preregistration.md`, run 2026-09-24) found Kalshi's mid-price calibrated
to within about a cent and a taker's loss to be the half-spread plus the fee - which is
to say that the spread goes to whoever quoted it. Whether a *resting* quote keeps that
spread when held to the outcome, or gives it back to the traders who chose to hit it, is
the one maker question not yet asked. It is a different strategy with a different risk,
so it is a separate metric, not a replacement.

**The metric.** For every quote the primary analysis places, with the same fill model
(`QUEUE_MODEL`), the unconditional PnL per quote placed of holding every fill to
settlement: `filled size x (payout of the side bought - fill price - maker fee)`,
unfilled quotes scoring zero, scalar settlements paid as they settled. A quote on a
market that has not settled when the analysis runs is left out of both numerator and
denominator, and counted.

**The decision, at day 20 with the primary.** Two metrics are two chances at a false
positive, so the secondary claims `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY` only if some
policy's **date-level** lower bound at **1 - 0.05/8** (four policies x two metrics) is
above zero. Its negative condition is the primary's: every policy's 95% date-level upper
bound below zero. Everything before day 20 is `INTERIM_NOT_A_DECISION`, as above.
