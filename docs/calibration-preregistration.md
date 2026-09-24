# Pre-registration: is any price range on Kalshi mispriced for a taker?

Committed before any return by price range has been computed. Written 2026-09-24.

## Why this, and why now

Every route measured so far was arbitrage, maker spread, or speed, and all of them
have come back unprofitable (`forward-measurement-results.md`, `maker-study-results.md`,
`polymarket-routes.md`, `weather-source-lag.md`, `crypto-settlement-lag.md`). The one
family left is systematic mispricing: whether contracts in some price range win more
often than their ask plus the fee implies - the favorite-longshot bias documented on
betting exchanges. It needs no speed, only a price that is wrong on average.

## The trade being tested

At a time fixed before the outcome, buy one side of a market at its quoted best ask
and hold it to settlement. One leg, one quote, one instant - no synthesis across
snapshots.

- **Observation time** `t_obs = floor_hour(expected_expiration_time) - H`, for
  H in {1 h, 6 h, 24 h}. `expected_expiration_time` is set from the schedule, not from
  what happened: on MLB games it is exactly the listed first pitch plus three hours,
  whatever time the game really ended. Anchoring on the *actual* close would leak the
  outcome (a "will X happen" market closes early because X happened; a game's length
  depends on who is winning), so the actual close is never used to place `t_obs`.
- **The market must be open at `t_obs`**: `open_time <= t_obs < close_time`. That is
  visible at the time.
- **The quote** is the last hourly candle ending at or before `t_obs`, carried forward.
  Validated against 1,000+ recorded order books at hour boundaries before this was
  written: the carried-forward candle ask equals the recorded best ask 98.5% of the
  time, is within a cent 99.8% of the time, and is not biased cheap (mean error
  -0.001c YES, -0.008c NO). A candle writing 1.00 / 0.00 means that side is empty.
- **Liquidity filter, observable at `t_obs`**: open interest on that candle >= 100
  contracts.
- **Both sides are candidates**: YES at the YES ask, NO at 1 - YES bid, each where
  quoted.
- **Net return per contract** = payout - price - taker fee, the fee being Kalshi's
  `ceil(rate x C x P x (1-P))` on an order of C = 100 contracts with the series'
  own multiplier. Payout uses `settlement_value_dollars`, so scalar (partial)
  settlements are scored as they paid.

## Universe and sample

- Kalshi markets, parlays excluded, finalized, with `expected_expiration_time` in
  **[2026-06-30, 2026-09-16)** - as far back as the settled listing reaches, and ending
  eight days before now so that everything in it has settled.
- A deterministic 10% sample of **events**: `sha256(event_ticker) mod 10 == 0`. Every
  market of a sampled event is kept, so events are whole clusters. Chosen for request
  budget (the maker study's recorder shares Kalshi's read limit), before any data.
- Series whose fee type is not `quadratic` or `quadratic_with_maker_fees` are dropped
  and counted.

## Split

- **Exploration**: `expected_expiration_time` in [2026-06-30, 2026-08-16).
- **Holdout**: [2026-08-16, 2026-09-16). Later in time - the rule has to survive
  the future, not a shuffled past. Touched once, for one rule.

## Rule family and selection

60 candidate rules: purchase price in one of twenty 5-cent bins
[0.00, 0.05) ... [0.95, 1.00), at one of the three horizons. No category, series or
side split in selection - those are reported afterwards, descriptively.

On exploration, each rule's mean net return per contract gets an event-clustered
bootstrap interval (10,000 resamples) at the Bonferroni level for 60 one-sided
tests (1 - 0.05/60). The candidate is the rule with the highest lower bound.

**Amendment, 2026-09-24, before the analysis first ran:** a rule is eligible to be the
candidate only if its exploration observations come from at least 30 events. At the
Bonferroni level the lower bound is roughly the 8th-smallest of 10,000 resamples, and
with a handful of clusters that tail is a property of which few events happened to
land in the bin, not of the rule. Every rule is still reported.

**Coverage, found while collecting:** the settled listing reaches back only to
2026-07-17 - older markets have moved to Kalshi's historical archive - so exploration
is effectively [2026-07-17, 2026-08-16), about 30 days, against a 31-day holdout. The
window boundaries in the rule are unchanged; there is simply no listed data before
that date.

## Success, fixed now

A route is claimed only if **both**:

1. On exploration, the candidate's Bonferroni lower bound is above zero.
2. On holdout, the same rule's one-sided 95% event-clustered lower bound is above
   zero.

The holdout number for the candidate is computed and reported whether or not (1)
holds, so that a near miss is visible. Anything short of both is reported as no
calibration edge.

## What a pass would still not prove

Candles show a quote of at least one contract, not its size, so a pass is a
hypothesis about price, not capacity. The next step would be forward: the daily
maker-study recorder captures real L2 books with sizes on a frozen universe, and a
passing rule would be scored on those books, at their sizes, before anything else.
