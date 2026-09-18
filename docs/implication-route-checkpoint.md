# Same-venue structure routes — checkpoint

Machine-readable state for resuming. Updated 2026-09-18.

## engineering_status: `REAL_DATA_VALIDATED`

Every acceptance condition below was met by a run recorded in this file.

| condition | evidence |
|---|---|
| algorithm wired to a real entry point | `pmvl pair implication-scan` (and `--live`), in `services/worker/src/pmvl_worker/crossvenue_cli.py` |
| uses real contract rules and market identity | Kalshi `strike_type` + `floor_strike`/`cap_strike`, `settlement_source`, `close_time`, rule text excerpt carried in every certificate |
| proves the payoff relation | `prove_implication` — exact set containment, per-field verdicts; `evaluate_payoff` enumerates the joint settlement space and prunes only the state the implication forbids |
| real order books, real direction, depth and fees | `executable_quote` VWAP on the ask ladders actually lifted; Kalshi quadratic fee per leg; Decimal throughout |
| explainable accept/reject | `StructureCertificate.rejection_reason` lists every failing condition separately |
| replayable offline | `pmvl pair implication-replay` — **725/725 reproduced identically**, no network, no database |
| positive, negative, boundary and safety tests pass | `tests/test_implication_structures.py`, 20 tests |
| at least one real structure through the whole chain | 1,588 live-quoted structures and 725 stored-book structures, all certified |

## opportunity_status: `NOT_OBSERVED_WITHIN_REPORTED_COVERAGE`

No structure passed every gate on fresh quotes.

* **Live run** (3 ladders, 89 rungs with live books, 1,588 pairs, all proven):
  cheapest combined top-of-book ask **exactly $1.0000**. Nothing under $1. The ladders
  are internally arbitrage-free to the tick.
* **Stored-book run** (16 ladders, 114 rungs, 730 pairs; 725 proven, 5 refuted): two
  structures reach a **positive net floor with `structural_status=guaranteed`**, and
  both are rejected on book age alone. They are historical snapshots, not opportunities:
  a floor that was positive three hours ago says nothing about now, and the live re-quote
  of the same Solana ladder showed the edge gone.

### Closest real structure (stored books, NOT tradeable)

```
event      KXSOLD-26SEP1817          venue kalshi
proof      proven — "greater 108.9999 implies greater 106.9999 on the same
                     observable, cutoff and settlement source"
consequent KXSOLD-26SEP1817-T106.9999  YES  [X > 106.9999]  vwap 0.0943
antecedent KXSOLD-26SEP1817-T108.9999  NO   [X > 108.9999]  vwap 0.8855
quantity   4,700 sets
payoff floor        $4,700.00      (1 per set; the unpaid state is pruned)
gross cost          $4,633.04
fees                   $33.48
total cost          $4,666.52
NET FLOOR             +$33.48      structural_status = guaranteed
books observed      2026-09-18T02:10:57Z / 2026-09-18T05:10:19Z
rejected because    oldest book is 11,839s old, over the 300s limit
```

Also disclosed on that certificate: the aligned-void branch. Same event, so a void
strikes both legs at once and returns the stake but not the fee — worst case −$33.48,
bounded by the fees and reported as `payoff_report.including_aligned_void`.

## Before/after on the same inputs

One correctness fix, measured on the identical stored books:

| structure | sizer maximising *average* cost < $1 | sizer maximising the *marginal* set |
|---|---|---|
| KXSOLD-26SEP1817 | 10,000 sets, blended $0.9934, **−$60.67**, not_guaranteed | 4,700 sets, **+$33.48**, guaranteed |
| KXBTCD-26SEP1710 | 3,756 sets, **−$21.22** | 2,302 sets, **+$26.29**, guaranteed |

Top of book on the first was $0.9400 — a 6c edge diluted to 0.66c by buying past the
point where the marginal set stopped paying, against a 1.27c fee.

## Coverage actually examined

