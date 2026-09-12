# Cross-venue matching: what stops a Kalshi/Polymarket arbitrage being found

**Measured 12 September 2026** against a live ingest of 8,193 Kalshi and 1,500
Polymarket markets (9,684 written, 897 orderbooks).

Two questions look identical from outside an empty arbitrage list: *the venues do not
list the same contracts*, and *we cannot see that they do*. This document separates
them with numbers, because the first is a finding and the second is a bug.

## What the machinery can now do

Before this cycle the guaranteed-arbitrage label was unreachable by construction, for
two independent reasons:

1. `_cancellation_component` in `matching/equivalence.py` returned `UNKNOWN`
   unconditionally, so `VERIFIED_EQUIVALENT_STRICT` — the only verdict mapped to
   `ArbitrageLabel.EXECUTABLE` — could never be derived.
2. `arbitrage/pipeline.py` passed `scan_cross_platform` a bare `MatchVerdict`, which
   `_as_equivalence` deliberately caps at `STANDARD`, so even a perfect pair was
   labelled Rule Mismatch Risk.

Both are fixed. The cancellation component now classifies each venue's own void and
postponement wording (`normalize/rules.py::classify_void_language`) and the pipeline
hands the scanner a full `EquivalenceScore`. `tests/test_guaranteed_arbitrage.py`
pins the end-to-end property: a pair agreeing on all eight components, including a
stated and matching void policy, is labelled `executable`, and every way of failing
that check still blocks the guarantee.

## What the live scan found

| | |
|---|---|
| Candidate pairs examined | 478 |
| Reached `identical` | **0** |
| Demotions that were genuine contradictions | 2,850 (95%) |
| Demotions that were missing information | 153 |

Top demotion reasons, after the alias fix below:

| Code | Count | Kind |
|---|---:|---|
| `low_title_similarity` | 442 | contradiction |
| `measurement_basis_differs` | 430 | contradiction |
| `threshold_differs` | 427 | contradiction |
| `source_differs` | 418 | contradiction |
| `cutoff_drift_hours` | 364 | contradiction |
| `entity_mismatch` | 314 | contradiction |

The contradictions are, on inspection, real. The highest-scoring candidate pair was
Kalshi's `SOL price on Sep 12, 2026?` (strike 97.9999, CF Benchmarks, average basis)
against Polymarket's `Will the price of Solana be above $110 on September 13?` (UMA
oracle, closing basis). Different strike, different day, different source, different
measurement basis. Refusing that pair is correct.

## Gap 1 — ticker versus name (fixed)

`unmatched_proper_nouns` compared raw text, so Kalshi's `SOL` and Polymarket's
`Solana` read as two different subjects. Both sides are now alias-expanded through
the existing `ENTITY_ALIASES` map before comparison.

Effect on the live scan: `entity_mismatch` fell from 478 to 314, and it stopped being
the single largest demotion reason. It produced no new matches on its own, because the
surviving candidates contradict on other terms as well.

This class of bug only ever **discards** real candidates — it cannot make a false pair
look good — which is why it survived a suite built around not over-claiming.

## Gap 2 — event linkage (open)

| | Kalshi | Polymarket |
|---|---:|---:|
| Open markets with a linked event row | 5,714 | 1,784 |
| Open markets with `platform_event_id` but **no** event row | **26,965** | 1,483 |

84% of open Kalshi markets carry an event identifier that resolves to nothing,
because event fetching is capped at `min(600, market_limit)` while market fetching is
not. Two things depend on that link:

* The multi-outcome scanner needs `Event.outcome_count` to establish that a basket is
  complete, and correctly refuses to evaluate an event whose count is unknown. The
  live scan skipped dozens of events for exactly this reason.
* Matching has no event context for a market whose own title does not carry it.

## Gap 3 — the venues title sports contracts in different shapes (open)

This is the largest overlap between the two venues and the one the matcher cannot
currently see at all.

| Venue | Title | Outcomes |
|---|---|---|
| Kalshi | `Seattle wins` | Yes / No |
| Polymarket | `Seattle Mariners vs. Athletics` | `["Seattle Mariners", "Athletics"]` |

Kalshi names the **outcome** and leaves the matchup to the event; Polymarket names
the **matchup** and leaves the outcome to the `outcomes` array. Token similarity
between `Seattle wins` and `Seattle Mariners vs. Athletics` is far below the 0.35
floor, so the pair is never proposed — which is what `low_title_similarity` (442) and
`low_entity_overlap` (311) are largely counting.

Closing this needs three things, in order: Kalshi event linkage (Gap 2), team-name
canonicalisation (`Seattle` ↔ `Seattle Mariners`), and outcome-level mapping so the
scanner knows which Polymarket outcome token corresponds to Kalshi's YES.

**Closing it would not by itself produce risk-free arbitrage.** Sports contracts turn
on a scheduled occurrence, so `postponement_is_material` is true for them, and the
strict verdict requires *both* venues to have stated matching postponement handling.
Most sports pairs should therefore land on Rule Mismatch Risk even once they are
found — which is the correct answer, not a failure of the matcher.

## What would change the conclusion

Nothing here shows a cross-venue arbitrage that the system missed. It shows that the
system's recall is bounded by event coverage and title shape, and that its precision
gates are working. A fair test of "is there risk-free arbitrage between these venues"
requires Gaps 2 and 3 closed first; until then the honest statement is that no pair
in the sampled candidate space settles on identical terms.
