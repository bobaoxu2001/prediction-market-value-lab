# PMVL Alpha Engine

**Status: RESEARCH INFRASTRUCTURE READY. There is no verified economic backtest.**

Run `pmvl alpha-readiness` for the current, computed version of that statement.

---

## Why this exists

PMVL used to go from a fair probability straight to a recommendation. That
collapses three different questions into one, and each of them can be answered
correctly while the trade is still wrong.

| | Question | Standard of success |
|---|---|---|
| **Model A — Outcome** | `P(settles YES \| information at t)` | Calibration. A good model's 30% events happen 30% of the time. |
| **Model B — Alpha** | `E[price(t+h) − price(t) \| information at t]` | Realised convergence. |
| **Model C — Execution** | `P(fill at p \| book, state, t)`, and the edge given a fill | Fill rate and realised vs expected edge. |

Model fair 42% against a market at 30% **does not imply a trade**. The market may
hold information the model does not. "The market is wrong" and "the market will
move" are separate claims — one is about the world, the other about the other
participants.

The separation is enforced by types, not by discipline. `AlphaScore` requires a
horizon and a component breakdown, so a probability cannot be typed into the slot
where an expected price move belongs. That is the same device the independence
gate uses: make the wrong thing hard to express.

---

## The flow

```
              ┌──────────────────────────────────────────────────────┐
              │ Venues (Kalshi, Polymarket) · NWS · Cleveland Fed ·  │
              │ Coinbase · ESPN                                      │
              └───────────────────────┬──────────────────────────────┘
                                      │  ingest
                                      ▼
        ┌──────────────────────────────────────────────────────────┐
        │ POINT-IN-TIME FEATURE STORE            alpha/pit.py      │
        │                                        alpha/store.py    │
        │ source_observations: one row per observation, with FOUR   │
        │ separate timestamps —                                     │
        │   event_time   when the measured thing happened           │
        │   observed_at  when the source published it               │
        │   available_at when WE could first have used it  ◀── the  │
        │   ingested_at  when the row was written           only one│
        │                                                   a back- │
        │ AsOfView(t) contains ONLY available_at <= t.      test may│
        │ Nothing else can be read.                         compare │
        └───────────────────────┬──────────────────────────────────┘
                                │
                                ▼
        ┌──────────────────────────────────────────────────────────┐
        │ ALPHA FACTOR REGISTRY               alpha/registry.py     │
        │                                     alpha/factors.py     │
        │ 21 registered factors, each versioned, each declaring     │
        │ what it reads, each able to REFUSE with a typed reason.   │
        │ A factor with no data returns nothing — never zero.       │
        └───────────────────────┬──────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌───────────────┐      ┌────────────────┐      ┌──────────────────┐
│ MODEL A       │      │ MODEL B        │      │ MODEL C          │
│ Outcome       │─────▶│ Alpha          │─────▶│ Execution        │
│ alpha/models  │      │ alpha/models   │      │ alpha/execution  │
│               │      │                │      │                  │
│ OutcomeEstim- │      │ AlphaScore:    │      │ maker vs taker,  │
│ ate: p, low,  │      │ expected_move  │      │ P(fill), queue,  │
│ high, indep-  │      │ over a HORIZON │      │ partial fill,    │
│ endent prior  │      │ + components   │      │ adverse select.  │
└───────────────┘      └────────────────┘      └──────────────────┘
                                │
                                ▼
        ┌──────────────────────────────────────────────────────────┐
        │ STRATEGY                            alpha/strategy.py    │
        │ entry + EXIT policy · sizing with NO_TRADE                │
        │ SourceLagConvergence v0 (weather / daily numeric)         │
        └───────────────────────┬──────────────────────────────────┘
                                │
              ┌─────────────────┴──────────────────┐
              ▼                                    ▼
┌───────────────────────────┐        ┌────────────────────────────┐
│ EVENT-DRIVEN BACKTEST     │        │ SHADOW LEDGER              │
│ alpha/simulator.py        │        │ alpha/shadow.py            │
│ alpha/events.py           │        │ frozen, hashed, append-only│
└─────────────┬─────────────┘        └─────────────┬──────────────┘
              └──────────────────┬─────────────────┘
                                 ▼
        ┌──────────────────────────────────────────────────────────┐
        │ EVIDENCE                            alpha/evidence.py    │
        │ INSUFFICIENT_DATA · RESEARCH_ONLY · SHADOW_ONLY ·         │
        │ VERIFIED_OOS  — computed from what exists, never asserted │
        └──────────────────────────────────────────────────────────┘
```

