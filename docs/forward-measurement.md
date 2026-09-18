# Forward measurement: what was built, and what it is allowed to conclude

The previous phase ended at `NET_ARBITRAGE_OBSERVED` with every positive result
withdrawn. Two independent falsifications did it: the three profitable structures had
legs quoted 454s, 455s and 10,762s apart, and all three were `THEORETICAL_ONLY` once
the fills were put in sequence. This phase exists to remove the first failure mode
structurally rather than by inspection, and then to measure the two things that could
still be true - that a maker has positive unconditional expectancy, and that a large
dislocation appears often enough to be worth hunting.

## 1. The root cause of the leg skew was a query-string encoding

Kalshi's batch orderbook endpoint takes a **repeated** `tickers` parameter. The
existing client coerced every parameter set to a dict, so it could only send the
tickers comma-joined - and the venue answers that with HTTP 200 and a *single empty
book* whose ticker is the joined string:

```
GET /markets/orderbooks?tickers=A,B,C   ->  200 {"orderbooks":[{"ticker":"A,B,C","orderbook_fp":{...empty...}}]}
GET /markets/orderbooks?tickers=A&tickers=B&tickers=C -> 200 three real books, one response
```

The old code correctly refused to trust the first response - an entry whose ticker was
not requested invalidates the chunk - and fell back to one request per market. That
fallback was the right call for coverage and the wrong one for evidence: it is where
every multi-leg quote in this repository came from, and nothing downstream recorded
how far apart the requests were.

`HttpClient.get_json` now accepts a sequence of pairs, and
`KalshiProvider.get_orderbooks_synchronized` uses it with **no fallback**. It returns
`ok=False` instead, because a caller who wants simultaneity must be told it was not
achieved rather than handed a slower substitute. The venue accepts 100 tickers per
request and rejects 188, so an event wider than 100 legs has no synchronous read at
all and is refused rather than truncated.

Measured effect: leg skew bound went from **10,762 seconds** to **300ms at the median
and 522ms at the worst**, and it is now an upper bound on a single request's round
trip rather than an unmeasured gap between two of them.

## 2. What a snapshot is

One event, every leg, one response. `capture_mode` is `SINGLE_REQUEST` or the snapshot
may not support a multi-leg claim. `skew_bound_ms` is the round trip - the venue does
not stamp its books, so the tightest honest statement is "every leg was read somewhere
inside this window", never zero.

Refusals are written to the same log as captures. An event too wide to fetch at once,
a failed request, a response containing an unrequested ticker: each is a line in
`snapshots.jsonl` with its reason. A file of only the successful captures would hide
exactly the slow and wide events where a dislocation is most likely to persist.

The log is a hash chain - each line carries the digest of the one before it. This is
not security; anyone who can write the file can rewrite the chain. It makes "the
losing observations are missing" a loud failure rather than a silent one, which is a
failure this project has already had.

Market **rules** are captured at the start of the run, alongside the books. Replaying a
recording must never re-fetch the ladder: by then the venue may have relisted,
corrected or expired rungs, and the replay would be against different markets wearing
the same tickers.

Two version numbers, deliberately separate. `STRATEGY_VERSION` is what the recorder
wrote and bumps when a recorded line changes shape or meaning. `ANALYSIS_VERSION` is
what the analysis did with those lines. A recording is evidence and does not change;
the rules for reading it do, and re-reading old evidence under a corrected rule is the
entire point of recording offline.

## 3. The maker experiment

Three fill estimators that bracket the truth, because a print at your price is not
your fill:

- **`TRADED_THROUGH`** - volume printed strictly past the quote. Proves the level was
  cleared, therefore proves the fill. A lower bound.
- **`QUEUE_MODEL`** - at-level volume minus the size already resting at that price when
  the quote was placed. Assumes the quote joined the back of the queue. Cancellations
  ahead would move it up, so this understates fills.
- **`TOUCHED`** - any print at the price is a full fill. **Inadmissible.** Computed only
  so the gap between it and the others is visible.

A print can only fill a quote if the aggressor took the *other* side, and Kalshi stamps
every print with a YES price whichever side the taker took, so a NO quote at 0.62 is
matched against prints at 0.38. Prints inside the placement latency window happened
while the order was still in flight and do not count.

Then three separate numbers, none of which may stand in for the others:

