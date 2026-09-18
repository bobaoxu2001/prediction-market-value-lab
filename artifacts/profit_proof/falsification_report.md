# Falsification report

Adversarial audit of the deterministic-arbitrage claim. The brief was to try to break
it, not to confirm it. It broke.

## BUG-1 — every positive result was built from quotes that never coexisted

**BUG.** Multi-leg structures were priced from books fetched independently per market
via `latest_orderbook(session, market_id)`. Nothing required the legs to come from the
same moment, and nothing measured how far apart they were.

**IMPACT.** All three positive-net-floor structures in the stored-book scan were
non-simultaneous:

| structure | reported net floor | leg-to-leg skew |
|---|---|---|
| KXSOLD-26SEP1817 | **+$33.48 "guaranteed"** | **10,762 s = 2.99 hours** |
| KXBTCD-26SEP1710 | +$26.29 | 454 s |
| KXBTCD-26SEP1710 | +$3.74 | 455 s |

The headline result — a $33.48 guaranteed profit on 4,700 sets — compared a Solana
quote from 02:10 against one from 05:10. It was never purchasable. Across all 725
certificates only 223 had legs within one second of each other.

**WHY PREVIOUS TESTS MISSED IT.** Every fixture builds its books with `utcnow()` inside
one test function, so fixture skew is microseconds. The suite could not have caught
this: it only ever tested the simultaneous case, and the field that would have revealed
the asynchronous one did not exist.

**FIX.** `snapshot_skew_seconds` is computed on every certificate from the books' own
`observed_at`, gated on acceptance at a default of 1.0 s, reported on rejection, and
exposed as `--max-snapshot-skew-seconds`.

**REGRESSION TEST.** `test_non_simultaneous_legs_are_refused` rebuilds the exact KXSOLD
skew (10,762 s), asserts the arithmetic still shows a positive net floor, and asserts
the structure is refused anyway.

**HISTORICAL RESULTS INVALIDATED: YES.** Every previously reported positive net floor
is withdrawn. Re-run under the gate, the stored-book scan accepts zero structures.

### Sensitivity — profitability exists only in the stale window

Quote-age relaxed to 24 h so staleness is not the binding gate; only the simultaneity
limit varies:

| max leg-to-leg skew | accepted structures |
|---|---|
| 1 s | **0** |
| 100 s | **0** |
| 250 s | **0** |
| 500 s | 2 |
| 1000 s | 2 |
| unlimited | 2 |

Profit appears only once legs quoted 250–500 s apart may be combined. This is
`STALE_COMBINATION_RISK`, and it disqualifies the result as evidence of anything
tradeable.

## FINDING-2 — none of them survives non-atomic execution either

Independent of simultaneity. Kalshi fills one order at a time, so a two-leg floor
computed as though both land together is an upper bound on a trade the venue does not
offer. Modelled worst case: leg one fills, the structure's own size is lifted off the
remaining leg by whoever else saw the same dislocation, and the rest is completed
against what is left — or unwound into the bid if it cannot be.

| structure | atomic floor | sequential worst case | if the second leg vanishes |
|---|---|---|---|
| KXSOLD-26SEP1817 | **+$33.45** | **−$52.80** | −$387.82 |
| KXBTCD-26SEP1710 (1,378) | +$3.74 | **−$19.26** | −$35.58 |
| KXBTCD-26SEP1710 (2,302) | +$26.18 | **−$8.11** | −$56.97 |

All three are `THEORETICAL_ONLY`. So the claim fails twice over and for unrelated
reasons: the legs never coexisted, **and** the structure would lose money even if they
had. Either alone is sufficient.

A modelling error was caught here by its own test. The first version consumed whole
price *levels*, so a single resting 5,000 lots counted as entirely gone while a thin
two-level book survived — punishing exactly the depth that makes sequencing safe. The
adverse move is a quantity, not a level.

## BUG-2 — the fee was measured at a size nobody trades

**BUG.** The first profitability-frontier measurement computed the all-in fee at
quantity 1, where Kalshi's ceil-to-the-cent rounding puts a floor of 1c on each leg
regardless of the true rate.

**IMPACT.** It reported a 2c fee floor per set and therefore overstated the economic
barrier by roughly 60%. This was my own measurement, caught before it reached a
conclusion — but it would have made the market look further from profitable than it is.

**FIX.** Fees are computed at the accretive executable size, where the cent ceiling
amortises. Measured floor: **0.63c – 1.31c per set**, median 1.21c.

**REGRESSION TEST.** Existing fee tests cover the rounding; the frontier script now
sizes first and prices second. No production code was wrong.

## BUG-3 — I destroyed a pre-existing test file

**BUG.** `tests/test_execution_model.py` already existed, covering `publish=false`
stateless recomputation. I created my sequential-execution tests at that path with
`cat >` without checking, silently replacing 282 lines and 13 tests with 123 lines and
8.

**IMPACT.** 13 tests stopped running. Nothing failed, so nothing announced it: the
suite went from 2,641 passing to 2,636 passing and read as a normal fluctuation. Had I
reported that number without reconciling it, the audit would have quietly reduced
coverage while claiming to increase it.

**WHY PREVIOUS TESTS MISSED IT.** No test can catch this. A deleted test does not fail;
it disappears. The only signal was the total count, which I nearly accepted because it
was close to the expected value and still very large.

**FIX.** The original file is restored from `5d300da`; my tests moved to
`tests/test_sequential_execution_model.py`. Collected count reconciles exactly:
2,642 at the pre-audit commit, minus 13 clobbered, plus 8 added = 2,637 observed;
restored to 2,650.

**REGRESSION TEST.** None possible for this class of error. The discipline is to
reconcile the collected-test count against the expected delta on every run rather than
to eyeball the pass count — which is now how the totals in this report are stated.

**HISTORICAL RESULTS INVALIDATED: NO.** The clobbered file tested an unrelated
publication path; no arbitrage finding depended on it. The suite figures quoted before
this fix were nonetheless understated coverage and are corrected here.

## What survived the audit

* The settlement mathematics. Payoff floors, implication containment including the
  asymmetric `>=` vs `>` boundary, and grid-based partition proofs all hold.
* Marginal sizing. Independently re-derived; average-cost sizing was already fixed and
  the fix is load-bearing (a $94 swing on one real structure).
* Fail-closed behaviour on incomplete partitions. `KXBTC-26SEP1709` offered 5 rungs of
  a 168-rung event at $0.82 against a $1 floor that does not hold, and was refused.
* Replay determinism. 725 of 725 certificates recompute identically offline.

## Negative controls

13 controls, each breaking exactly one condition, all refusing as required: reversed
implication, mismatched settlement source, non-simultaneous legs, 10x fees, thin depth,
empty book side, equally-stale books, and five ladder-promotion controls asserting that
an unmeasured gate reads NOT_ESTABLISHED rather than PASS.

## Not yet tested — and therefore not claimed

* **Sequential execution.** All figures assume `ATOMIC_THEORETICAL` fills. Two-leg
  Kalshi orders are not atomic; a worst-case sequential model is not implemented, so no
  figure here is an executable-profit claim.
* **Maker fills.** Not modelled. No result assumes one.
* **Historical L2.** The store holds snapshots, not a replayable historical order book,
  so `HISTORICAL_EXECUTION_PROOF_UNAVAILABLE`. Forward-collected snapshots are the only
  admissible evidence.
* **Forward sample.** 0 independent event-days under the frozen rules against a
  pre-registered minimum of 20.
