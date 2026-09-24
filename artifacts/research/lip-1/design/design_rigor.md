# LIP maker quoting: design for a forward shadow study (`lip-1.0.0`)

**Status:** this is the design for `docs/lip-preregistration.md`. No LIP data has been recorded. The document and the scoring, simulator and study code must be committed before the pre-flight in section 7.

- Universe numbers come from 211,924 historical programs plus `lip.json`, swept over the 7 days to 2026-09-24 02:50 UTC. Scratch scripts: `/private/tmp/claude-501/-Users-xuao-Documents-2025-----AI-Projects-prediction-market-value-lab/6306567b-fa12-4743-9bb1-c4f3881d7852/scratchpad/lipstudy/design_numbers.py` and `fam.py`.
- Nothing in the repo was edited and no API was called.

## 0. Question and estimand

**Question.** Kalshi pays an incentive for resting bids. Does a bona fide two-sided resting quote on LIP markets earn more from that incentive than it loses to its own fills?

**Estimand.** For each declared policy p:

> θ_p = Σ (LIP payout after the $1 floor + fill PnL − fees) / Σ admitted market-hours

The sums run over every admitted market-second on the study dates.

- It is unconditional. Every admitted second counts, whether or not the policy was quoting, filled, or earned credit.
- An unfilled quote earns zero fill PnL.
- A second that is not counted, or not observed, earns zero LIP.

## 1. Universe

### Admission rule

The rule is frozen before any data and reads only the recorded program list (`programs.jsonl`). A program is eligible if all of these hold:

- `incentive_type = liquidity`
- `end − start ≥ 6 h`
- pool rate `period_reward/10⁴ ÷ duration_h ≥ $2.00/h`
- `end > start`
- its series does not match the fast-information list:
  - `15M$`
  - `^KXTEMP[A-Z]*H$`
  - the in-play match/game patterns in `lipstudy/classify2.py`, copied verbatim into the prereg

### Why the exclusions

The duration rule alone already removes every 15-minute, hourly-weather and in-play program. The pattern list also keeps out any future long program on those series. The reasons, all known before any data:

1. Information reaches these markets faster than a REST poller at about 410 ms plus a 1 s poll can react. `weather-source-lag.md` measured repricing within a minute of data that free feeds show 17 to 46 minutes later. 15-minute markets follow a live spot price.
2. KXTEMPMIAH fails the pivotality rule: its YES side was empty in every recorded snapshot.
3. These series have designated LPs, and their periods are shorter than 90 minutes. The $1 floor per period then needs a 5-10% share.

**Scope of any verdict.** The verdict applies to multi-hour programs paying at least $2/h. Intraday LIP would be a separate study.

### What the rule selects (7-day sweep)

| | p10 | p50 | p90 |
|---|---|---|---|
| Eligible live markets | 480 | 875 | 1,081 |
| Live series | | 70 | |
| Live families (see below) | 32 | 57 | 64 |
| Capped sample (5 per family) | 136 | 182 | 197 |

- Pay rate of eligible markets at p50: $4.5k/h, which is 48% of the $9.4k/h live across all programs.
- Over the week: 127 families, 153 series, 5,288 markets.
- Eligible markets live by hour: about 1,000 during 12:00-03:59 UTC and about 500 during 04:00-11:59 UTC. The gas ladders are live 12:00-04:00 UTC.

### Family, the second clustering unit

A family is the series ticker, with one mechanical fold: if the ticker ends in a US state or DC code and the remainder is itself a series that has ever had a program, the family is the remainder. For example, `KXAAAGASDMI` folds to `KXAAAGASD`. This groups markets that share one information source; the 50 state gas ladders all follow the national price.

### Admission and sample size

Admission is re-evaluated each time the program list is refreshed (every 300 s):

1. Rank eligible (market, program) pairs by `sha256("pmvl-lip-1.0.0" | market_ticker | program_id)`.
2. Admit in hash order while the family has fewer than 5 admitted markets and fewer than 200 are admitted in total.
3. An admitted market stays until its period ends, its market closes, or its program is withdrawn. It is never dropped for how it performs.

Each hourly segment therefore has at most 200 markets, 182 at p50. That is about 4,300 admitted market-hours a day and about 86,000 over 20 dates, per policy.

### How the rule avoids choosing on outcomes