---

## Point-in-time: four timestamps, and why none is redundant

Collapsing these into one "timestamp" is how look-ahead gets written by accident,
and the accident has **no symptom** — the backtest simply looks better.

A weather observation timestamped 14:00 that this system could not read until
14:40 belongs at 14:40 in the timeline. Sorting the event stream by `event_time`
lets a strategy act on it at 14:00. A CPI print published on the 12th and revised
on the 26th is *two* observations of one `event_time` with different
`available_at`; a decision on the 20th may see only the first. Revision handling
therefore needs no special case — which is precisely why the field exists rather
than a single timestamp plus a convention.

**An observation with no `available_at` is inadmissible.** Absent provenance is
not evidence of freshness. `AsOfView.build` filters at construction, so an
inadmissible observation is not in the object at all — reaching it requires
deliberately going around the view rather than merely forgetting a filter.

---

## The factors

21 registered, across the families the research plan names. Every one declares a
version, its inputs, and a typed refusal reason.

| Family | Factors |
|---|---|
| Source divergence | `source_divergence`, `source_divergence_conservative` |
| Source lag | `source_update_lag_seconds`, `source_change_magnitude`, `market_reaction_to_source`, `source_freshness_ok` |
| Threshold distance | `threshold_distance`, `threshold_distance_z` |
| Forecast dispersion | `forecast_dispersion` |
| Microstructure | `book_spread`, `book_imbalance`, `book_slope` |
| Maker/taker economics | `taker_edge`, `maker_proposed_edge`, `maker_taker_fee_difference` |
| Time to resolution | `hours_to_resolution` |
| Price bucket | `price_bucket` |
| Cross-venue | `cross_venue_divergence` |
| Sibling coherence | `sibling_coherence_error` |
| Catalyst | `hours_to_next_catalyst` |
| Liquidity / capacity | `executable_capacity_usd` |

### Refusals that matter

- **`threshold_distance_z` refuses without a *measured* error scale.** Dividing
  by an assumed scale produces a number that looks comparable across markets and
  is not, and a z of 2.0 carries an implied tail probability a reader will use
  even when the docstring says not to. The scale must come from the source's own
  measured error history — the standard the CPI model already holds itself to.
  Even with a scale, `normality_validated` is a label rather than an assumption.
- **`cross_venue_divergence` refuses anything below strict verified
  equivalence.** Two contracts that read alike and settle differently produce a
  divergence that is a *rule* difference; trading it means taking the short side
  of a settlement definition nobody read.
- **`forecast_dispersion` counts declared mirrors once.** A vendor republishing
  NWS is not a second opinion about the weather. Ten pulls of one gridpoint are
  one source.
- **`sibling_coherence_error` requires a declared exhaustive set.** Assuming
  exhaustiveness from the shape of the data turns a partial board into a fake
  incoherence.
- **`hours_to_next_catalyst` refuses with no schedule.** "No catalyst known" and
  "no catalyst for a long time" are opposite research conditions.

### Why refusal, not zero

A factor returning `0.0` when it has no data is indistinguishable, downstream,
from one that measured zero. A strategy fitted on that mixture has learned the
shape of the missing data, and nothing in the output says so.
`FeatureVector.number()` returns `None`, never a default — a caller who wants
zero has to write that themselves, in the open.

---

## Execution: no fill on touch

**The rule:** a maker order is not filled because the quote touched its price.

This is the single most common way a prediction-market backtest becomes fiction.
A resting bid at 29c and a best bid that ticks down to 29c look identical in
candle data. They are not one event:

- the order is at the **back of the queue** at that price — everything already
  resting there trades first;
- a touch with **no traded volume** is a quote that moved, not a trade;
- the touches that do fill are **adversely selected** — an order fills fastest
  exactly when the market is moving through it.