| | stored books | live |
|---|---|---|
| ladders | 16 | 3 |
| rungs | 114 | 89 of 389 requested returned a book |
| pairs examined | 730 | 1,588 |
| proven / refuted | 725 / 5 | 1,588 / 0 |
| certified | 725 | 1,588 |
| accepted | 0 | 0 |
| cheapest combined ask | $0.9400 | $1.0000 |

Denominators: open Kalshi markets only, `provenance != demo`, `strike_type` in
{greater, greater_or_equal, less, less_or_equal}, grouped by `platform_event_id`, both
rungs requiring a non-empty book. Pairs are unordered within a ladder and not deduped
across ladders. Books are shared across the structures within one ladder — the 1,588
live structures rest on 89 books, so they are **not** 1,588 independent capacities.

## Commands

```bash
.venv/bin/pytest tests/test_implication_structures.py -q
.venv/bin/pmvl pair implication-scan --max-age-hours 24 --max-quote-age-seconds 300 --out run.json
.venv/bin/pmvl pair implication-scan --live --live-events 8 --live-rungs 60 --out live.json
.venv/bin/pmvl pair implication-replay run.json
```

## Hypotheses closed

* *Truncation at top-40 loses real matches* — **refuted**. All seven known-good pairs
  rank first by both orderings.
* *Monotonicity violations are the way in* — **refuted**. The executable condition is
  `ask_YES(L) < bid_YES(H)`, which an ask-vs-ask violation test neither implies nor
  follows from.
* *Available depth is capacity* — **refuted**, with a measured $94 swing.
* *Kalshi ladders are internally mispriced* — **not supported**. Live cheapest combined
  ask is exactly $1.0000 across 1,588 structures, and $1.0000 again across 4,038 on the
  wide sweep.
* *A basket sharing an event ticker is exhaustive* — **refuted, with a worked case**.
  Five rungs of a 170-rung event priced at $0.82 against a $1 floor that does not hold.

## Route B — exhaustive baskets (`pmvl pair basket-scan`)

Implemented. Buying every YES pays $1 because exactly one rung wins; buying every NO
pays $(N-1) because every rung but the winner does. Both directions priced.

`prove_partition` checks disjointness and coverage separately, because they break
different floors: overlapping rungs let two win and destroy the all-YES floor, a gap
lets every rung lose and destroys the all-NO floor.

**Coverage is proven on an inferred grid, not on the reals.** The Chicago ladder
`<74 | [74,75] | [76,77] | [78,79] | [80,81] | >81` leaves open gaps at (75,76),
(77,78), (79,80). A 75.4 reading settles every rung NO and pays $N against an assumed
floor of $(N-1). The grid step is inferred from the rung boundaries, the tiling verified
on it, and the step recorded on every certificate as a stated assumption. Gaps wider
than one step are refused.

### What the partition proof caught on real data

`KXBTC-26SEP1709` carries ~170 rungs; the local store holds books for five, all
adjacent. All-YES across those five costs **$0.82** and reads as an 18c/set edge. The
event can settle in any of the other 165 buckets, where all five pay nothing and the
whole $0.82 is lost. **Refused** — "a partition of the line needs exactly one rung open
at each end". This is the "same event implies exhaustive" error, caught by the gate
built to catch it.

Stored-book run: 20 baskets, 40 certificates, **10 partitions proven** (the complete
six-rung weather baskets, grid=1), **0 accepted** — a rung lacked an ask, or the
combined ask did not clear ($1.10 against a $1 floor on KXHIGHLAX-26SEP18).

### Route B live result

Command: `pmvl pair basket-scan --live --event-prefix KXHIGH --live-events 12 --max-rungs-per-event 8`

10 complete weather baskets, 60 of 72 rungs quoted live, 20 certificates,
**20 of 20 partitions proven** (grid=1, 6/6 rungs each), **0 accepted**.

