# Calibration: no price range on Kalshi pays a taker

Pre-registered in `calibration-preregistration.md` (commit `1757b51`, amended before the
analysis ran in `35dc686`); exploration committed in `4e4e02c` before the holdout was
touched. Inputs and scripts in `artifacts/research/calibration-1/`.

## Verdict

**NO CALIBRATION EDGE.** Neither criterion met.

| | rule | observations | events | mean net / contract | lower bound |
|---|---|---|---|---|---|
| exploration (Jul 17 - Aug 16) | best of 60: buy under 5c, 1 h before expected expiration | 5,111 | 1,124 | **-1.36c** | -1.54c (Bonferroni, 60 tests) |
| holdout (Aug 16 - Sep 16) | the same rule, once | 5,793 | 1,504 | **-1.07c** | -1.20c (one-sided 95%) |

All sixty rules - twenty 5-cent price bins at 1, 6 and 24 hours before the scheduled
expiration - have a negative Bonferroni lower bound on exploration, and 59 of 60 have a
negative mean. The one positive mean (+1.07c, [0.35, 0.40) at 24 h) has a lower bound of
-10c. The candidate replicated on the holdout almost exactly, which says the loss is a
stable property of the market, not noise in one month.

## Data

79,633 settled markets from a deterministic 10% sample of events (parlays excluded,
volume >= 100), 1,178 series, all on Kalshi's quadratic fee schedule. 96,122 observations
survived the filters observable at the time (market open, open interest >= 100, a quote on
the side bought).

Two things were settled before the design was trusted:

- **The observation time is fixed by the schedule, not the outcome.** Settled data reports
  the *actual* close, and anchoring on it leaks the result (a "will X happen" market closes
  early because X happened; a game's length depends on who is winning).
  `expected_expiration_time` is schedule-derived - on MLB it is the listed first pitch plus
  exactly three hours - so `t_obs` is placed from it.
- **Candles stand in for the book.** Checked against order books the maker study recorded:
  the carried-forward hourly candle ask equals the recorded best ask 98.5% of the time and
  is within a cent 99.8%, with no cheap bias (`validate_candles.py --hourly`). The
  apparent mismatches in the first pass were notation: candles write an empty side as
  1.00 / 0.00.

The settled listing reaches back only to 2026-07-17 (older markets are in Kalshi's
historical archive), so exploration is effectively thirty days.

## Where a taker's money goes (descriptive, after the test)

`decompose.py` splits the taker's return into the market's calibration, the half-spread
and the fee, pooled over both periods:

| horizon | payout - mid | half-spread | fee |
|---|---|---|---|
| 1 h (often in play) | -0.85c | 6.16c | 0.87c |
| 6 h | +0.67c | 1.36c | 0.97c |
| 24 h | +0.90c | 2.67c | 1.03c |

- **The mid-price is calibrated to within about a cent** at every horizon. There is a mild
  favorite-longshot tilt in play (at 1 h, mids of 0.2-0.5 are 3-4c rich and 0.6-0.8 are
  2-3c cheap), always smaller than the cost of crossing.
- **A taker's loss is the spread plus the fee.** In play the half-spread near 50/50 is
  12.7c; six hours out it is about 1.4c.
- The best-looking cell (YES at 0.8-0.9 mid, 6 h: about +1c net) is the best of thirty
  examined after the test, at roughly 2.5 standard errors. That is what the maximum of
  thirty noisy cells looks like. It is not a finding.

## What it implies

The spread a taker pays goes to whoever quoted. That makes the one unmeasured maker
question - does a resting quote keep the spread when its fill is **held to settlement**,
or give it back to the traders who chose to hit it - worth asking. It was added to the
multi-day maker study as a pre-registered secondary metric before any number existed
(`maker-multiday-preregistration.md`, amendment of 2026-09-24), with a family-wise bound
for eight tests, decided at day 20 with the primary.
