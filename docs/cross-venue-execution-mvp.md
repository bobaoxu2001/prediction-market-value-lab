# Cross-venue arbitrage execution MVP — shadow only

What this round built, what it measured, and what it refuses to claim.

Scope: research / paper / shadow. No order was submitted to any venue, no funds
moved, no token allowance approved, no account setting changed. The execution mode
enum still has no live member, and the transport gate still refuses any non-GET aimed
at a production host.

---

## A. Conclusions by dimension

| Dimension | Verdict | Basis |
|---|---|---|
| **Structural** — can a proven pair be shown not to lose? | **Yes, mechanically** | `settlement/payoff.py` enumerates the full joint settlement space and returns `GUARANTEED` only when the minimum payout over every un-pruned state exceeds every dollar spent. Verified on 24 tests. |
| **Structural** — does any *real* pair currently qualify? | **No** | Zero pairs reach `VERIFIED_EQUIVALENT_*`; zero markets are attested. Both gates independently refuse. |
| **Quotes** — are both venues' books read correctly? | **Yes** | Kalshi's bids-only book is reconstructed per the published rule (YES ask = $1 − NO bid), confirmed against the live docs. Polymarket YES/NO are read as two independent token books. |
| **Quotes** — is the outcome↔token mapping verified? | **No, and now blocked** | The provider assigns `clobTokenIds[0]` to YES positionally. Only 6,464 of 13,086 open Polymarket markets (49%) are labelled `('Yes','No')`. The execution bridge now refuses the rest rather than guessing. |
| **Economics** — are both buy directions priced at real depth? | **Yes** | Both orientations are walked; sizing is equal settlement shares, not equal notional; the limit is the worst level touched, not the VWAP. |
| **Economics** — is any real pair net positive? | **No** | 3 of 7 candidates are positive after fees (+0.6% to +0.85%), all negative after the capital cost of a 2028 resolution (≈ −10% ROI). |
| **Execution** — is a two-leg failure distinguishable from success? | **Yes** | `arbitrage_completed` is recomputed from actual fills by the independent evaluator. Nine failure injections verified. |
| **Funding** — can two candidates double-spend? | **No** | Per-venue reservations, all-or-nothing across legs, unread balance refuses. |
| **Access** — is trading permission established? | **No, and it blocks** | `AccessStatus` defaults to `UNVERIFIED`, which fails `tradeable`. Nothing in this round checks venue or regional eligibility, so nothing may proceed past it. |

### Market-data contract, verified against the current docs (2026-09-16)

| | Kalshi | Polymarket |
|---|---|---|
| Docs index | `https://docs.kalshi.com/llms.txt` | `https://docs.polymarket.com/llms.txt` |
| API version | REST v2 (`/trade-api/v2`; `Create Order (V2)`) | Gamma + CLOB, unversioned paths |
| Production base | `https://api.elections.kalshi.com/trade-api/v2` | `https://gamma-api.polymarket.com`, `https://clob.polymarket.com` |
| Sandbox base | `https://external-api.demo.kalshi.co/trade-api/v2` | **none verified — do not fabricate one** |
| Book endpoint | `GetMarketOrderbook` | `GET /book?token_id=…` |
| Book shape | **Bids only.** The docs state it "returns yes bids and no bids only (no asks are returned)" | Two independent token books, each with bids and asks |

The repo derives Kalshi's asks as **YES ask = $1 − best NO bid** and **NO ask = $1 −
best YES bid**, carrying sizes across unchanged. That matches the documented semantics.
It does **not** use `1 − YES ask` to stand in for a NO ask anywhere; the sizes on the
resting opposite-side bids *are* the liquidity. Polymarket's YES and NO books are read
separately and never algebraically linked, because on that venue they are genuinely
independent and a missing side means no liquidity rather than an implied price.

