# Snapshot retention policy

The deployment snapshot is bounded by a **budget**, not by a filter that happens
to produce a small enough file. This document says what the budgets are, why each
one exists, and what a reader loses when one of them bites.

Implementation: [`pmvl_shared/snapshot_retention.py`](../packages/shared/src/pmvl_shared/snapshot_retention.py).
Tests: [`tests/test_snapshot_retention_budget.py`](../tests/test_snapshot_retention_budget.py).

---

## The problem this replaced

The builder's prunes were each written because something was large at the time.
None of them was a bound, so the artefact's size was a function of things nobody
controlled: how many markets the venues listed, and how many times the pipeline
had run.

Both grow. The artefact crossed its 40 MiB ceiling twice, and both times every
job in the pipeline reported SUCCESS while the artefact silently stopped being
publishable. The second time it cost roughly 199 consecutive failed scheduled
runs and left production stale at a quote cutoff of 5 August.

On the operational database that motivated this policy, plain selection produced
**52.2 MB**. Three things drove it, and no filter could have bounded any of them:

| Growth vector | Size | Why a filter cannot bound it |
|---|---:|---|
| `backtest_runs` + `backtest_trades` | 12.9 MB | Ten runs are written per pipeline execution and nothing removed one. Grows with *time*, not with data. |
| Resolved-market free text | 4.8 MB | A settled market keeps its row forever, because it never stops being settled. Grows monotonically. |
| `trades` / `price_snapshots` per market | ~2.4 MB | Append-only histories with no per-market cap at all. |
| Live browsable universe | balance | Grows with the venues' own boards. |

`volume_24h > 0` is a *selection* rule. If Kalshi lists twice as many actively
traded contracts next year, the artefact is twice as big and the pipeline fails
again.

---

## Two separate decisions

**Selection** — `scripts/build_snapshot.py`. What has any business being in a
deployment artefact: raw provider payloads no endpoint reads, superseded order
books and predictions, markets that can render nothing. Unconditional; it would
apply even if the artefact were a tenth the size.

**Retention** — `pmvl_shared/snapshot_retention.py`. What fits. Applied after
selection, expressed as budgets, with a fixed tightening ladder.

Keeping them apart is what makes the artefact's size a decision that can be read
back out of the manifest rather than an emergent property.

---

## The budgets

Applied in this order — cheapest and least destructive first.

### 1. Backtest history: newest 30 runs

Ten runs per pipeline execution, so 30 is roughly three executions of comparable
history: enough to watch a strategy's numbers move, far short of an archive.
`backtest_trades` cascades.

Ordered by `(created_at DESC, id DESC)`. `created_at` alone is not a total order
— one execution writes all ten of its runs inside the same second.

**Reclaimed on the motivating build: 13.0 MB (52.2 → 39.2).**

### 2. Free text on resolved markets outside the audit set

`description`, `settlement_rules_raw` and `settlement_rules_normalized` are
blanked on markets whose status is `settled` or `closed`.

The **row stays**. A settled market is the subject of a settlement and of
whatever outcome history a calibration study reads; deleting it to save bytes
would throw away the only record that the contract existed. What goes is ~1.1 kB
per row of prose that the detail page renders for a *live* contract — where a
reader is deciding whether the rules say what they think — and that nobody opens
for a contract that has already resolved.

Markets carrying a recommendation are exempt: the case study renders exactly that
text beside the call published against it.

**Reclaimed: 6.5 MB (39.2 → 32.7).**

### 3. Per-market history caps

- `trades`: 20 most recent per market
- `price_snapshots`: 12 most recent per market

Newest-first by `id`, which is insertion order on both tables and therefore total
over rows sharing an `observed_at` to the second.

**Reclaimed: 2.4 MB (32.7 → 30.3).**

### 4. The market ladder — last resort only

Engages only if the artefact is still above the **32 MiB build target** after
steps 1–3. Each rung is a `(live budget, resolved budget)` pair:

```
(4000, 6000) → (3000, 4000) → (2000, 3000) → (1600, 2000)
             → (1200, 1200) → (800, 600)   → (400, 300)
```