| event | direction | floor/set | combined ask | gap |
|---|---|---|---|---|
| KXHIGHMIA-26SEP18 | all_no | $5 | **$4.9800** | **−$0.0200** |
| KXHIGHDEN-26SEP18 | all_no | $5 | $5.0200 | +$0.0200 |
| KXHIGHCHI-26SEP18 | all_no | $5 | $5.0000 | ±$0.0000 |
| KXHIGHAUS-26SEP18 | all_yes | $1 | $1.0500 | +$0.0500 |
| KXHIGHLAX-26SEP18 | all_yes | $1 | $1.0700 | +$0.0700 |
| KXHIGHCHI-26SEP18 | all_yes | $1 | $1.0800 | +$0.0800 |
| KXHIGHMIA/NY-26SEP18 | all_yes | $1 | $1.0900 | +$0.0900 |

One structure priced **below** its floor at top of book. It still loses:

```
event        KXHIGHMIA-26SEP18   all-NO basket
partition    PROVEN, grid 1, 6 of 6 rungs
assumption   observation reported on a grid of 1, inferred from rung boundaries

  T92     (92, inf)   NO  qty 3  vwap 0.9900  1 level   fee $0.01
  T85     (-inf, 85)  NO  qty 3  vwap 0.9900  1 level   fee $0.01
  B91.5   [91, 92]    NO  qty 3  vwap 0.7200  1 level   fee $0.05
  B89.5   [89, 90]    NO  qty 3  vwap 0.5067  2 levels  fee $0.06
  B87.5   [87, 88]    NO  qty 3  vwap 0.8200  1 level   fee $0.04
  B85.5   [85, 86]    NO  qty 3  vwap 0.9600  1 level   fee $0.01

quantity      3 sets      (depth ran out; the 4th set costs $5 or more)
payoff floor  $15.0000
gross cost    $14.9600    <- 4c of gross edge
fees           $0.1800    <- 4.5x the gross edge
total cost    $15.1401
NET FLOOR     -$0.1401    REJECTED
```

The structure is real, the partition is proven, the edge is real and 4 cents. Six legs
of Kalshi taker fee cost 18 cents. This is what the route is for: the arithmetic is
decided on the floor, and the floor says no.

## Next actions

1. Weather baskets are the liquid, completable structures and there are six cities per
   day. A scheduled re-quote near each city's settlement would sample the window where
   the books are thickest; nothing here polls, by design.
2. Cross-venue equivalence remains gated where it was; neither same-venue route touches it.
3. Fee structure is the binding economic constraint on both routes, not the spread. Six
   legs of Kalshi taker fee cost 18c against a 4c basket edge; two legs cost 1.27c against
   a 6c ladder edge that depth dilutes to 0.66c. A maker-side variant would change that
   arithmetic and is not implemented — nothing here assumes a maker fill.

## Confirmed on real data

`KXBTCMINMON-BTC-26SEP30` is 18 rungs all `less` — nested, not a partition. The basket
prover refuses it (18 rungs unbounded below, no upper tail) and the implication route
produces **153 proven certificates** from it, exactly as the split intends. A bucketed
partition's two tails, conversely, imply nothing in either direction. Both directions of
that complementarity are pinned by tests.

Live implication over today's Bitcoin ladder (`KXBTCD-26SEP1803`, 24 near-the-money rungs
quoted): 276 pairs, **276 proven**, 0 accepted, cheapest combined ask **$1.0300**.

## Resource limits hit

Kalshi's circuit breaker opened after 8 failures when the live basket path requested
1,379 rungs in one pass - most of them rungs of 168-rung Bitcoin ladders that can never
be completed within a request budget. Event selection now caps on the venue's true rung
count, so only completable baskets are fetched. No retry was attempted into the open
circuit.

## Boundaries held

Research/read-only throughout. No order, no funds, no wallet, no allowance, no live
mode. The scanner is a pure function of markets and books — a test asserts its source
contains no HTTP or order-placement symbol.
