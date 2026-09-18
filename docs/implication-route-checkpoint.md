# Implication route — checkpoint

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
  ask is exactly $1.0000 across 1,588 structures.

## Next actions

1. `--live` currently seeds ladders from prior book coverage; 300 of 389 requested rungs
   returned no book. Widen to quote by event directly and record which rungs are unquoted.
2. Route B (same-venue exhaustive/mutually-exclusive baskets) is **not implemented**.
   `between` rungs are correctly `NOT_APPLICABLE` here and are the natural input to it —
   81,975 open Kalshi markets carry that comparator.
3. Cross-venue equivalence remains gated where it was; nothing in this route touches it.

## Boundaries held

Research/read-only throughout. No order, no funds, no wallet, no allowance, no live
mode. The scanner is a pure function of markets and books — a test asserts its source
contains no HTTP or order-placement symbol.
