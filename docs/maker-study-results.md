# Maker study: 20 sessions, and what they settle

**VERDICT: `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION`**

> every declared policy's 95% upper bound is below zero on the **session-level**
> interval, on the single date 2026-09-19, so the day factor is unsampled and nothing
> here extends to a different day

Pre-registered at [`96f550d`](../docs/maker-preregistration.md), before any session was
recorded. The decision rule was evaluated once, at the planned count of 20. H1 - that
some declared maker policy has positive unconditional expectancy - **is not supported**.

## The study

20 sessions × 30 minutes, 2026-09-19 08:45 to 19:38 UTC, against a universe of 21
series frozen before session 1.

| | |
|---|---|
| snapshots | 10,398, all single-request |
| prints | 128,551 |
| rejections | 2, both `REQUEST_FAILED`, in session 15 |
| distinct events | 31 |
| distinct quoted markets | 143 |
| skew bound | 98.6% ≤ 1 s, 99.91% ≤ 2 s |
| composition | MLB totals and spreads, WTA tennis, one rain event, KBO, one LoL map |

## Primary result, exactly as pre-registered

`QUEUE_MODEL`, 60-second markout, unfilled quotes contributing zero, cluster bootstrap
resampling whole sessions, 10,000 iterations, seed 20260919.

| policy | quotes | fills | E[PnL] / quote | 95% CI | family-wise 98.75% | sessions negative |
|---|---|---|---|---|---|---|
| improve-no | 5,317 | 1,120 | −$0.043 | [−0.082, −0.012] | [−0.095, −0.007] | 16/20 |
| improve-yes | 5,317 | 588 | −$0.050 | [−0.087, −0.021] | [−0.099, −0.014] | 16/20 |
| join-no | 5,317 | 597 | −$0.035 | [−0.064, −0.012] | [−0.074, −0.007] | 13/20 |
| join-yes | 5,317 | 333 | −$0.032 | [−0.061, −0.009] | [−0.070, −0.004] | 14/20 |

Every interval, per-policy and family-wise, lies entirely below zero.

## The loss is broad, not tail-driven

This is the part that decides what the result *means*. A mean dragged down by a handful
of resolution jumps says "avoid the tail". A mean produced by a small persistent tilt on
ordinary fills says the quote is priced wrong, and no tail management fixes it.

improve-no, 1,120 fills with a 60-second markout:

| | |
|---|---|
| median | **0.0000** |
| mean | −0.22c |
| **mean of the middle 90%** | **−0.26c** |
| losing / winning / flat | 451 / 446 / 138 |

Trimming the outer 10% does not rescue it - the middle of the distribution loses at the
same rate as the whole of it. Wins and losses are nearly equal in count and near-symmetric
in size; what is left over is a small, persistent tilt against the resting quote. That is
adverse selection, and it is structural rather than episodic.

The hedged pair is worse and needs no statistics at all: buy the fill, close it into the
other side of the same market, which settles for exactly $1. That is `1 − price − hedge
cost`, an identity per fill, and it runs **−2.7c to −4.1c** across the four policies.

## The horizon, which cuts against the result

The markout horizon was fixed at 60 seconds in the pre-registration. Reading the other
two recorded horizons afterwards is descriptive, and it is worth reporting because it
qualifies the claim rather than supporting it.

| policy | E[PnL] @30s | E[PnL] @60s | E[PnL] @300s |
|---|---|---|---|
| join-yes | −$0.039 * | −$0.032 * | −$0.024 |
| improve-yes | −$0.056 * | −$0.050 * | −$0.041 |
| join-no | −$0.020 | −$0.035 * | −$0.023 |
| improve-no | −$0.034 * | −$0.043 * | −$0.043 * |

`*` = 95% session-level upper bound below zero.

**The mean is negative in all twelve cells.** But the interval excludes zero for all four
policies only at 60 seconds - three of four at 30 seconds, and one of four at 300. Longer
horizons carry more of the market's own movement, so the intervals widen while the point
estimate stays put.

The pre-registered horizon is therefore the one where the result looks strongest, and it
would be easy to present that as a clean sweep. It is not: at five minutes, three of the
four policies are negative without being distinguishable from zero. What survives at
every horizon is the *direction*, and the hedged-pair identity, which does not depend on
a horizon at all.

## Sensitivity, both labelled as such

