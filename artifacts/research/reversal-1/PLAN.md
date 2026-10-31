# Exploration: does a sharp drop on Kalshi tend to rebound? (reversal-1)

**Status: EXPLORATORY. Not evidence.** Written on 2026-10-05 before any minute candle was
fetched. Whatever it finds is a lead for a pre-registered forward shadow test, never a result
on its own. No order is placed; only public, read-only market data is used.

It is separate from the LIP shadow study: it reads none of that study's recordings, and it
calls Kalshi only while neither study is recording (the fetcher refuses otherwise and stops
at 12:10 UTC).

*Operational change, 2026-10-05 14:40 UTC, before any candle was fetched:* the fetcher's
"stop at 12:10 UTC" became "never run 12:10-13:35 or 01:55-02:40 UTC" (around each study's
start), with the lock and process checks covering the rest of each recording. It lets the
fetch use a day on which a study did not run (LIP 2026-10-05 never launched). It changes
nothing in what is fetched or how it is analysed. Markets settled before Kalshi's historical cutoff
(2026-08-06) are served only by `/historical/markets/{ticker}/candlesticks`; the fetcher falls
back to it when the live endpoint has no candles, and requests at most 4,900 minutes at a time
(the endpoint returns at most 5,000 candles).

## Universe (fixed before fetching)

- The calibration study's sample (`artifacts/research/calibration-1/rows.jsonl`): Kalshi markets
  settled with closes from 2026-06-30 to 2026-09-16, a 10% sample by event hash (chosen by
  event, not by outcome), multivariate markets excluded.
- Kept: volume >= 1,000 contracts. To stop a few huge series (BTC hourly, MLB props) from being
  the whole sample: at most 20 markets per series, the 20 with the lowest sha256(ticker).
- Category from the series metadata already fetched (`fees.json`).
- Candles: 1-minute, from max(open_time, close_time - 7 days) to close_time. Minutes without a
  candle carry the last bid and ask forward.

## The event (all cells reported, none chosen afterwards)

- mid = (yes_bid + yes_ask) / 2, only where both exist and 0 < bid < ask < 1; spread = ask - bid.
- A **drop** at minute m: mid(m) - max(mid over the W minutes before m) <= -D, and spread(m) <= 5c.
- Grid: D in {5c, 10c, 15c} x W in {5, 15, 60} minutes - 9 cells.
- In one market, an event starts a 60-minute cooldown (later drops in it are not new events).
- **Control:** the mirror - a **rise** of at least D within W (the trade there is to buy NO).
- **Baseline:** every minute meeting the same spread filter, sampled 1 in 60 per market - the
  plain cost of a round trip, with no event at all.

## The trade (a taker, costs included; nothing uses information after its own minute)

- Entry: buy YES at the ask of minute m+1 (for a rise: buy NO at 1 - bid of minute m+1).
- Exit after H in {15, 60, 360} minutes: sell at the bid of minute m+1+H (NO: 1 - ask). If the
  market closes first, it is held to settlement and paid the settlement value.
- Fees: Kalshi taker fee on each leg, ceil(0.07 x p x (1 - p) x 100) / 100 dollars per contract
  at the traded price (the series' fee multiplier applied; series with no taker fee are noted).
- Also reported without costs: the change of mid from minute m+1 to m+1+H - the size of the
  rebound itself, to separate the signal from what it costs to trade it.
- Also: hold to settlement (payout - entry price - entry fee).

## Statistics

- Per cell: number of events, markets and event groups; mean net P&L per contract and the
  mean mid change, each with a 95% interval from a cluster bootstrap over event tickers (2,000
  draws, seed 20261005) - drops in one game or one release are not independent.
- Splits, for every cell: category; time to close (more than 6 h, 6 h or less); price level
  before the drop (mid < 20c, 20-80c, > 80c).
- 9 cells x 3 horizons x 2 directions are many comparisons: anything that looks positive
  here is a hypothesis to pre-register and test forward, not a finding.

## Implementation details (fixed 2026-10-05, before any result was computed)

Choices the plan above leaves open, written down before `analyze.py` was first run:

- A candle with `end_period_ts` t is the quote at the end of the minute that ends at t. The
  minute grid runs from the first whole minute after the window start to the last whole minute
  before close_time; a minute without a candle has the last candle's bid and ask (none before the
  first candle).
- "The W minutes before m" are minutes m-W .. m-1; only valid minutes (0 < bid < ask < 1) enter
  the max (drop) or min (rise). Comparisons use a 1e-9 tolerance, since mids are half cents.
- The cooldown is per market and per cell (D, W, direction): an event at m blocks m+1 .. m+59.
- An event is traded only if minute m+1 exists and has a valid quote; otherwise it is dropped and
  counted.
- Exit when the bid is 0 (no bid) sells YES at 0; NO when the ask is 1 sells NO at 0. They count.
- Mid change: mid(m+1+H) - mid(m+1) (sign flipped for NO); if m+1+H is past the last minute, the
  settlement value replaces the exit mid; if the exit minute has no valid quote, the event is left
  out of the mid-change mean only.