Cost-model constants in force: `slippage_ticks=1` per side,
`cross_platform_execution_risk_usd=0.01` per set, `min_orderbook_notional_usd=25`,
`max_quote_age_seconds=300`.

---

## B. What was reused, and what changed

**Reused unchanged** (already implemented and test-passing before this round):

- `execution/modes.py` — six-layer write refusal. Untouched.
- `execution/risk.py`, `ledger.py`, `reconcile.py`, `authorization.py`, `sizing.py` — untouched.
- `execution/idempotency.py` — the derivation pattern was followed, not modified.
- `arbitrage/cross_platform.py` — already searched both orientations, walked full depth
  and sized by matched sets. Untouched.
- `pricing/fees.py`, `pricing/orderbook.py`, `pmvl_shared/money.py` — untouched.
- `providers/kalshi.py::parse_orderbook` — already correct. Untouched.

**Added** (no existing file was modified except to register the CLI):

| File | Lines | Why it could not be an edit to something existing |
|---|---|---|
| `settlement/payoff.py` | 615 | Had to be unable to see the signal. A method on the scanner could not be. |
| `execution/venues.py` | 289 | `OrderIntent` is integer cents and whole contracts; Polymarket is neither. |
| `execution/funding.py` | 305 | `risk.py` checks one intent against a balance; a pair needs reservations across two venues. |
| `execution/assessment.py` | 134 | Six axes that must not overwrite each other. |
| `execution/twoleg.py` | 1,208 | `OrderIntent`/`LedgerRecord` have no sibling-leg concept. |
| `execution/simulator.py` | 312 | Fault injection against real depth. |
| `execution/candidates.py` | 349 | Rebuilds the case from primary sources, discarding the scanner's label. |
| `matching/cancellation.py` | 277 | Rule-text reading, and the review gate that stops a regex certifying a trade. |
| `arbitrage/coverage.py` | 124 | The funnel. |
| `execution/shadow_pairs.py` | 341 | The runner. |
| `pmvl_worker/crossvenue_cli.py` | 252 | `pmvl pair scan / replay / attest-draft`. |

**Three defects found and fixed while testing**, all in code written this round:

1. A linkage claiming matched void handling pruned divergent states even when the two
   rules admitted *different* non-binary outcomes — deleting exactly the states where
   that asymmetry loses money. Now the rules win and the void claim is withdrawn.
2. A funnel stage that proposed zero pairs rendered as "not reached", which reads as a
   pipeline that never ran rather than a matcher that compared everything and liked
   nothing. The prescreen's rejections are now recorded explicitly. Fixing this is what
   made the live result legible at all.
3. A fully unwound pair reported `structural_status=unknown`, because the evaluator was
   handed an empty basket. An empty basket is not unknown — it pays $0 in every state,
   exhaustively, which makes the money already spent a *realised loss*. Reporting it as
   unknown hid a loss that was both known and final.

---

## C. Real coverage funnel

Source: local database, open markets with an order book stored in the last 24 hours.
Command: `pmvl pair scan --max-quote-age-seconds 86400 --bankroll 5000`

Observation window: `2026-09-15T02:01:27Z .. 2026-09-16T01:49:08Z`
Book age across the set: min 219s, median 8,405s (2.3h), max 85,879s (23.9h).

```
markets_loaded              703 / 703       288 Kalshi + 415 Polymarket
pairs_proposed              117 / 119,520   recall ceiling: 0.098% of the cross-product
rules_compared              117 / 117
equivalence_verified          0 / 117       110 REJECTED, 7 PROBABLE_MATCH
books_fresh                   —             never reached
gross_spread                  —             never reached
net_positive                  —             never reached
settlement_proven             —             never reached
fully_attested                —             never reached
shadow_executed               —             never reached
```

**The "7 verified cross-platform matches" figure resolves here.** It is 7
`PROBABLE_MATCH` pairs — below the threshold for any guaranteed claim — not 7 verified
matches. The persisted `market_matches` table holds 26 rows, *all* of them
`rule_compatibility = 'similar'`, none `identical` or `equivalent`.

