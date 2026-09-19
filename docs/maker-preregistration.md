# Pre-registration: does a maker quote have positive unconditional expectancy?

Written and committed **before the first session of this study was recorded**. The git
history is the timestamp. Nothing below may be changed once session 1 starts; a change
means a new `ANALYSIS_VERSION`, a new pre-registration, and a session count that starts
again at zero.

The previous phase left exactly one question open. Two of the three routes were closed
decisively - no synchronized dislocation in 32,403 observations, and no atomic multi-leg
primitive on the venue - and the maker route split in two. The hedge-immediately branch
is negative by an accounting identity per fill and needs no statistics. The
inventory-carrying branch came out negative on all four policies with every interval
straddling zero, on **one session**, which is one draw of the session factor. That is
what this study is for.

## Hypothesis

**H1**: some declared maker policy has `E[PnL per quote placed] > 0` under an
admissible fill estimator, net of fees, over the pre-registered universe.

**H0**: it does not.

## Universe, frozen

The candidate universe is the **set of series** that qualified in the census frozen at
`artifacts/forward/universe-study-frozen.json`: at least two two-sided legs, non-zero
24h volume, and few enough legs to be captured in one request.

That file is the **union of two censuses taken at different hours of the trading week**,
and the reason is worth stating. The first scan for this study ran at 08:37 UTC on a
Saturday - the quietest hour of the week for US markets - and returned an almost
entirely Asian-session universe of KBO baseball and WTA tennis. A ten-hour study
starting from it would have spent its middle sessions with nothing open, and that
emptiness would have been an artefact of when the census clock happened to fire rather
than a fact about the market. Unioning it with the 2026-09-18 11:30 UTC census gives a
universe with something open around the clock. Amended **before session 1**, with no
session data in existence and none seen: the only recording made before this point is a
90-second smoke test of the runner, which used the narrow universe and a duration
matching no part of this study, and which is discarded rather than pooled.

Series rather than events, deliberately. Events roll over - a baseball game listed today
has expired by tomorrow - so freezing events would deplete the universe within a day and
force a mid-study change. Freezing the series fixes the market universe while letting
the instruments roll, which is the level at which "the same experiment, twenty times" is
actually true.

Per-session selection is mechanical: the top 8 currently-open events inside those
series, ranked by 24h volume, each with at least 2 two-sided legs and at most 100 legs.
No discretion, no per-session substitution, no dropping a session because its events
looked unpromising.

## Sessions

- **20 sessions**, each **30 minutes** of capture at a 10-second cadence.
- Sessions do not overlap. Each runs its own selection at its own start time.
- A session that captures zero snapshots is still a session and is reported; it is not
  re-run.
- Every session records `session_id`, start time and calendar date.

## Primary analysis

- **Estimator**: `QUEUE_MODEL`. `TRADED_THROUGH` is reported as the lower bound.
  `TOUCHED` is inadmissible and reported only for contrast.
- **Endpoint**: unconditional `E[PnL per quote placed]`, markout horizon **60 s**,
  unfilled quotes contributing exactly zero.
- **Policies**: the four already declared in the source - join or improve one tick, on
  each side. All four are reported. No fifth policy may be added mid-study.
- **Clustering**: cluster bootstrap resampling **whole sessions**, 10,000 iterations,
  seed 20260919. A date-level bootstrap is reported beside it, because sessions sharing
  a calendar date share that day's conditions and the session-level interval does not
  price that.
- **Multiplicity**: four policies are one family. The family-wise claim uses a
  Bonferroni-adjusted interval (98.75%). The per-policy 95% interval is reported too,
  labelled as not carrying the correction.

## Decision rule

Evaluated once, at 20 completed sessions.

| outcome | condition |
|---|---|
| `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY` | some policy's **98.75%** lower bound > 0 |
| H0 sustained | **every** policy's 95% upper bound < 0 |
| `INSUFFICIENT_EVIDENCE` | neither - intervals still straddle zero at 20 sessions |

Interim numbers may be reported at any point and are explicitly **not** decisions. The
study does not stop early for a favourable interim, and it does not continue past 20
looking for one.

## Stated in advance, so it cannot be discovered later

- A positive result under `TOUCHED` alone is not a result. On the pilot session it was
  the only estimator that turned positive, taking join-no from −0.92c to +1.04c.
- The hedged-pair number is an accounting identity, not an estimate. It is reported per
  session and is not part of this hypothesis test.
- The pilot session (2026-09-18, `artifacts/forward/run4`) is **not** pooled into this
  study. It was recorded under a different cadence and a different selection rule, and
  it is the observation that motivated the study - including it would be using the same
  data to generate and to test the hypothesis.
- Sessions recorded on the same calendar date are not independent draws in time. If all
  20 land on fewer than 20 dates, the date-level interval is the binding one and the
  session-level interval is reported as the optimistic bound.

## Added after pre-registration, before session 1

Two diagnostics, neither of which changes a threshold or a decision.

**Jaccard overlap between the market sets of consecutive sessions**, plus the count of
distinct markets across the study. The design cannot rule out that a frozen universe and a top-by-volume rule
hand consecutive sessions the same books an hour apart, in which case the session-level
interval measures autocorrelation rather than independent variation. That is worth
knowing and it is not knowable in advance, so it is measured and reported rather than
assumed away. It does not enter the decision rule above.

**The shape of the per-fill markout** - percentiles, win/loss/flat counts, and means
with the worst 1% and the outer 10% trimmed. A mean dragged down by a handful of
resolution jumps and a mean produced by a small persistent tilt on ordinary fills are
the same number and completely different findings: the first says avoid the tail, the
second says the quote is priced wrong and no tail management fixes it. Added at session
5, from the interim data, and reported for every policy - it describes a result, it does
not decide one.

## What this study cannot settle

Holding to settlement, other venues, other asset classes, and any strategy with a view
on direction. It measures one thing: whether resting a quote and being filled is, on
average over every attempt, worth more than nothing.