- "Hold to settlement" is reported as a fourth horizon, `settle`, next to 15/60/360.
- Fee for one contract: ceil(round(0.07 x multiplier x p x (1-p) x 100, 9)) / 100, on the entry and
  on a market exit; none at settlement. Per-contract rounding is the cost of a one-lot taker.
- Price level before the event = the window's reference mid (the max over the W minutes for a
  drop, the min for a rise). Time to close = close_time - minute m.
- Baseline: in each market, every 60th minute that passes the spread filter (the 1st, 61st, ...),
  traded both ways (buy YES and buy NO), same horizons, same costs.
- Bootstrap: resample event groups (event_ticker) with replacement, 2,000 draws, a generator seeded
  20261005 for every row; the mean is pooled over events (sum / count) in each draw; the 95%
  interval is the 2.5th and 97.5th percentiles.
- Output: `results.csv` (every row: overall and every split, every cell, horizon, direction, and
  the baselines) and `REPORT.md`. The report lists every row whose net interval is above zero next
  to the number of rows tested, so the count can be read against chance (about 2.5% of rows
  would be above zero by chance alone if nothing were there).

## Changes after the audit (2026-10-06, decided after seeing the first partial run)

A partial run on 7,468 of the 13,110 markets (`REPORT.partial-v1.md`, code `analyze.v1.py`) was
audited by three independent reviewers and one skeptic (read-only, no network). These changes
were made **after** seeing those partial numbers, so they are post hoc and marked as such:

1. **Fixed - the exit mid (high).** The rule above ("if the exit minute has no valid quote, the
   event is left out of the mid-change mean only") left out exactly the exits where the price had
   run to 0 or 1 (no bid, or no ask): 3.5-12% of exits per row, and which ones depends on the
   outcome. Every "mid change above zero" overall row of the partial run disappeared once they
   were put back (and two drop rows that had not been above zero became so: the fix removed the
   rise rows' apparent reversal and strengthened the drop rows at 15 minutes - it did not only
   remove reversal). Now the exit mid also accepts a one-sided book at the boundary (0 <= bid < ask
   <= 1, not both 0 and 1), e.g. 0.00/0.01 gives 0.005. The entry and the events still need a
   strict quote (0 < bid < ask < 1). Net P&L never left these exits out and is unchanged.
2. **Sensitivity added - a tight reference.** The plan's drop compares the mid with the window's
   highest mid whatever its spread; 41-66% of events had their reference at a minute wider than
   5c, i.e. a wide quote collapsing rather than a price move. Reported next to the plan's rows,
   overall only: the reference minutes must pass the 5c spread filter too.
3. **Sensitivity added - a 100-lot fee.** The plan's fee is rounded up to the cent on one
   contract (about 1c of the 5.6c baseline round trip). Every row also shows the net with the fee
   rounded on 100 contracts.
4. **Report.** Rows from fewer than 10 event groups are not listed as leads (with 2 groups the
   bootstrap interval is just the two group means). The "by chance about 2.5% of rows" sentence was
   removed: rows overlap and the null is not zero-mean, so that count tests nothing. Mid-change
   rows below zero (the move continued) are listed too.
5. **Small fixes.** A reference mid of exactly 20c fell into "<20c" by float error (under 0.1% of
   events). The market count now counts a market only if it has a valid quote at some minute.
   `load()` also stops cleanly on a gzip member cut after its first byte.

Left as specified, with the audit's measured effect: the cooldown lets a still-running drop fire
again at m+60 (9% of events; keeping onsets only moves overall net by up to -0.4c and changes no
conclusion); exiting with no bid sells at 0 (a taker cannot do better); the minute grid starts at
the minute ending at or after the window start (the text above says "after"; no event changes).

## Final check (2026-10-06, after the complete run)

Two independent checkers (read-only, no network) reviewed the post-audit code and tried to refute
the summary claims. The code changes 1-5 do what is written above, and nothing else: over all
13,110 markets the plan's events and 1-lot nets are identical to `analyze.v1.py`, and mid changes
are identical wherever v1 had one. Fixed after the check (report and safety only, no number moved):

- the leads sentence counted its rows against all 1,224 rows; it now uses the 1,076 rows with at
  least 10 event groups;
- `load()`'s broad catch could hide a truncated read: it now refuses to run when fewer records load
  than `fetched.txt` lists (`--partial` while a fetch is running).

Noted, not changed: 9.2% of the boundary exits are one-sided books wider than 5c (e.g. 0.25/1.00
gives a mid of 0.625), so "the price ran to 0 or 1" describes most but not all of change 1.
The checkers' own extra calculations (excess over the baseline, the momentum trade, drops split
by a wide or tight reference) are in `checks/`, labelled there; `SUMMARY.md` gives the corrected
reading.
