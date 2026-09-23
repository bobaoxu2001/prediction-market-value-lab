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