- **Live markets** are ranked by 24h volume, descending. That is a coverage
  judgement stated once: when the artefact cannot hold every board, the contracts
  a reader is most likely to look up are the ones being traded.
- **Resolved markets** are ranked by settlement time, descending. Recent history
  is what a track record and a calibration study read.

Tightening stops at the first rung under target.

**On today's data the ladder does not engage. No market is dropped for size.**
That is why it is step 4: steps 1–3 reclaimed 22 MB without costing any coverage,
and a policy that had started by deleting markets would have thrown the browsable
universe away for nothing.

---

## What is never traded away

Markets referenced by `recommendations` or `recommendation_snapshots` survive
every budget, at every rung.

This is the audit trail. `recommendation_snapshots` is what the public track
record reads; `recommendations` is what settlement grading resolves through.
Shrinking the artefact by dropping the market a published call was made against
would make the record of what was claimed shorter — the one trade this repository
exists to refuse.

`settlements` rows are **nulled, not deleted**, when their market is budgeted
out. The foreign key is nullable by design and the row carries
`(platform, platform_market_id)` beside it, so resolution history stays
identifiable after the market row is gone. Resolution history is not
reconstructible; a market row is.

---

## Determinism

Every ordering that picks survivors ends in a unique `id` tiebreak, every budget
is a fixed integer, and nothing reads the clock or a random source. The same
operational database produces byte-identical output — which is what lets the
manifest carry a content hash that means anything, and what lets `verify_artifact`
compare a committed blob against a claim.

The tightening loop keeps deleting from the same database rather than rebuilding
at each rung. That is valid only because rungs are non-increasing, so each rung's
survivor set is a subset of the one before it; `RetentionPolicy.__post_init__`
rejects a non-monotonic ladder, and a test asserts that walking the ladder gives
the same market set as building directly at the rung it stopped on.

---

## Failing closed

If the tightest rung still exceeds the **40 MiB ceiling**, the builder raises
`SnapshotTooLargeError`, deletes the manifest and exits non-zero. Nothing
publishable is left behind.

The ceiling is not raised to make a build pass. The artefact ships inside a
serverless function bundle: one that does not fit is not a validation preference
to relax, it is a deployment that would not work. The number is declared once, in
`snapshot_retention.MAX_SNAPSHOT_BYTES`, and imported by
`scripts/validate_snapshot.py` — a second copy is how a "fixed" build starts
failing the gate again the next time somebody adjusts one of them.

---

## Reading a retention decision back

Every build writes a `retention` block into the manifest:

```json
{
  "retention": {
    "policy": { "target_bytes": 33554432, "max_bytes": 41943040, "max_backtest_runs": 30, ... },
    "bytes_before": 52150272,
    "bytes_after": 30265344,
    "markets_before": 6757,
    "markets_after": 6757,
    "audit_markets": 462,
    "ladder_engaged": false,
    "steps": [
      { "name": "backtest_history",     "bytes_after": 39198720, "detail": "backtest_runs 150 -> 30, backtest_trades 35406 -> 7587" },
      { "name": "resolved_market_text", "bytes_after": 32653312, "detail": "stripped free text from 3906 resolved markets" },
      { "name": "per_market_history",   "bytes_after": 30265344, "detail": "trades 13688 -> 3060 (<= 20/market); price_snapshots 3898 -> 3826 (<= 12/market)" }
    ]
  }
}
```

"Why is this market not in the snapshot?" has two very different answers — it was
never renderable, or it lost a size budget — and without this block the only way
to tell them apart is to re-run the build against a database that no longer
exists.

---

## Adding a table

A new child table of `markets` must be added to
`snapshot_retention.MARKET_SCOPED_TABLES`. A table missing from the cascade is an
unbounded leak: the pruned market never comes back, so nothing ever deletes its
rows, and every run adds another generation of orphans. That is exactly how
`market_rule_versions` grew to 9.75 MB of a 10.15 MB table while looking, from
the outside, like ordinary growth.

`tests/test_snapshot_retention_budget.py::TestTheCascadeCoversEveryMarketScopedTable`
is parameterised over that tuple, so adding the table to it also adds the test.