- Admission is a function of program attributes and wall-clock time only. Its signature takes no book, trade, price or volume, and a test enforces this.
- The salt is a plain fixed string, so it cannot have been searched for.
- Hashing on `program_id` re-randomizes daily programs every day.
- Programs that were seen but not admitted are logged with the reason (rate, duration, pattern, cap).
- Nothing is dropped after the fact. Per-family results are descriptive only.

## 2. Quote policies

The family has four policies: 2 placements × 2 sizes. Every policy quotes both sides.

| Name | Placement | Size per side S |
|---|---|---|
| J100 | Join others' best bid | T (1,000 for T = 1,000) |
| I100 | Improve by one grid step when others' spread is at least 3 steps, otherwise join | T |
| J30 | Join | 0.3·T (300) |
| I30 | Improve under the same condition | 0.3·T |

"Others' best" is always taken from the recorded book, which does not contain our paper orders, so the policy never chases itself. Improving needs a spread of at least 3 steps; at 2 steps, improving both sides would make our own YES and NO bids cross.

**Quote rules:**
- **Never cross.** The price must be below others' best ask on that side, on the grid (`price_ranges`), and below the highest valid price; otherwise the policy joins.
- **Unquotable side.** If others have no bid on a side, that side is not quoted.
- **Decision points.** The policy decides at every fresh recorded book, using only data received up to that moment.
- **Latency.** L = 500 ms, about the measured 410 ms round trip plus a margin. A cancel lands at decision + L, and the old order can still fill until then. A new order rests from decision + L.
- **Reprice.** If the resting price differs from the target, the order is cancelled and replaced. The new order starts a new episode at the back of the queue.
- **Replenish.** When the remaining size is ≤ 0.5·S, cancel and replace to S at the back of the queue.
- **Inventory cap.** Net position is tracked as YES minus NO, because Kalshi nets the two sides. While |net| ≥ S, the side that would add to the position is cancelled. It is re-placed as a new episode once |net| < S.
- **Stop.** The policy stops quoting 300 s before the scheduled close, when the program period ends, and when no fresh book has arrived for 5 s (it cannot see the market).
- **Write budget.** Each policy is a separate hypothetical account with a token bucket of W write actions per second. W is the basic-tier write budget, verified from the docs before commit; 10/s is a placeholder. A cancel-replace costs 2 actions, and a queued action adds to the latency.
- **Bona fide by construction.** No rule cancels when aggressive orders approach. The only cancels are for reprice, inventory, the close buffer, and loss of data.

## 3. Fill model

**Eligible prints.** A print can fill our order only if all of these hold:

- the aggressor took the other side (`MakerQuote.is_relevant`);
- it is not a block trade;
- its `traded_at` falls inside the episode's live window, after correcting for the logged NTP clock-offset bound.

Prices are compared in YES coordinates (`yes_price`).

**Three admissible estimators.** Each gives a cumulative fill path F(t) ≤ S, so partial fills are continuous.

