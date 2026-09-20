# Pre-registration: does the 2026-09-19 maker result replicate on another day?

Committed **before the first session of 2026-09-20 was recorded**. The git history is
the timestamp.

## Why this exists

The first study returned
`CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION` on twenty sessions
- and stated in its own verdict text that the day factor was unsampled, because twenty
half-hours on one calendar date is one draw of the day. That is the only thing it left
open, and it is not closed by more sessions on the same afternoon however many quotes
they hold.

This is the second draw.

## What is identical to study 1

The frozen universe (`artifacts/forward/universe-study-frozen.json`, 21 series), the
per-session selection rule (top 8 open events by 24h volume), 20 sessions of 30 minutes
at a 10-second cadence, the four declared policies, `QUEUE_MODEL`, the 60-second
markout horizon, and the cluster bootstrap with 10,000 iterations.

## What is not, and why it blocks pooling

Study 1 was recorded under `STRATEGY_VERSION` 1.0.0. Two recorder faults were fixed
afterwards and the version is now **1.1.0**:

- `skew_bound_ms` is measured around the socket rather than around the rate limiter, so
  it no longer counts this process's own backlog as venue latency. A 1.0.0 skew bound is
  **inflated** relative to a 1.1.0 one.
- the tape pull has a time budget, so it can no longer starve the book capture. Study 1's
  sessions fell from 952 snapshots to 8; study 2's will not.

Both change what a recorded line means. **The two days are therefore not pooled into one
estimate.** The reader already refuses to mix recorder versions and that refusal stands.

## Primary analysis: replication by comparison, not by pooling

For each of the four policies, computed on day 2 alone and compared with day 1's
already-published numbers:

1. **Sign.** Is day 2's mean `E[PnL per quote]` negative?
2. **Containment.** Does day 2's mean fall inside day 1's 95% session-level interval?
3. **Exclusion.** Does day 2's own 95% session-level interval exclude zero?

| outcome | condition |
|---|---|
| `REPLICATED` | all four policies negative on day 2, **and** at least three of four day-2 means fall inside day 1's interval |
| `FAILED_TO_REPLICATE` | any policy's day-2 interval lies entirely **above** zero |
| `INCONCLUSIVE` | anything else |

Day 1's numbers are already known and published. That is stated rather than pretended
away: this is a replication, not a blind test, and the protection against reading the
result into the data is that the comparison rule above is fixed before day 2 exists.

## Secondary, and labelled as such

A two-date cluster bootstrap over both days. Two clusters is the bare minimum the
procedure accepts and the interval will be extremely wide; it is reported because it is
the first time the day factor is resampled at all, and it must carry the recorder-version
mismatch on its face. **It does not decide anything.**

## Stated in advance

- A day that produces fewer sessions than planned - the venue closes, the universe
  empties, the process dies - is reported with the count it reached. It is not re-run
  and the shortfall is not hidden.
- Two days is two draws. Neither this study nor its predecessor can support a claim
  about the population of days; twenty days would, and this is the second of them.
- If day 2 contradicts day 1, that is the finding. The first result does not get
  defended.
