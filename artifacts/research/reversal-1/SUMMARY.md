# reversal-1 - summary (2026-10-06)

**EXPLORATORY. Not evidence.** Complete sample: 13,110 Kalshi markets (12,885 with a valid quote),
1-minute public candles, closes 2026-06-30 to 2026-09-16. A taker; no order was placed. The plan
(PLAN.md) was fixed before the data; the changes made after a partial run are listed there and
marked post hoc. Full tables: REPORT.md and results.csv. The checkers' extra calculations, which
are not part of the plan, are in `checks/` (cents per contract; joint cluster bootstrap over event
groups, 2,000 draws, seed 20261005).

## Answer

**No: buying YES after a sharp drop does not pay on Kalshi, at any horizon, size or split we
measured.** No overall row and no split row with at least 10 event groups has a net interval above
zero, with 1-lot or 100-lot fees (0 of 1,076 rows). The best overall row is drop 5c / 60 min held
to settlement: -1.88c [-3.44, -0.34] (1-lot), -1.42c [-2.98, +0.11] (100-lot) - no better than
buying YES at a random minute and holding (-1.31c, 1-lot).

## What the prices do (before costs, beyond the baseline's own drift; `checks/excess_mid.csv`)

- **After a drop, a small bounce of about 0.3-0.6c within 15-60 minutes** (7 of 9 cells above zero
  at 15 min, 6 of 9 at 60). By 6 hours it is mostly gone (2 of 9), and at settlement none of the 9
  cells differs from the baseline. The raw "+1 to +2c to settlement" in REPORT.md is almost all the
  baseline's own drift (+1.49c for buying YES at any minute).
- **At 15 minutes, most of the bounce is a wide quote collapsing**, not a real price move: drops
  whose reference minute was a wide quote (56-73% of events) bounce +0.5 to +0.8c, the others -0.3
  to +0.3c (`checks/refwide.csv`). At 60 minutes, drops between tight quotes bounce about as much as
  the others (about +0.45 to +0.74c). The "tight reference" rows in REPORT.md look weaker mainly
  because they contain different events (the cooldown falls elsewhere), some of which keep falling.
- **After a rise, the price keeps rising** (momentum, not reversal): 0.4-1.2c beyond the baseline at
  60 minutes, 9 of 9 cells, and 0.8-1.8c with the tight reference. By 6 hours only 2-3 of 9 cells
  still differ from the baseline.

## Why none of it trades

- A taker round trip at a random minute costs about 5.6-5.8c per contract with 1-lot fees and
  4.7-4.8c with 100-lot fees (half-spreads about 1.2c + 1.25c, fees about 3.2c).
- Around these events it costs 7.7-10.3c at 15-60 minutes. All of the extra cost is spread, and
  the spread stays wide for hours (6-8c at the 60-minute exit, against 2.7c for the baseline):
  these moves happen in thin, wide-spread periods.
- Holding to settlement avoids the exit leg (cost about 3.9-5.2c), and there the raw effects are
  about half the cost. But beyond the baseline's drift they are about zero.
- The momentum trade (buy YES after a rise, buy NO after a drop) loses too: all 144 rows are
  negative, none has an interval above zero, and the best is -1.43c [-3.14, +0.23] (1-lot) /
  -0.98c (100-lot) held to settlement (`checks/momentum.csv`).

## Caveats

- Many comparisons (9 cells x 4 horizons x 2 directions x splits, plus sensitivities); every number
  above is a lead at most.
- A taker who trades minutes after the move. A maker who is already quoting when the drop happens
  is a different strategy (fills, adverse selection), already measured by the maker study.
- Drops held to settlement, with more than 6 hours to close, have slightly positive means (+0.1 to
  +0.2c 1-lot, +0.6 to +0.7c 100-lot), with intervals that include zero. It is one split among
  many, and it is not proposed as a lead.

## Recommendation

No forward shadow test: the largest effect is a fraction of the cost of trading it, and nothing
here is a lead worth pre-registering.