`ConservativeFillModel` therefore requires traded volume through the level, and
fills only the share a queue position could have captured. Without queue data it
assumes `blind_queue_share = 0.25` — not 1.0, because assuming the whole print
says our order was alone at the price, which is the *least* likely arrangement
rather than the neutral one. Fractional fills round **down**.

Every trade carries a `FillConfidence`, and a run's confidence is the **worst**
of its trades, never an average — the same rule the existing backtest applies to
`data_quality`.

| Confidence | Meaning |
|---|---|
| `TAKER_FROM_LADDER` | Lifted from an observed ask ladder. The certain case. |
| `TRADE_CONFIRMED` | Volume through the level, with a queue estimate. |
| `TRADE_CONFIRMED_QUEUE_UNKNOWN` | Volume through, queue share assumed. |
| `CROSSED_NO_VOLUME` | Book crossed, no print. Off by default. |
| `TOUCH_ONLY` | Never a fill. |

**Exits sell into the bid**, net of the closing fee. A convergence exit marked at
the ask has not modelled an exit; it has recorded a wish. A position with no bid
returns no exit price at all rather than being marked at the last ask — which
would convert every illiquid loser into a clean close.

---

## Strategy v0: Source-Lag Convergence

**Scope, deliberately narrow:** weather threshold contracts, where the
settlement source is the NWS observation the model already reads, and daily
numeric official-source contracts where the mapping is equally exact. Not
football. Not every Kalshi category — a strategy spanning categories with
different settlement mappings cannot attribute a failure to the thesis rather
than to one category's rules.

**The thesis, which the strategy does not assume:** a trusted source changes and
the market does not fully incorporate it immediately.

The chain, each step conservative:

1. start from the **conservative bound** of the outcome interval, never the mean;
2. subtract the **executable** entry price, never the midpoint;
3. subtract the venue's real fee at that size, including Kalshi's cent ceiling;
4. require the source to be **fresh** — a persistent gap is evidence *against*
   the thesis, because a market that has had a day to absorb a forecast and still
   disagrees probably knows something the model does not;
5. require the alpha model to expect a move;
6. require capacity — an edge with no depth behind it is a screenshot.

The alpha model is **deterministic and says so**. `absorption_assumption = 0.35`
is a **stated prior, not a measurement**, and it is the number that must be
replaced by a fitted one before any claim about profitability is defensible.

### Exits

`SettlementMode` holds to resolution — the honest control, and a statement about
the outcome model alone. `ConvergenceMode` exists so `29c entry → 43c exit` can be
evaluated before the binary event resolves, and checks in this order:

1. **signal invalidated** — the source reversed. Not a decayed signal, a *wrong*
   one, and it must not be counted as convergence;
2. **converged** — most of the gap closed;
3. **alpha decayed**;
4. **near settlement** — riding into resolution silently converts a convergence
   position into an outcome bet;
5. **time stop**; 6. **risk stop**.

The distribution over exit triggers is one of the more informative outputs:
mostly `MAX_HOLD` means it never converged; mostly `SIGNAL_INVALIDATED` means it
was reacting to noise.

---

## Sizing, and NO_TRADE

Not Kelly. Kelly is optimal against a probability you *know*; against one you
estimated it is a leverage multiplier applied to model error, and no estimate
here has been shown to beat the market price.

Defaults are small on purpose: `$25` base notional, `$50` per market, `$200` per
category, `$500` per strategy, `$250` per day, `$75` per correlated group, and at
most 10% of observed profitable depth. `SizeDecision` reports which cap actually
bound — over a run that answers "is this small because the edge is small, or
because one limit is doing all the work?"

`NO_TRADE(reason)` is a first-class return with eleven typed reasons, so refusals
can be counted. The most informative refusal wins: signal checks run before
portfolio checks, so a run's refusal histogram describes the strategy rather than
the order markets were visited in.

---

## Shadow mode

A shadow decision is the only evidence this system can produce that is neither a
backtest nor a live trade: a claim published **before** the answer exists.