- **markout** at 30/60/300s from the *recorded book after the fill*, never from the fill
  price. A negative markout means the spread was not income, it was payment for being
  wrong shortly afterwards.
- **hedge cost** - the taker cost of the opposite side of the same market, which closes
  the position for exactly $1 of settlement value. The cheapest hedge available; a
  strategy that fails against it fails against any.
- **unconditional expectancy** - total PnL divided by *quotes placed*. Unfilled quotes
  contribute exactly zero. Dividing by fills is the arithmetic that makes every market
  making strategy look profitable.

Quotes never overlap on a market: the next one is placed only after the previous has
expired. Overlapping quotes share the trades that fill them and the move that follows;
pooling them shrinks the interval around a number that has not become better known.
The confidence interval resamples **whole market-days**, not individual quotes.

Four policies are declared in the source - join or improve, on each side - and the
sweep runs and reports all four. The best of four looks good at N=4 too.

What the experiment cannot see is written into its own output: the quote was never
sent, so nothing here captures how the book would have reacted to it.

## 4. The dislocation harvester

Two structures, and the difference between them is the point.

**The complete set inside one market** is the control. Buying YES and NO in the same
market pays exactly $1 whatever happens, and both legs come from the same book in the
same response, so leg skew is not small - it is structurally absent. But on Kalshi the
asks are derived from the bids, so a pair costing under a dollar is the same statement
as `yes_bid + no_bid > $1`: a crossed book, which the matching engine removes the
instant it appears. A positive reading here would be surprising and its absence is
close to a tautology. It is measured because if a dislocation cannot be found where
simultaneity is free, a wider structure is not where the money is.

**The implication pair across two ladder rungs** is the real target. Two rungs are two
markets and two independent books, and nothing in the exchange enforces that "above
$4,200" stays cheaper than "above $4,100". A reading of zero here is a finding about
the market rather than about the matching engine.

Every observation is kept, winners and losers. Near misses - cleared the bar one cent
lower but not this one - are counted separately, because "never came close" and "one
tick away, repeatedly" are different zeros. Refused candidate pairs are counted by
reason. Structures that could not be priced because a leg was unquoted are counted
outside the denominator but counted.

### The bug this phase found in its own first result

The first implication harvest reported that 12.45% of observations cleared 5c, with a
maximum gross edge of 78 cents and a maximum net-after-fees of $503. That was wrong,
and the top of the ledger said why: the pair was
`KXRAIN-26SEP19-DAL | KXRAIN-26SEP19-MKE`. Rain in Dallas and rain in Milwaukee.

All 27 "will it rain in `<city>`" markets carry `strike_type: greater` and
`floor_strike: 0`. The threshold predicate `[X > 0] implies [X > 0]` is perfectly true,
and it held for all 351 pairs of cities. The arithmetic was never wrong; the strike
field simply was not what distinguished the two contracts, and the harvester had been
calling the bare predicate instead of the full proof.

Two guards now sit in `prove_implication`, so they protect every route that uses it:

1. **The strike must discriminate.** Identical comparator and identical strike makes the
   claim mutual rather than ordered. A ladder rung implies the one below because its
   strike is higher; equal strikes mean the strike is not what separates the contracts
   and this route cannot see what does.
2. **The observable must match.** With every number stripped out of the two titles they
   must read identically. "Will the temp in Miami be above 71.99" and "...above 72.99"
   collapse to the same string; "will it rain in Dallas" and "will it rain in
   Milwaukee" stay different.

`ANALYSIS_VERSION` was bumped to 1.1.0 and the 1.0.0 implication numbers are withdrawn.

## 5. Kalshi has no atomic multi-leg route

Audited separately in [kalshi-atomic-execution-audit.md](kalshi-atomic-execution-audit.md).
Single-leg orders only; the batch endpoint reports partial failure by design; FIX is
single-order; order groups are a rate limiter; the multivariate collection is a bundle
that cannot absorb or emit leg positions. Netting and collateral return reduce capital,
not the risk in the gap between two fills.

So the sequencing loss term is a property of the venue, not a modelling assumption that
a better executor could remove.

## 6. What this phase is allowed to conclude

Nothing about a strategy until the forward window closes. The recorder, the experiment
and the harvester are instruments; a number is a result only when it comes out of a
recording that ran to its declared length, under one analysis version, with the
refusals and the losers still in the file.

Results are in [forward-measurement-results.md](forward-measurement-results.md).