- **TRADED_THROUGH:** only prints strictly through our price. This is a lower bound on fills.
- **FIFO_STATIC:** the existing `QUEUE_MODEL` arithmetic applied cumulatively over time:
  - F = min(S, max(through, at_level + through − Q₀));
  - Q₀ = max(others' size at our price in the decision book, the same in the first book at or after live_from). Orders that arrived during our latency are ahead of us.
- **FIFO_DEPLETING:** Q(t) = min(Q(t⁻) − at-level prints, others' size at our price in each new book). This assumes every cancel at the level came from ahead of us, which gives the most fills of any FIFO reading.

**Crossing.** If a recorded book shows others' ask at or below our bid, we are filled at our price, for the smaller of our remaining size and the crossing size. The order stops resting for credit from that record on.

**`TOUCHED` is never a fill.** Its count is reported only so the gap is visible.

**Why this is not optimistic.**
- A fill needs a real opposite-side print after our order rests, beyond the queue ahead of it.
- Arrivals during our latency are put ahead of us, and crossings count as adverse fills.
- The sign of fill PnL is unknown in advance. Fewer fills flatter the strategy if fills lose and hurt it if they win. So every verdict must hold under all three estimators, which covers both directions.

## 4. LIP credit per second

The rule spec is Grounding Report 1. It is frozen at prereg.

**Books for second k.** B⁻ is the latest recorded book received at or before k; B⁺ is the first received at or after k+1. Each must be within S_max = 3 × cadence of the second (3 s at 1 Hz). Otherwise the second is UNOBSERVED.

**Conservative envelope (used for positive claims):**

1. **Is the second counted?** Only if:
   - the market is `open` on both bracketing status polls (unknown counts as not open);
   - the second is observed;
   - both sides reach T in both B⁻ and B⁺, walking whole levels of the recorded book without our orders (pivotality);
   - neither side's best bid is at the highest valid price.
2. **Is our order credited?** Only if it rests for all of [k, k+1).
   - Credited size is the remaining size at k+1.
   - Credited price is min(actual price, the policy's target computed on B⁻). A stale order that others' cancels have left alone above the book is not credited for the stale price.
3. **Share per side.** share_lo = M_min / (M_min + O_max), evaluated on B⁻ and on B⁺, keeping the smaller.
   - Readings: {whole-level credit, price-time q_eff = clamp(T − A, 0, r)} × {reference F = best bid, reference H = first level reaching T/5, our order included} × {grid-step ticks, cent ticks}.
   - A = size at strictly better prices plus our current queue ahead under the estimator in use.
   - M_min is our smallest score DF^N·credited size over those readings.
   - O_max is others' largest qualifying score over the same reference and tick readings, walked both with and without our order. Our insertion therefore never pushes anyone out in this envelope.
   - Because x/(x+y) rises in x and falls in y, share_lo is at or below every consistent reading.
4. v_k = share_lo(YES) + share_lo(NO), or 0 if the second is not counted.
5. **Accrual:** R/(2·N_total) × v_k, with N_total = ceil(end) − floor(start) whole seconds of the full period. This is the help-center formula including the exclusion factor. It is additive per second, so our payout over the seconds we recorded is computed directly; no ratio is extrapolated from samples.
6. **Period payout:** apply the per-account cap if set. Then zero if the accrual over the study window is below $1, else floor to the cent. This is done per (market, period, policy, estimator).
   - The study window is truncated at admission and at study end.
   - If the program is withdrawn, changes terms, or does not reach `closed` or `paid_out` with the same `period_reward` within 7 days after the end, the payout is $0.
   - Hour values carry the post-floor allocation.

**Generous envelope (used only for the negative claim):**
- max M, min O, latest book only;
- credit for the covered fraction of a second;
- UNOBSERVED seconds carry the last value forward; unknown status counts as open;
- the filing's one-sided counting and its no-exclusion payout, R·Σv / (N_total × the recorded mean of q_YES + q_NO).

Both envelopes keep the project rules: the target test on the book without us, and bona fide prices.

**Cadence.** Kalshi takes one snapshot per second at a random instant. We score every second from the two bracketing books, so the conservative envelope resolves any book change inside the second against us.

## 5. Valuation and metrics

### Primary, MARK_1H

LIP payout, plus fill PnL marked at the recorded mid one hour later, minus fees, plus a terminal exit charge.

- **Each fill** (size q, price p, side s): q × (mid_s(t_f + 3,600 s) − p) − maker fee. If the market closes first, use the settlement payout if it has settled, otherwise the last two-sided mid before close.
- **Why a mid mark.** Kalshi's mid is calibrated to about 1c (`calibration-results.md`), so under a martingale mid a later mark changes only variance, not expectation. The 1 h horizon still captures slower adverse selection than the maker study's 60 s.
- **Terminal exit charge** when a market's participation ends: the residual net position is exited by walking the recorded opposite bids at t_end + L, charged (walk proceeds − |n|·mid) minus the taker fee at the recorded fee multiplier.
- **Fees.** The maker fee applies where the recorded series `fee_type` charges it, `quadratic_with_maker_fees` at 0.0175 × the multiplier. An unknown fee type is charged at 0.0175.
- **Dating.** LIP is dated by the second; fills and the terminal charge by their own time.

### Second decision metric, SETTLE

The same LIP payout, with fills held to payout: q × (payout_s − p) − maker fee, and no exit charge. Markets not settled at final + 7 days are excluded from both numerator and denominator and are counted.

### Descriptive only (never decisive)

- LIP-only and fill-only per market-hour
- fills per market-hour
- markouts at 30, 60, 300 and 3,600 s (the 300 s one is comparable to the maker study)
- counted-second fraction, credited fraction, and the share distribution
- dollars lost to the $1 floor
- reprices and write-budget delays
- the fraction of the time the I-policies had to join
- gaps between the two envelopes and between the estimators, and the TOUCHED count
- per-family table
- capital: resting remaining × price + |net| × average cost, time-averaged. About $1,000 per market at S = 1,000, or about $180k across the book. Reported as $/day per $1,000; not charged.

## 6. Statistics and decision rule

### Units and intervals

- **Cells:** (UTC date d, family f), each holding Σ$ and Σ admitted market-hours per (policy, metric, estimator, envelope).
- **Estimator:** the ratio Σ$/Σh.
- **Bootstrap:** a two-way pigeonhole bootstrap (Owen 2007). Each draw resamples dates and families independently with replacement; cell weight = c_d·c_f. 10,000 iterations, seed 20260924, percentile intervals via `maker_study._interval`. This is conservative relative to one-way clustering and captures both daily shocks and persistent family effects.
- **Minimum clusters:** at least 10 dates and at least 10 families (`MIN_CLUSTERS_FOR_INTERVAL`). Below that, no interval is computed and the result is INSUFFICIENT_EVIDENCE.

### Multiplicity

- Positive claims: 4 policies × 2 metrics = 8. Bonferroni gives a two-sided 99.375% interval (z ≈ 2.73).
- Negative claim: every policy must be negative, so this is an intersection-union test at 95%.
- All three fill estimators are required for either claim. This is an intersection, so it adds no correction.

### Length and interims

- **Length:** 20 eligible dates or 28 calendar days from day 1, whichever comes first.
- **Eligible date:** at least 20 h recorded, and at least 90% of admitted market-seconds pass the tape gate.
- Every recorded date is analysed; failing hours are dropped by the outcome-blind gate (section 7) and listed.
- The analysis of the MARK_1H metric runs at the end. The verdict is issued at end + 7 days, after settlements and program payouts.
- **Interims:** scored only at dates 5, 10 and 15, labelled `INTERIM_NOT_A_DECISION`. There is no stopping on outcome in either direction and no extension past 28 days.
- **Minimum detectable effect:** reported after the fact as 3.57 × the two-way SE (80% power at the Bonferroni level). No pilot is scored to estimate it beforehand.

### Decision table (evaluated once)

| Verdict | Condition |
|---|---|
| **Precondition** (if it fails: `INSUFFICIENT_EVIDENCE`) | Final analysis; at least 10 dates and at least 10 families; one version (`lip-1.0.0`); tape gate passed on ≥ 90% of admitted market-seconds overall |
| `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY` | For some policy p and metric m ∈ {MARK_1H, SETTLE}, in the conservative envelope: the 99.375% lower bound > 0 under **each** of TRADED_THROUGH, FIFO_STATIC and FIFO_DEPLETING, **and** the point estimate stays > 0 under each estimator after removing the family with the largest absolute contribution |
| `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION` | For every policy, MARK_1H in the generous envelope: the 95% upper bound < 0 under each of the three estimators, and no positive condition holds |
| `INSUFFICIENT_EVIDENCE` | Anything else, including both conditions holding at once (the conflict is reported) |

### Rules against gaming

- Analysis code is pinned by commit.
- A bug fix after data exists reports both numbers, and the verdict uses the more conservative of the two.
- A policy, universe or scoring change after day 1 bumps the version. Data from different versions are never pooled.
- There are no subgroup verdicts.
- If the current regulatory notice, fetched later, differs from the frozen spec, both scorings are reported and the verdict uses the more conservative.

## 7. Recording plan and schedule

### Process

- A new module and process with its own version `lip-1.0.0`, its own serializer, and its own mkdir lock and launchd job.
- It does not touch `recorder.py`, `schema.py` or `STRATEGY_VERSION` 1.1.0.
- `CountingHttpClient` subclasses `HttpClient` inside the LIP module: rate 4/s, burst 4, per-minute counts and 429s logged. The shared HTTP code is not edited.
- Book requests fire concurrently each tick. Books, tape, status and programs run as separate loops.

### Request budget

| Stream | Cadence | Requests/s |
|---|---|---|
| Books: `/markets/orderbooks`, ≤ 100 tickers per request, ≤ 200 admitted markets | 1 Hz, 2 concurrent requests | 2.0 |
| Tape: global `/markets/trades?min_ts=watermark−5s` with cursor, deduplicated by `trade_id`, universe prints kept, every poll's watermark and page chain logged | every 2 s | 0.5-1.5 (depends on print rate and page limit) |
| Status and metadata: `/markets?tickers=` (status, close_time, price_ranges) plus `/exchange/status` | every 30 s | 0.1 |
| Programs: active every 300 s, upcoming every 900 s | | 0.004 |
| Series fee info, daily settlements | daily | < 0.01 |
| **LIP total** | | **2.6-3.6, capped at 4** |

**Combined load, estimated actual:**

| Process | req/s |
|---|---|
| Maker recorder | ≈ 2 |
| Collector | ≈ 1-3 |
| LIP recorder | ≤ 3.6 |
| **Total** | **≈ 6-9, against a 20/s budget** |

The limiter caps alone sum to 9 + 9 + 4 = 22 during maker hours, so the following protections are pre-registered:

- **Maker guard.** It runs daily at 12:30 UTC. It trips if any maker session recorded while LIP was running has fewer than 1,200 snapshots, or a skew p50 above 700 ms. Then LIP records only 12:15-02:05 UTC for the rest of the study, and the analysis of the MARK_1H metric is restricted to that window on all dates.
- **429 ladder.** On any 429, drop to a 2 s cadence for 10 minutes; if 429s pass 1% of an hour's requests, pause that hour. Every gap is logged.
- **Optional, user's decision.** Lowering the collector's cap. Nothing in this design changes any configuration.

### Pre-flight

A 60-minute read-only run outside maker hours, after the prereg commit. The crypto recorder has ended by then (07:03 UTC today). It measures:

- the exchange print rate, and whether the global feed honours `min_ts` and accepts `limit > 200`;
- the token cost of a 100-ticker batch;
- storage per hour.

The cadence is then fixed by a fallback ladder written in the prereg:
- the first of 1 s → 2 s → 5 s that keeps LIP at or below 4 token-equivalent requests per second;
- if the global tape does not work, per-ticker tape polling every 60 s with the universe cap cut to 100.

The result is committed as `cadence.json` before day 1. Pre-flight data is tagged SHAKEDOWN and never scored.

### Storage

- Bids only, truncated at cumulative 2T per side.
- A line per request with sent and received times; only changed legs are written, with a full keyframe every 60 s.
- Gzip hourly segments, with the hash chain continued across segments.
- Estimate about 0.2-1 GB/day, hard alarm at 5 GB/day. 28 days fit comfortably in the 275 GB free.
- Layout: `artifacts/forward/lip-daily/<date>/hour-HH/` holding `programs`, `universe`, `books`, `tape_polls`, `trades` and `status` JSONL files.

### Quality gate

`replay lip-quality` reads recorder logs only and imports no valuation code. Tape continuity failures, meaning a poll gap over 10 s or an incomplete cursor chain, drop those market-seconds from all metrics. The gate's output is committed before any scoring.

### Sequence

1. Commit the prereg, code and tests.
2. Pre-flight, then commit `cadence.json`.
3. Day 1 starts at the next 00:00 UTC, likely 2026-09-26 at the earliest. Recording runs 24/7, in parallel with the maker cron (02:10 to about 12:10 UTC).
4. Programs are currently scheduled to 2026-10-14. Dates with no admitted markets are not eligible, and running out of programs leads to the minimum-cluster rule.

## 8. Not measured, and which way each omission biases the result

| Omission | Bias |
|---|---|
| Competitors reacting to our size (the pool is fixed; others add size or improve past us) | Optimistic for LIP |
| Our order's effect on takers: extra fills from sellers who would trade at our better price but not at the real best | Optimistic, since the missing fills are likely informed |
| Kalshi might leave ineligible MM or DLP depth out of its target test, so fewer snapshots would count | Optimistic |
| Revocation under the abuse clause, program edits | Optimistic (partly mitigated by the `paid_out` check) |
| Mid not a martingale beyond 1 h | Optimistic for MARK_1H; SETTLE is the check |
| Actual write limit lower than W | Optimistic |
| Cost of capital, taxes and SSN withholding | Optimistic |
| REST polling plus 500 ms latency instead of WebSocket | Pessimistic: more adverse selection, fewer credited seconds |
| Bracketing books, conservative envelope, ineligible traders kept in the denominator | Pessimistic for positive claims |
| True queue priority | Bracketed by the estimators |
| Fast-information series excluded | Scope, not bias; the result does not carry over to them |
| Live-event seconds inside admitted markets (sports futures during games, mention markets) | Included; the tape charges them |

## 9. Implementation plan

### New files

All under `packages/market-normalization/src/pmvl_markets/lip/` unless a path says otherwise.

- `version.py`: the version constant.
- `programs.py`: ProgramSpec, N_total, pool $/s, lifecycle, family fold.
- `universe.py`: eligibility and admission state machine, programs-only input.
- `grid.py`: steps from `price_ranges`, grid-step and cent readings.
- `scoring.py`: side qualification, reference price (F and H), multiplier, q_eff, the share envelope, second value, period accrual with floor and cap.
- `fills.py`: episodes, `fill_path` for the three estimators, the crossing rule. Reuses `MakerQuote`'s yes_price, is_relevant, at_level and through.
- `simulator.py`: policy, per-market state machine, write budget, event-interval per-second ledger (vectorized, target under 1 h per date).
- `valuation.py`: MARK_1H, terminal exit by book walk, SETTLE, fees (reuses `pricing.fees`).
- `study.py`: date × family cells, pigeonhole bootstrap, decision table, interim labels, leave-one-family-out.
- `quality.py`: the outcome-blind gate.
- `recording/lip_recorder.py` and `recording/lip_log.py`: diff and keyframe serializer and reader, gzip rotation, chain continuity. Wraps `writer.AppendOnlyLog`.
- CLI:
  - `record lip`, `record lip-preflight`, `record lip-settlements`;
  - `replay lip-quality`, `replay lip-days`, `replay lip-study`.
- `scripts/record_lip.sh` (launchd, mkdir lock), `scripts/lip_maker_guard.py`, and `docs/lip-preregistration.md` committed first.

### Unchanged

- `recorder.py`, `schema.py`, `maker_experiment.py` and `maker_study.py` stay unchanged. Only pure helpers are imported.

### Tests that catch ways this could flatter the strategy

**Scoring**
- The help-center example: 20% × $100 × 8,000/10,000 = $16.
- A side of 900 others plus our 1,000 earns 0. A book whose YES side is always empty earns 0 in every second.
- Property test on random books: conservative envelope ≤ every reading ≤ generous envelope.
- Joining behind T or more at the best level gives 0 in the conservative envelope and more than 0 in the generous one.
- Others improving past us: the per-snapshot minimum over the F and H references binds.
- A stale order above the book is credited at the target price.

**Time**
- Placement at k+0.2 or cancel at k+0.9 earns no credit for second k.
- A book older than S_max gives UNOBSERVED: zero credit, but still counted in N_total and in the denominator.
- N_total uses floor(start) and ceil(end) with microsecond timestamps.
- Unknown status earns no credit.

**Fills**
- Prints inside [decision, decision + L) cannot fill the new order; the old order stays fillable until the cancel lands.
- An at-level print smaller than the queue ahead gives zero fill (touched is not filled).
- Fills are ordered TRADED_THROUGH ≤ FIFO_STATIC ≤ FIFO_DEPLETING.
- A crossing ask fills us at our price.
- The two I-policy bids never self-cross.
- The inventory cap cancels the side that would add to the position.
- A burst of more than W actions per second is queued and delayed.

**No look-ahead**
- Perturbing any book or print received after t leaves every decision at or before t unchanged.

**Payout**
- A $0.99 period accrual pays 0; a $1.005 accrual pays $1.00.
- A withdrawn or unpaid program pays 0.
- A series that charges maker fees is charged them; an unknown fee type is charged 0.0175.

**Selection**
- Admission is identical when book and trade files are removed or shuffled.
- The hash ranking and family fold are deterministic.
- A market that was never quoted still adds market-hours; dropping it changes θ.

**Study**
- Fewer than 10 dates or 10 families gives no interval and INSUFFICIENT_EVIDENCE.
- The pigeonhole bootstrap matches the analytic two-way variance on synthetic data.
- Every run before the final one returns `INTERIM_NOT_A_DECISION`.
- Each decision-table row is exercised, including a result that is positive under only one estimator, which must not decide.
- Mixed versions are refused.

**Isolation**
- `STRATEGY_VERSION == "1.1.0"`, and the sha256 of `recorder.py` and `schema.py` are unchanged from the prereg commit.
- LIP lines carry `lip-1.0.0`.
- `quality.py` cannot import `valuation` or `simulator`.
- Diffs plus keyframes round-trip to the full book, and the hash chain verifies across gzip rotation.

## 10. To close before the prereg commit (no data needed)

- Fetch the current Kalshi regulatory notice and diff it against the frozen spec. The filing's own term ended 2026-08-01, and the help center gives a program end of 2027-01-01.
- Set W and the batch token cost from `docs.kalshi.com/getting_started/rate_limits`. This task did not allow a web fetch, so these remain placeholders.
- Confirm the account holds no Market Maker Agreement and is not an IB or FCM customer. The US account is already confirmed.