Its value rests on one property — **a published decision is never rewritten.**
Outcomes are appended to `shadow_outcomes`; the decision gains no result field,
because one that can be updated will be: by a backfilling migration, a rerun that
"corrects" a stale profile, a fix that recomputes an old signal under the current
factor version. Each is individually reasonable; each destroys what the record
was for.

`content_hash` is a digest over a **fixed field list**, so adding a field later
cannot change the hash of records already written. `audit_decisions()` recomputes
it and **reports** drift — never repairs it, because repairing deletes the only
evidence that anything moved.

Frozen on every decision: decision timestamp, information cutoff, source values
with all four timestamps, the full feature vector including refusals, model
probability and interval, alpha score, executable market state, proposed order
and size, rationale, and every version identifier.

**Refusals are recorded too.** A strategy that declines 400 times and trades 3 is
a different object from one that trades 3 out of 3.

No order is placed. There is no execution service and no venue credential in this
package.

---

## Metrics

Probability metrics stay (Brier, log loss, calibration) — a model that cannot
beat the market price adds no information. Strategy metrics add: gross and net
P&L, fees, deployed capital, return on deployed, max drawdown, turnover, trade
count, average holding time, fill rate, maker vs taker P&L, expected vs realised
edge, capacity, profit factor, worst trade and the 95th-percentile loss.

**No annualised Sharpe.** It needs a return series with meaningful periodicity
and enough observations for the second moment to mean anything; positions here
are irregular, overlapping and few. A per-bet mean-over-stdev is reported instead
— unannualised, named so it cannot be mistaken for the other thing, and `None`
below ten observations.

### Segmentation

Every run segments by category, liquidity, exit trigger, fill confidence, price
bucket, time to expiry, spread bucket, liquidity bucket, source-divergence
bucket, threshold-distance bucket and signal-strength bucket. The goal is to find
whether a narrow sub-strategy holds the alpha even when the aggregate does not —
and equally, whether a positive aggregate is one bucket carrying eleven that are
not. Thin buckets are marked `"thin": true` rather than dropped: a bucket with
two trades and a spectacular ROI is the most misleading cell in the table, and
hiding it would lose the fact that the strategy fired there.

---

## Evidence status

| Status | Meaning |
|---|---|
| `INSUFFICIENT_DATA` | Not enough observations to say anything, including "it does not work". |
| `RESEARCH_ONLY` | Synthetic/fixture data, or a period used for development. Machinery works; proves nothing about money. |
| `SHADOW_ONLY` | Decisions frozen before outcomes were known. Real evidence, no capital at risk. |
| `VERIFIED_OOS` | Untouched held-out period, enough settled decisions. **The only status that licenses a profit claim.** |

Computed from what is true, never asserted. Synthetic data caps at
`RESEARCH_ONLY` regardless of volume — a million fixture trades are a million
facts about the fixture. The status travels *with* the numbers in
`ResearchRecord`, so there is no route by which an ROI reaches a display without
the label that says what it is worth.

---

## Where this stands today

`pmvl alpha-readiness` computes it. As of this writing:

- **0** point-in-time source observations recorded (the tables exist; collection
  has not started);
- **0** live recommendation snapshots — all 450 in the published artefact are
  `provenance='demo'`, and every live backtest run has `n_settled=0`;
- 3,898 order-book captures and 13,738 trade prints exist, which is real and is
  the input a fill simulation needs;
- 21 factors registered, 177 alpha tests passing on fixtures.

So: **NO VERIFIED ECONOMIC BACKTEST YET**, and the correct next move is
collection, not analysis.

### What must happen next, in order

1. **Record source observations on every ingest.** NWS gridpoint forecasts and
   Cleveland Fed nowcasts into `source_observations`, with `available_at` set to
   when the poll completed — *not* to the source's own publication time.
2. **Retain trade prints for the research universe** rather than pruning them to
   the display window.
3. **Run shadow mode continuously** so decisions accumulate frozen.
4. **Only then** fix split boundaries — before looking at the test period — and
   run the simulator.

Steps 1–3 are calendar time. There is no shortcut, and manufacturing one by
running the simulator over demo rows would produce exactly the artefact this
architecture was built to make impossible.