The bare command is the pre-registered analysis and nothing else. Both runs below were
produced with explicit filters and are recorded as `SENSITIVITY - not the pre-registered
primary analysis`.

**A - drop the 9 snapshots whose skew bound exceeded 2 s.** Verdict unchanged, estimates
move in the fourth decimal (improve-no −0.0434 → −0.0435). Skew is a non-issue by volume.

**B - drop the seven starved sessions** (fewer than 400 snapshots; sessions 14-20). The
result gets *stronger*, not weaker:

| policy | quotes | mean | 95% CI | sessions negative |
|---|---|---|---|---|
| improve-no | 4,810 | −$0.050 | [−0.093, −0.018] | **13/13** |
| improve-yes | 4,810 | −$0.049 | [−0.089, −0.016] | 11/13 |
| join-no | 4,810 | −$0.039 | [−0.071, −0.013] | 10/13 |
| join-yes | 4,810 | −$0.034 | [−0.065, −0.008] | 9/13 |

The decision machinery correctly reports `INTERIM_NOT_A_DECISION` for B, because 13 is
not the planned count. That is the stop rule doing its job even where it is inconvenient:
a sensitivity run may inform, it may not decide.

## What went wrong during the study

**The tape pull starved the book capture.** Snapshots per session fell from 952 to 8 by
session 17 while the trade count *rose*. A hundred trade requests per cycle take longer
than a ten-second interval on a busy evening, and nothing reported that the cadence had
collapsed. Sessions 14-20 contributed 24-147 quotes each against roughly 450 for the
healthy ones - and each still carries equal weight as one bootstrap cluster, which is
what sensitivity B tests. Fixed in `6ad09a4`: the tape now has a time budget and a
rotating cursor, and cycles that hit the budget are counted.

**The skew bound was measured in the wrong place.** It was timed from before the
rate-limiter acquire, so it counted this process's own backlog as venue latency. That is
the "944-second skew" in session 20 - a request the venue answered in under a second. The
old number was conservative, so nothing was wrongly accepted, but a measurement that
reports local queueing as venue latency is not a measurement. Now stamped around the
socket.

Neither fault changes the recordings. Both are limitations of this study, not of the
recordings it produced.

## What this settles, and what it does not

**Settled, within one day, at the pre-registered horizon.** Across 5,317 quotes per
policy, 143 markets and four independently declared policies, the maker route's
unconditional expectancy is negative with every interval excluding zero at 60 seconds,
and the loss is broad rather than episodic. At five minutes the direction holds and the
intervals widen until only one of the four excludes zero - so the strong form of the
claim belongs to the horizon that was fixed in advance, and the durable form is the
direction plus the hedged-pair identity. The
inadmissible estimator remains the only thing that ever turns positive, which is why it
was declared inadmissible before the data existed.

**Not settled: the day factor.** Twenty half-hours on one calendar date is *one* draw of
the day. The verdict text says so itself rather than letting a session-level interval
read as a date-level one. Twenty separate days would close it; twenty sessions in an
afternoon cannot, however many quotes they contain.

**Not settled: anything outside this universe.** The frozen 21 series delivered mostly
in-play sports and one weather event. In-play books move on every point, which is why
fill rates here (14-49%) are an order of magnitude above the pilot's 4%. The finding is
about the regime the frozen universe actually delivered.

**Also unmeasured, and stated in the report itself:** the quotes were never sent, so
nothing here captures how the book would have reacted to them.

## Reproduction

```bash
pmvl record census --out artifacts/forward/universe-2026-09-19.json
pmvl record sessions --universe artifacts/forward/universe-study-frozen.json \
  --count 20 --duration 1800 --interval 10 --watch 100 --out artifacts/forward/maker-study
pmvl replay maker-study artifacts/forward/maker-study
pmvl replay maker-study artifacts/forward/maker-study --max-skew-ms 2000
pmvl replay maker-study artifacts/forward/maker-study --min-session-snapshots 400
```

The raw L2 and tape - 221 MB across the twenty sessions - are untracked by design, as
the research archive is. Everything derived is committed: the pooled report, both
sensitivity reports, each session's selection census, and `study_quote_ledger.csv.gz`,
which holds **all 21,268 quotes** across the four policies with session and date on every
row. The unfilled quotes are most of that file and all of the denominator; a repository
carrying only the summary could not show that the fill rate was computed over every
attempt rather than over the ones that worked.
