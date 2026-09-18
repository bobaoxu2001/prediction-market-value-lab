# Deterministic arbitrage: what is proven, what is not

**FINAL VERDICT: `NET_ARBITRAGE_OBSERVED`** — two rungs below executable, five below
stable profit.

The mission was falsification-first. The strategy did not survive it intact: every
positive result previously reported was an artifact of combining quotes that never
coexisted. What survives is the mathematics, not the money.

## What is proven

* **Settlement floors are correct.** Implication containment (including the asymmetric
  `>=` vs `>` boundary at equal strikes) and grid-based partition proofs both hold, and
  refuse the cases designed to fool them.
* **Fail-closed on incompleteness.** A 5-of-168-rung basket priced at $0.82 against a
  $1 floor was refused. Exhaustiveness is proven on an inferred observation grid and the
  grid is reported as a stated assumption.
* **Replay determinism.** 725 of 725 certificates recompute identically from stored
  inputs with no network and no database.
* **Marginal sizing.** Sizing on the marginal set rather than the average; worth a $94
  swing on one real structure.

## What is NOT proven

* **That any of it is executable.** Zero structures pass every execution gate. The
  blocking gate is simultaneity.
* **That execution is safe.** All figures assume atomic fills. Kalshi two-leg orders
  are not atomic and the sequential worst case is not modelled.
* **That opportunities recur.** 0 independent event-days under frozen rules, against a
  pre-registered minimum of 20.

## The economic barrier, measured

Across 324 proven two-leg structures with a two-sided top of book:

| quantity | value |
|---|---|
| gross dislocation, median | **−34 c** per set (the pair costs $1.34) |
| gross dislocation, p90 | −2 c |
| gross dislocation, p99 | **+1 c** |
| gross dislocation, max | +6 c |
| all-in taker fee at executable size | **0.63 c – 1.31 c** per set, median 1.21 c |
| structures with any accretive size at all | **4 of 324** |
| structures with positive net edge | **3**, all with 454–10,762 s skew |
| structures surviving simultaneity | **0** |

**The barrier:** the fee floor is roughly 1.2 c per set and the 99th-percentile
dislocation is about 1 c. The market's best routine mispricing is smaller than the cost
of taking it, and the median structure is not close — it is 34 cents out of the money.
The handful that do clear are not simultaneous.

## Capacity

Not a capacity problem — an existence problem. The best live basket
(`KXHIGHMIA-26SEP18`, all-NO, a proven 6-rung partition) had **3 sets** of depth: $15
of notional carrying 4 c of gross edge against 18 c of fee across six legs. Deployable
capital at any tested level is bounded by opportunities that do not exist rather than by
size.

## Execution assumptions — and the second falsification

`TAKER_SEQUENTIAL_WORST_CASE` is now implemented, and it kills the same three
structures independently of the simultaneity problem. Kalshi fills one order at a time,
so a floor computed as though both legs land together describes a trade the venue does
not offer.

| structure | atomic floor | sequential worst case | if the second leg vanishes |
|---|---|---|---|
| KXSOLD-26SEP1817 | **+$33.45** | **−$52.80** | −$387.82 |
| KXBTCD-26SEP1710 (1,378) | +$3.74 | **−$19.26** | −$35.58 |
| KXBTCD-26SEP1710 (2,302) | +$26.18 | **−$8.11** | −$56.97 |

All three are `THEORETICAL_ONLY`. **The claim therefore fails twice over, for unrelated
reasons: the legs never coexisted, and the structure would lose money even if they
had.** Either is sufficient on its own.

The rescue column is the one that decides whether a thin edge is worth attempting at
all: on KXSOLD, winning was worth $33 and failing costs $388. An 11:1 adverse ratio
means the structure needs a fill probability above ~92% just to break even on attempts,
before any edge.

`TAKER_SEQUENTIAL_OBSERVED` and `MAKER_EXPERIMENTAL` remain unimplemented. No result
assumes a maker fill.

## Known failure modes

1. **Cross-snapshot mixing** — found, fixed, gated, regression-tested. Was producing
   $33 of fictional profit.
1b. **Atomic-fill assumption** — found, modelled, gated. Turns the same $33 into −$53.
2. **Size-1 fee measurement** — found in my own analysis before it reached a conclusion;
   overstated the barrier by ~60%.
3. **Detection multiplicity** — an N-rung ladder yields O(N²) structures over N books.
   They compete for the same resting size and are deduplicated by economic exposure id.
4. **No historical L2** — `HISTORICAL_EXECUTION_PROOF_UNAVAILABLE`. Forward-collected
   snapshots only; no backtest is possible and none is claimed.

## Reproducibility

```bash
.venv/bin/pytest -rs
.venv/bin/pmvl pair implication-scan --max-age-hours 24 --max-quote-age-seconds 86400 \
    --max-snapshot-skew-seconds 1 --out impl.json
.venv/bin/pmvl pair basket-scan --max-age-hours 24 --max-quote-age-seconds 86400 \
    --max-snapshot-skew-seconds 1 --out basket.json
.venv/bin/pmvl pair implication-replay impl.json
.venv/bin/pmvl proof stable-profit --scan impl.json --scan basket.json
```

## Minimum remaining obstacle to the next verdict

To reach `EXECUTABLE_ARBITRAGE_OBSERVED` a structure must clear **three** gates
simultaneously, and no observed structure has cleared even two:

1. positive net floor after fees — 3 of 458 certificates
2. all legs quoted within 1 second — **0 of those 3**
3. positive floor under sequential execution — **0 of those 3**

Gate 3 is the harder one and it does not depend on better data collection. It requires a
dislocation large enough to survive one adverse fill of the structure's own size, which
on the measured books means roughly **3–4 c of gross edge** rather than the ~1.3 c that
merely beats the fee. The 99th percentile of observed dislocation is +1 c and the
maximum across 324 structures is +6 c.

The cheapest next experiment is a live poller capturing both legs of the few ladders
with two-sided depth in a single batch, which would at least produce simultaneous
quotes and let gate 2 be tested honestly. Gate 3 would remain.