Components that were not `MATCH`, across all 117 scored pairs:

| Component | Not-MATCH count | Reading |
|---|---|---|
| `threshold` | 117 / 117 | Extraction asymmetry, see below |
| `data_source` | 117 / 117 | Settlement authority unidentified on at least one side |
| `cancellation` | 117 / 117 | Always `UNKNOWN` by construction |
| `entity` | 110 mismatch | Prescreen pairing different entities |
| `settlement_rule` | 107 | Measurement basis not stated |
| `time` | 77 unknown + 34 mismatch | |
| `operator` | 72 unknown + 36 mismatch | |

### The same funnel against fresh live books

Command: `pmvl pair scan --live --limit 800 --bankroll 5000`
Observation window: `2026-09-16T01:56:05Z .. 2026-09-16T02:00:21Z` (books seconds old)

```
markets_loaded             1,587 / 1,587    800 Kalshi + 787 Polymarket
pairs_proposed                 0 / 629,600  prescreen found no shared tokens or entities
rules_compared                 —            never reached
...
```

**A cold live scan of 1,587 markets across both venues proposed zero pairs.** Not zero
opportunities — zero *comparisons*. The 117 pairs the database run found come from a
set accumulated over time and biased toward markets this pipeline has already been
interested in; the most-recently-closing markets on each venue, fetched fresh, have
essentially no subject overlap under the current prescreen.

This is the binding constraint on the whole product. The settlement engine, the cost
model and the execution machinery are all downstream of a matcher that, on live data,
proposes nothing.

### Gaps this exposes

1. **Recall ceiling of 0.098%.** 117 pairs out of 119,520 possible. "No opportunity"
   is a statement about those 117, and about nothing else.
2. **A live run proposed zero pairs from 800×787 fresh markets** (629,600 combinations).
   See above. Recall, not economics, is what currently stops this system.
3. **Kalshi weather markets cannot match on entity.** The city lives in the series
   ticker, not the title — `"Will the maximum temperature be 86-87° on Sep 5, 2026?"`
   against Polymarket's `"...in Miami..."`. Entity comparison cannot succeed on text
   that does not contain the entity.
4. **A bare year is extracted as a numeric threshold.** `"...nominee in 2028"` yields
   `threshold=2028` on Polymarket, while Kalshi correctly suppresses it (its structured
   strike metadata says the condition is non-numeric). The component then reports
   `None vs 2028`. **Not fixed this round**: the fix changes `resolution_hash` for every
   market with a year in its title, which invalidates every stored hash and every
   attestation keyed on one — a migration cost for no gain here, because four other
   components remain `UNKNOWN` on these same pairs and the verdict would not move.

---

## D. The closest real candidates

All 7 `PROBABLE_MATCH` pairs are the same structure: a Kalshi 2028 Democratic-nomination
market against its Polymarket counterpart. They are, on reading, the same question:

> Kalshi `KXPRESNOMD-28-AOC` — "Will Alexandria Ocasio-Cortez be the Democratic
> Presidential nominee in 2028?"
> Rules: *"If Alexandria Ocasio-Cortez wins and accepts the nomination for the
> Presidency for the Democratic party in 2028, then the market resolves to Yes."*
>
> Polymarket `559653` — "Will Alexandria Ocasio-Cortez win the 2028 Democratic
> presidential nomination?"
> Rules: *"This market will resolve to 'Yes' if the named individual wins and accepts
> the 2028 nomination of the Democratic Party for U.S. president. Otherwise, this
> market will resolve to 'No'."*

Four of the seven show a genuine sub-$1 top-of-book sum. Economics at the size where
the pair price stays under $1, capped at 10,000 shares:

| Pair | Direction | Top of book | Optimal size | After fees | After all costs |
|---|---|---|---|---|---|
| Newsom | K-YES + P-NO | 0.9780 | 10,000 | **+$60.33** | −$1,111.67 (−10.01%) |
| **AOC** | K-YES + P-NO | **0.9690** | 10,000 | **+$58.96** | −$1,112.04 (−10.01%) |
| M. Obama | K-NO + P-YES | 0.9880 | 10,000 | **+$84.18** | −$1,099.82 (−9.91%) |
| Ossoff | K-NO + P-YES | 0.9980 | 90 | −$1.16 | −$12.42 (−12.08%) |

Worked example, AOC, 10,000 shares:

```
payout                    $10,000.00
gross cost                 $9,787.62   (VWAP K 0.1600 + P 0.8188 = 0.9788)
+ Kalshi taker fee            $94.08
+ Polymarket taker fee        $59.35
= net after fees              +$58.96   <- level 3 reached
+ slippage (1 tick/side)      $20.00
+ transfer                     $1.00
+ capital cost             $1,050.00   <- resolves 2028-11-07, ~2.1 years out
+ execution risk             $100.00
= NET                     -$1,112.04
```

**The capital cost is what kills it.** A 0.6% gross return over 2.1 years is roughly
0.3%/year. These are not arbitrage; they are a carry trade that loses to cash.

Against the spec's six levels, on real data:

1. Displayed probability anomaly — **yes**
2. Executable quotes with a gross spread — **yes, 4 pairs**
3. Net positive after fees — **yes, 3 pairs** (+0.59% to +0.85% absolute)
4. Net positive after all costs — **no, 0 pairs**
5. Simulated two-leg completion on a real pair — **no**, blocked independently at
   equivalence and at attestation
6. Live fills / realised settlement — **not attempted, out of scope**

Note the top-of-book figure overstates the opportunity badly: walking the *whole*
available depth on the AOC pair (52,076 shares) gives a VWAP sum of 0.9850 and a
net-after-fees of **−$9.48**. Best-level × arbitrary quantity is not a number this
system reports.

---

## E. Simulated execution, normal and failure paths

State machine:

```
DETECTED → VALIDATED → FUNDS_RESERVED → RECHECKED → SUBMITTING
         → BOTH_FILLED / PARTIALLY_FILLED / ONE_LEG_FILLED / NO_FILL / UNKNOWN
         → HEDGED / UNWINDING / ESCALATED
         → RECONCILED
```

`ABANDONED` and `NO_FILL` were added: a candidate refused before anything was sent is
a different event from one that reached a venue and came back empty.

**Normal path**, 100 shares, Kalshi YES @0.45 + Polymarket NO @0.50:

```
VALIDATED       every reachable settlement state nets at least $3.26 on $96.74 committed
FUNDS_RESERVED  reserved $46.74 on kalshi, $50.00 on polymarket
RECHECKED
SUBMITTING
BOTH_FILLED     both legs filled 100 settlement shares
RECONCILED      $96.74 spent against 100 hedged shares
→ arbitrage_completed = True, realised_pnl = $3.26
→ reservations: kalshi committed $46.74, polymarket committed $50.00
```

**One-legged failure**, same pair, Polymarket leg rejected at submission:

```
VALIDATED       every reachable settlement state nets at least $3.26 on $96.74 committed
FUNDS_RESERVED  reserved $46.74 on kalshi, $50.00 on polymarket
RECHECKED
SUBMITTING
ONE_LEG_FILLED  only kalshi:KXDEMO-26-A filled (100); the position is directional, not hedged
UNWINDING       legs are unbalanced; closing the exposed quantity
HEDGED          unmatched quantity closed at a cost of $3.72; 0 settlement shares remain hedged
RECONCILED      every leg accounted for; $5.46 spent against 0 hedged shares
→ arbitrage_completed = False
→ realised_payoff.structural_status = not_guaranteed, min_payout = $0.00
→ realised_pnl = -$5.46
```

Per-leg reconciliation for that run:

| Leg | Requested | Filled | Fees | Closed | Net spent |
|---|---|---|---|---|---|
| `kalshi:KXDEMO-26-A` | 100 @ 0.45 | 100 @ 0.45 | $1.74 | 100 @ 0.43 | **$5.46** |
| `polymarket:token-no-demo` | 100 @ 0.50 | 0 | — | — | $0.00 |

Funding after the run: the Kalshi reservation of $46.74 **committed $5.46** — what was
actually lost — and the Polymarket reservation of $50.00 was **released** in full. The
$5.46 is the 2c round-trip spread on 100 shares plus both fees. Every dollar is
attributable.

*(Inputs above are synthetic fixtures; the export's `book_provenance` says `fixture`.
These figures describe this system's logic, not a market opportunity.)*

**Failure injections, all verified by test:**

| Injection | Result | Key assertion |
|---|---|---|
| Leg 2 rejected, leg 1 live | `ONE_LEG_FILLED → UNWINDING → HEDGED → RECONCILED` | `arbitrage_completed = False`; unwind loss recorded, not rounded away |
| Unequal fills (50 vs 100) | `PARTIALLY_FILLED → UNWINDING → HEDGED` | Excess 50 closed, `matched_quantity = 50` |
| Submit timeout, order had landed | `UNKNOWN → BOTH_FILLED` | Queried by `client_order_id`; venue order count stays 1 — **no resubmission** |
| Confirm never resolves | `UNKNOWN → ESCALATED` | Funds stay **held**, not released; no auto-unwind |
| Book moves a tick after ack | fills at 0.51 | Realised PnL $2.26 vs $3.26 clean; the tick lands in the realised figure |
| Book moves past the limit | leg does not fill | `arbitrage_completed = False` |
| Cancel racing a fill | `ONE_LEG_FILLED → UNKNOWN → BOTH_FILLED` | Reconciliation re-read caught it; first read was not treated as final |
| Process crash mid-pair | `recover()` → `RECONCILED` | Same `client_order_id`s; **no liquidity consumed** — recovery is a read |
| Live (non-simulation) client | `NotASimulation` raised at construction | |

**Per-leg reconciliation** is recorded for every leg: requested vs filled quantity and
price, fees, venue order id, every attempt id, the reservation ref, closed quantity,
close proceeds, unwind loss, gross and net cash out, plus free-text notes including
*"venue acknowledged the request; this is not evidence of a fill"* and any correction
reconciliation applied.

**The property that matters**: `arbitrage_completed` requires four independent
conditions — terminal state is `BOTH_FILLED`/`RECONCILED`, every leg filled its full
size, zero unwind cost, and the settlement evaluator re-run *on the actual fills*
returns `GUARANTEED`. A one-legged position fails the fourth even if everything
upstream said the pair was fine, because the evaluator sees one leg and enumerates the
state where it pays nothing.

---

## F. Tests

```bash
.venv/bin/pytest -q                      # full suite
.venv/bin/pytest tests/test_cross_venue_execution.py -q
.venv/bin/pytest tests/test_settlement_payoff.py -q
.venv/bin/pytest tests/test_cross_venue_pipeline.py -q
.venv/bin/pytest tests/test_venue_constraints_and_funding.py -q
.venv/bin/pytest tests/test_cancellation_attestation.py -q
.venv/bin/ruff check packages/ services/ tests/
```

| Suite | Tests | Result |
|---|---|---|
| `test_cross_venue_execution.py` | 27 | pass |
| `test_settlement_payoff.py` | 24 | pass |
| `test_cross_venue_pipeline.py` | 21 | pass |
| `test_venue_constraints_and_funding.py` | 17 | pass |
| `test_cancellation_attestation.py` | 16 | pass |
| **New this round** | **105** | **105 pass, 0 fail** |
| Pre-existing suite | 2,468 pass / 3 skip | unchanged |

`ruff check packages/ services/ tests/` — clean.

### What these tests do not cover

- **Any real venue.** Every fill is simulated. The 2 skipped tests are the Kalshi demo
  order path, skipped because no demo credentials are present.
- **Websocket book maintenance.** The spec asks for reconnect, out-of-order, duplicate
  and sequence-gap handling. This round consumes REST snapshots only; there is no
  streaming book, so none of that is implemented *or* tested. The one guarantee that
  does hold is that a stale snapshot cannot overwrite a fresher one, because nothing
  writes books here at all — `build_pair_candidate` reads a book and rejects it on age.
- **Concurrency.** `FundingLedger` is not thread-safe and is not exercised
  concurrently. Reservations are correct under sequential use, which is how the runner
  uses them.
- **Polymarket order construction.** `VenueOrderSpec` models the constraints, but no
  EIP-712 signing, allowance or on-chain settlement path exists. Nothing is built that
  could submit a Polymarket order.
- **negRisk.** Mutual-exclusivity and exhaustiveness checks for negRisk events are
  untouched this round; the two-leg path does not use them.

---

## G. Runnable commands

All verified to run.

```bash
# Shadow scan from the local database
.venv/bin/pmvl pair scan --max-quote-age-seconds 86400 --bankroll 5000 --out run.json

# Shadow scan against fresh public books (read-only, unauthenticated)
.venv/bin/pmvl pair scan --live --limit 800 --bankroll 5000 --out run_live.json

# Replay an exported run: funnel, then each pair's transitions and legs
.venv/bin/pmvl pair replay run.json
.venv/bin/pmvl pair replay run.json --pair <pair-id>

# Draft a reviewer's worksheet for one market's cancellation terms
.venv/bin/pmvl pair attest-draft KXPRESNOMD-28-AOC --venue kalshi --out draft.json
```

There is no stop command: `scan` is a single pass that terminates. Nothing runs as a
daemon, so there is nothing to stop.

---

## H. Gates remaining before any limited live trading

Each gate below is currently **closed**. None may be opened by configuration.

| # | Gate | Evidence today | Missing | Minimum action |
|---|---|---|---|---|
| 1 | A real pair reaches `VERIFIED_EQUIVALENT_*` | 0 of 117 | `threshold`, `operator`, `time`, `data_source`, `settlement_rule` all UNKNOWN on the closest candidates | Fix rule extraction so these components resolve; re-measure the funnel |
| 2 | Cancellation terms attested on both venues | 0 attestations | A person has to read two rule pages per pair | Run `pair attest-draft`, confirm against published rules, sign `reviewed_by` |
| 3 | Outcome↔token mapping verified | 49% of Polymarket markets | The other 51% are `Over/Under`, `Up/Down`, team names | Derive the mapping from the question text, not the label; or restrict to `Yes/No` markets |
| 4 | Net positive after *all* costs | 0 of 7 | Capital cost dominates on long-dated markets | Only near-dated pairs can clear; none currently qualify |
| 5 | Real per-venue balances | Both `UNKNOWN`; reservation refuses | No credentials present for either environment | Out of scope — requires explicit authorization |
| 6 | Trading eligibility per venue and region | `AccessStatus.UNVERIFIED` | Never checked | Must be checked directly and independently. **Not** to be worked around by proxy, identity or deployment location |
| 7 | Kalshi demo order behaviour | Code present, 2 tests skipped | No demo credentials | Provide demo credentials; run the existing demo harness |
| 8 | A Polymarket sandbox | **None exists that this round verified** | — | Do not fabricate one. Kalshi demo prices must never be paired with Polymarket live prices and called an opportunity |
| 9 | A live write path | Does not exist | `ExecutionMode` has no live member; `assert_transport_write_allowed` refuses | Deliberate, reviewable source change — not a config flag |

Credential presence is not authorization. Nothing in this round requested plaintext
keys, and nothing enabled live trading.
