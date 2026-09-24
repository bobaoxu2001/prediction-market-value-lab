# LIP maker shadow study: design `lip-1.0.0`

**The question.** Kalshi pays a Liquidity Incentive Program (LIP) reward for resting bids. For a bona fide two-sided maker (one that joins or improves the best bid), does that reward exceed the adverse selection on the fills the same resting orders would get? The test is run as a shadow simulation: the orders are paper orders inserted into recorded books, the reward is scored second by second, and fills come from the tape.

**How the verdict works.** Every point where the rule text, the fill model or the latency is ambiguous is computed twice:
- **LOW** takes the reading least favourable to the strategy. A positive verdict must survive LOW.
- **HIGH** takes the reading most favourable to the strategy. A negative verdict must survive HIGH.

Anything in between is `INSUFFICIENT_EVIDENCE`.

---

## 0. Gates before the pre-registration commit

No study data exists until all five gates pass. Operational output from the pre-flight and the dry run goes to `artifacts/forward/lip-dryrun/`, is never scored and is never pooled.

1. **Governing rule text.** The filing's term ended on 1 Aug 2026, so a later notice now governs. Fetch the current LIP notice from kalshi.com/regulatory/notices (this needs the user's OK for that web fetch) and diff it against §4. Every §4 choice that the governing text settles gets fixed in the pre-registration. The LOW/HIGH split then covers only what that text leaves open.
2. **Rate-limit costs.** Read docs.kalshi.com/getting_started/rate_limits for two numbers:
   - the token cost of a 100-ticker `/markets/orderbooks` call. If it is not about 1 token, §7's cadence plan fails and the pre-registration is rewritten, not patched.
   - the account's write limit. The simulator enforces it (§2).
3. **Global trades feed pre-flight.** Ten minutes, read-only, inside 12:30–01:50 UTC. Check that `/markets/trades` honours `min_ts`, see whether it accepts `limit` above 200, and measure the exchange-wide print rate.
4. **Actual request rates.** Count the collector's (`com.pmvl.collector`) actual requests per second for one day from its logs.
5. **Code in place.** All §9 code and tests merged. A synthetic replay day passes. A 30-minute recorder dry run meets its cadence and storage targets.

---

## 1. Universe

**Rule.** It is evaluated per program period, at the moment the period first appears in the recorded program list, using only program and series attributes. A program period is in the universe if all of the following hold:

- `incentive_type = liquidity`, `end_date > start_date`, and the market is open.
- **Pool rate** R / period hours ≥ **$2/h**.
- **Not excluded by E1–E3:**
  - **E1: period shorter than 4 h.** This removes 15-minute crypto/FX/metals/natgas programs, hourly temperature, the 90-minute in-play periods, live mention markets (KXNBAMENTION, KXTRUMPMENTION*) and hourly index up/down markets (KXINXHUD, KXNDQHUD).
  - **E2: in-play sports series**, matched by a frozen regex list (the `classify2.py` in-play pattern, copied verbatim into the pre-registration), whatever their duration.
  - **E3: hourly-weather series** (`^KXTEMP[A-Z]*H$`), whatever their duration.
- **Event sample:** `sha256("lip-1.0.0|" + event_ticker)[:8] / 2^32 < 0.20`. Sampling whole events keeps each state's gas ladder together and costs no extra requests.
- **Cap:** at most 300 concurrently live markets. If the cap binds, events are dropped in descending hash order, deterministically, and the drop is logged.

A sampled program period stays in for its whole life. It is never dropped for being quiet, one-sided, losing or illiquid. It counts with zero if it earns nothing.

**Why E1–E3.** The unit that fails in these markets is the **fill model**. Informed flow reprices within about a second (`weather-source-lag.md`; spot-driven 15-minute markets; points in in-play matches). In those regimes the makers ahead of us cancel before the informed taker arrives. A queue model driven by the recorded book at 1 Hz, reacting after roughly 615 ms (half a round trip of staleness plus a 410 ms order round trip), therefore puts our order behind size that was already gone. Fills would be undercounted exactly where they are most toxic. The model is known to flatter there, so those markets are out, not merely downweighted.

- KXTEMPMIAH, the largest single pool, also fails the pivotality rule (its YES side was empty in every recording).
- **What the verdict does not cover:** E1–E3 hold **27.6%** of the live pool $/h, and non-fast programs under $2/h hold another 27.3%. The verdict covers the remaining **45%**.

**Expected size.** Computed from the saved program history plus the snapshot, restricted to the recording window (§7):

| | value |
|---|---|
| eligible universe inside the window, unsampled | about 933 live markets p50, $4,921/h of pool |
| sampled at 20%, live markets | p10 127, **p50 173**, p90 209, max 250 |
| sampled events / series | about 19 / about 17 at p50 |
| sampled pool | about $902/h p50 |
| share of the eligible universe's daily dollars inside the window | 68.7% |
| mix by $/h in window | daily energy/commodity ladders 59%, entertainment 19%, politics 8.5%, mentions 4.6%, sports futures 3.8%, weather/climate 2.6% |
| program shape | T = 1,000 for 7,464 of 7,486 programs; period p50 16.9 h; pool $100/period (p10–p90) |

**No selection on outcomes.** The inputs are pool, duration, a series regex, T and a hash of the event ticker under a committed salt. No book, volume, spread, price, fill or settlement enters the rule. The regex list and the salt are in the pre-registration.

---

## 2. Quote policies: four declared, both sides always

| id | placement per side | size per side S |
|---|---|---|
| **J1** | join others' best bid | T |
| **I1** | improve by one grid step if the spread is ≥ 3 steps, else join | T |
| **J5** | join | T/5 |
| **I5** | improve if the spread is ≥ 3 steps, else join | T/5 |

**Common mechanics, identical for all four:**

- **Both sides.** A YES bid and a NO bid; the NO bid is the YES ask in YES coordinates.
- **Why ≥ 3 steps to improve.** Improving both sides of a 2-step spread would put p_YES + p_NO at $1, a self-cross. `min_spread` is 0.01 (the maker study used 0.02, which never quotes a 1-tick book).
- **Prices are set from others' orders only.** "Best bid" always means the best of other traders' recorded bids. We never price off our own paper order.
  - JOIN target = others' best.
  - IMPROVE target = others' best + 1 step, never ≥ others' best ask.
- **Reprice rule.** Reprice when the current price differs from the target. One exception for IMPROVE: when others join at our price we stay (they queue behind us). We never react to the opposite side approaching or to trades. The abuse clause makes "cancel when aggression approaches" out of scope.
- **Gate.**
  - Quote only after **10 consecutive** observed books satisfy all of: both sides reach T in the recorded book without us; status open; program live; now ≤ close_time − 30 min; now < 01:45 UTC.
  - Withdraw after **3 consecutive** failures.
  - This implements the out-of-scope decision in the strategy itself, not only in the scoring: we are never present when our order is what makes a side eligible.
- **Latency.**
  - A decision is taken at the `response_received_at` of a recorded book.
  - A new order is live at t + L.
  - A cancelled order stays **fillable until t + L** but **stops earning LIP credit at t**.
  - L_LOW = max(410 ms, the measured round trip of the request that triggered the decision). L_HIGH = 300 ms.
- **Write budget.** Every placement, cancel and decrease counts. A reprice is 2 writes. The budget is the documented account write limit (gate 2), or 10 writes/s if lower. Writes over budget queue FIFO, so under load the simulated maker is as slow as a real one. How often the budget binds is reported.
- **Replenishment.** The strategy learns of a fill at the first decision instant ≥ print time + L. The filled quantity is then re-posted as a **new child order at the back of the queue** at the current target, subject to the inventory cap. The parent keeps its place.
- **Inventory.**
  - Net position N = YES − NO, because Kalshi nets the two.
  - Resting YES ≤ min(S, S − N); resting NO ≤ min(S, S + N); |N| ≤ S.
  - Shrinking an order uses the decrease endpoint, which keeps priority. Nothing is ever flattened with a taker order.
- **Dead-man switch.** If no fresh book arrives for 10 s, cancel everything (effective at +L). Re-entry goes through the gate.
- **Window.** Quoting runs 12:30–01:45 UTC and everything is cancelled at 01:45. Recording runs to 01:50 so the last 300-second markouts exist.

---

## 3. Fill model

Each paper order carries a state: price p (in YES coordinates for tape matching), remaining q, queue ahead A, and its live time. Events are applied in exchange-time order. Block trades are excluded. Sizes are fixed-point to 0.01. There are two **fill scenarios**:

| | **HEAVY** | **LIGHT** |
|---|---|---|
| initial A on JOIN | min(level size in the last book before live, level size in the first book after) | max of the two |
| initial A on IMPROVE | 0 (a new price level) | 0 |
| relevant print at our level (aggressor on the other side) | consumes A, then q | same |
| print through our price | fills min(q, size); A → 0 | same |
| level shrinks by more than the prints explain | the excess is cancels **ahead** of us: A −= excess | the excess is cancels behind us; A = min(A, level size) only |
| level grows | growth queues behind us | same |
| recorded opposite side crosses our paper price (possible only when we sit above others' best) | fills min(q, crossing size) at p, dated at the **earlier** bracketing book | no fill (the counterparty might have been post-only) |
| tape-coverage gap (from the watermark log) | unexplained shrink at our level counts as prints | no fill |
| L | max(410 ms, measured round trip) | 300 ms |

`QUEUE_MODEL`'s static arithmetic, the at-level and through tests and the tape price convention (`MakerQuote.yes_price`, `is_relevant`, `at_level`, `through`) are reproduced and cross-tested against `maker_experiment.py`. `TOUCHED` is computed for display only and never used.

**Partial fills** reduce q in place. Credit in later seconds uses the reduced q (§4).

**Why this is not optimistic.**
- In this strategy fills are expected to lose, while unfilled resting time is paid. So *under*-counting fills is the flattering error, the reverse of the maker study.
- HEAVY therefore advances us through the queue as fast as the recorded book allows. It adds book-cross fills that never print on the tape, because in reality they would have traded against us. It infers fills through tape gaps.
- Prints inside the latency window never fill a new order. They do still fill a cancelled one.
- Neither scenario is assumed to be right. The verdict takes the worse of the two for a positive finding and the better of the two for a negative one (§6).

---

## 4. LIP credit per second

**Rule.** This follows grounding report 1: the help center (H) wherever it speaks, the filing (F) where H is silent, as confirmed by gate 1. The payout for one market's program period P:

- G = R · Σ_k v_k / (2 · N_total)
- N_total = ceil(end) − floor(start), in seconds.
- Apply the cap if one is set. Pay floor-to-the-cent if G ≥ $1.00, else 0. This is per market per period, not per account.
- G is allocated to study days in proportion to each day's Σv_k.
- Seconds outside the recording window are in N_total and earn 0. The payout is therefore exactly what a strategy quoting only in the window would earn.
- A program the list withdraws before its end pays 0 under LOW, and the observed-seconds payout with the original N_total under HIGH.

**Per second k, per market, per policy:**

1. **Observed?** Take b⁻, the last book at or before k, and b⁺, the first book at or after k+1. Book times are socket-window midpoints. If either is missing or b⁺ − b⁻ > 3 s, the second is UNOBSERVED and v_k = 0. Tape fills still apply during such a second, because the orders are resting.
2. **Open?** Status must be open at both bracketing status polls and the exchange active. Otherwise the second is excluded and v_k = 0.
3. **Target test, without our orders.** Both sides must reach T, walking whole levels from the best bid. A best bid at the highest valid price makes that side empty.
   - Under LOW this must hold both in the recorded book and in the counterfactual book of other traders. The counterfactual removes resting size that, under that second's fill scenario, would have traded against us.
   - A pivotal second is therefore 0 by construction.
4. **Insert our orders.** Build the book with our remaining q at our price and queue position. Walk whole levels until cumulative size ≥ T to get the qualifying set. Our size can push deeper levels out of that set.
5. **Reference and multiplier.**
   - Ref_H is the first level where cumulative size, ours included, is ≥ T/5. Ref_F is the best bid.
   - m = DF^N, where N is the number of grid steps from the price up to Ref. Bids at or above Ref get m = 1.
6. **Share.** Per side, share = Σ ours / Σ all qualifying scores. v_k = share_YES + share_NO, which is at most 2.

**Readings of the ambiguous points.** LOW and HIGH are computed on the same simulated path:

| point | LOW | HIGH |
|---|---|---|
| bracketing books | min(v(b⁻), v(b⁺)) | max |
| coverage of the second | earns only if live for all of [k, k+1), at the minimum q within it | covered fraction × time-averaged q |
| our credited size | q_eff = clamp(T − A_better − A_queue, 0, q) | whole remaining q (whole levels) |
| other traders at our level | recorded size + prints we absorbed (they would still be resting behind us) | recorded size |
| competitor response | depth that our insertion displaced from the qualifying set re-joins at the best level, behind us, in the same second | none |
| reference price | min over Ref_H and Ref_F of our share | max |
| tick on sub-cent grids | min share over the grid-step and the cent readings | max |
| ineligible traders' depth (MMA, non-US, designated LPs) | counted in the denominator | counted in the denominator |

**Why 1 Hz approximates Kalshi's snapshot.**
- Kalshi takes one snapshot at a uniformly random instant inside each second.
- The recorder fires on a 1.000 s grid with ±250 ms seeded random phase jitter, so our sample instants neither alias with bots that update on second boundaries nor sit at a fixed phase.
- The LOW/HIGH bracket over b⁻ and b⁺ bounds the book at the snapshot instant whenever the book changes at most once between them. At 1 Hz on quiet LIP books that is nearly always.
- How often v(b⁻) ≠ v(b⁺) is reported as a diagnostic.

**Why this does not overstate the share.** The LOW column is conservative on every open point. The target test is run without our orders. Payouts use H's two-sided rule and exclusion factor (the filing's rule, F, pays at least as much). The $1 threshold is applied after the exclusion factor and the cap. Unobserved and closed seconds stay in N_total.

The exploratory numbers in `artifacts/research/lip-1/` are **not reused**. They tested the target with our order included and hard-coded a 1c tick.

---

## 5. Valuation and metrics

**Primary metric:** unconditional **net $ per universe market-hour**, per policy.

- Numerator for one (market, period, study day): V = LIP allocation (§4) + Σ over fills q_f · (mid at t_f + 300 s − p_f) − maker fees.
- **Denominator:** every second in the window during which a sampled program period was live. This counts whether or not the recorder was healthy, the gate was open, we quoted or we filled.
- Consequences: unfilled resting time earns only its LIP credit; gated-off time earns 0; recorder outages can only lower the rate.

**Details of the fill term:**
- **Mid:** the mid of the recorded book (other traders only), so there is no self-marking. The calibration study found this mid within about 1c of settlement value.
- **300 s horizon.** It captures the post-fill drift the maker study saw at 30/60/300 s. Beyond 300 s, a calibrated mid is a martingale in expectation; the settlement secondary checks that assumption.
- **No mid available.** If no two-sided book exists in [t + 300, t + 600], LOW marks at the held side's best bid (0 if there is none) and HIGH at the last mid. The count is reported.
- **Maker fee.** For series with `fee_type = quadratic_with_maker_fees`: ceil_cent(0.0175 × multiplier × q × p × (1 − p)) per fill event (`kalshi_maker_fee`). An unknown fee type is charged under LOW and not under HIGH.
- **No capital charge in the primary.** A 5% annual charge on $1,000 is about $0.006/h, which is immaterial next to the rewards involved.

**Secondary metrics.** Descriptive only; no verdict rests on them.
1. **Hold to settlement:** LIP + Σ q_f (payout − p_f − fee) over markets settled by the final analysis + 14 days. Markets still unsettled are counted and left out.
2. **Decomposition:** LIP $/market-hour vs fill $/market-hour; fills per market-hour; markout per filled contract at 5, 60, 300 and 3,600 s.
3. **Break-even flow:** the LIP rate divided by the adverse selection per filled contract. For scale, at the maker study's 1.5–2c per contract, $0.38/h breaks even at about 22 filled contracts per hour and $4.17/h at about 240.
4. **Capital:** mean and peak collateral per market, Σ(resting q·p) + |inventory| at cost. For scale, one J1 market ties up about $1,000, so the full unsampled universe would need about $0.93M. Also $ per $1,000 of capital per hour.
5. **Credit diagnostics:** share of seconds counted; mean v_k while quoting; how often the payout falls under the $1 threshold; how often the gate was open.
6. **Displacement:** depth our order pushed out of the qualifying set, and the credit taken from others.
7. **Rule sensitivity:** whole-level vs truncated credit, H vs F, and the F payout where coverage allows.
8. **Operations:** write-budget binding; reprices per hour; latency distribution.

---

## 6. Statistics and decision

- **Cluster.** A study day runs from 12:30 UTC on date D to 01:50 UTC on D+1 and is labelled D.
- **Primary bootstrap.** Ratio estimator Σ V / Σ hours, resampling whole study days: 10,000 iterations, seed 20260924. This needs a new `ratio_cluster_bootstrap`, because the existing `_cluster_bootstrap` averages per-quote values.
- **Series bootstrap.** The same markets and series persist across days, so a second one-way bootstrap resamples **series**, pooling each series across all days.
- **Envelopes.** Two simulation scenarios (HEAVY, LIGHT) × two scoring readings (LOW, HIGH).
  - LOW value of a cluster = min(HEAVY scored LOW, LIGHT scored LOW).
  - HIGH value of a cluster = max(HEAVY scored HIGH, LIGHT scored HIGH).
  - By construction LOW ≤ HIGH in every cluster, so the verdicts are mutually exclusive. A test enforces this.
- **Multiplicity.** Four policies, Bonferroni: a family-wise 98.75% interval (per-policy two-sided α = 0.0125). Requiring both bootstraps is an intersection test and needs no further correction.
- **Minimum clusters:** 10 study days and 10 series (`MIN_CLUSTERS_FOR_INTERVAL`). Below that no interval is reported.
- **Sample.** **20 study days**, about 2,300 sampled market-hours per day per policy, about 46k in total. The rule is evaluated **once, at day 20**.
- **Interim reports.** Every report before day 20 is labelled `INTERIM_NOT_A_DECISION`. There is no early stop and no extension.
- **Days and exogenous stops.**
  - A short day is included with the coverage it reached; a missing day is a gap and is listed.
  - An exogenous stop (Kalshi changes the rule, eligible programs lapse for 7 days, or rate access is lost) ends the study. The table is then evaluated once at the count reached if it is at least 10, otherwise the verdict is `INSUFFICIENT_EVIDENCE`.
- **Versioning.** Any change to a declared parameter after day 1 bumps to `lip-1.1.0`, and versions never pool. A bug fix in the replay code is re-run on all days and listed in the results.

| verdict | condition at day 20 |
|---|---|
| `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY` | some policy's **LOW** primary 98.75% lower bound > 0 on **both** the study-day and the series bootstrap |
| `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION` | **every** policy's **HIGH** primary 95% upper bound < 0 on **both** bootstraps |
| `INSUFFICIENT_EVIDENCE` | neither, or fewer than 10 eligible study days or series |

---

## 7. Recording plan

**Window: 12:30–01:50 UTC, every day of the study.**
- It does not overlap the maker recorder (02:10–12:10) or the hourly crypto recorder (which ends at 07:03 today).
- It keeps 68.7% of the eligible universe's dollars, and the daily ladders are live in it (about 12:00–04:00).
- The window is part of the declared strategy. Under H a strategy that quotes only in this window earns exactly what is scored, so it is a scope limit, not a bias.
- It is not widened after the maker study ends.

| stream | cadence | req/s at p50 (p90) |
|---|---|---|
| books: sampled 173 (209) markets, 100 tickers per request, all requests of a tick fired concurrently | 1 Hz, jittered phase | 2 (3) |
| global tape `/markets/trades?min_ts=<watermark>`, cursor pages; every poll's watermark and page count logged | every 2 s + pages | 0.5 + prints/s ÷ 200 = 0.5–1.5 |
| status + metadata `/markets?tickers=` (status, close_time, `price_ranges`) | 30 s | 0.07 (0.1) |
| `/exchange/status` | 30 s | 0.03 |
| programs (`status=active` every 300 s, `upcoming` every 900 s) | | 0.004 |
| series fee type and multiplier | on first sight | ≈0 |
| **LIP total** | | **2.6–3.6 (4.6)**, own limiter at rate 5, burst 5 |

**Budget in the window.** LIP is capped at 5 and the collector at 9, for 14 at the caps. The maker recorder is not running. The 20 req/s basic tier holds even at the caps. Estimated actual use is 4–7 req/s.

**Fallback if gate 3 fails** (the global feed does not honour `min_ts`): sample fraction 0.10 (about 87 markets), books 1 req/s, per-ticker tape every 30 s at about 2.9 req/s, for about 4 req/s in total. The fallback is chosen before the pre-registration commit, never after.

**On any 429:** book cadence drops to 2 s for 10 minutes and the drop is logged. With S_max = 3 s, those seconds still count as observed. Gaps are never hidden.

**Files** go to `artifacts/forward/lip-daily/<D>/segment-HH/`:
- programs.jsonl (diffs, plus the full list hourly), universe.jsonl, snapshots.jsonl, trades.jsonl, tape_polls.jsonl, status.jsonl, markets.jsonl, requests.jsonl (per-minute counts and 429s);
- a `study.json` in the maker manifest shape, so `pmvl record settlements --out artifacts/forward/lip-settlements.jsonl` works unchanged;
- every line is stamped `lip-1.0.0`, never 1.1.0, and `source_timestamp` is kept.

**Storage.** Each book is stored as bids only, truncated at cumulative 2T, written only when it changes, with a 60 s keyframe. Files are gzipped hourly, with the hash chain continued across segments. Estimated at under 1 GB/day; an alarm fires at 5 GB/day.

**Schedule.** Installing these cron entries is a config change and needs the user's approval.

| time (UTC) | job |
|---|---|
| 12:20 | `pmvl record settlements` for LIP |
| 12:25 | `scripts/record_lip_day.sh`: mkdir lock, launches `pmvl record lip --until 01:50` |

- The code refuses to run between 01:50 and 12:30.
- The existing maker cron is untouched.
- **Daily guard:** each maker session must keep ≥ 1,200 snapshots with skew p50 ≤ 700 ms. There is no overlap, so this checks collector drift. On a breach, LIP pauses and the pause is logged.

---

## 8. What is not measured, and which way it biases the result

| omission | direction |
|---|---|
| Competitors react to our size or improvement (improve past us, add depth to defend their share). LOW models only displaced depth re-joining behind us. | **Flatters** (overstates our share) |
| Own-order impact on takers: a larger or better bid attracts more, and more informed, taker flow | **Flatters** (unobserved extra adverse fills) |
| Makers ahead of us cancelling faster than our ~615 ms reaction during fast moves (HEAVY bounds it only from the recorded book) | **Flatters**, mostly handled by E1–E3 |
| Tail latency beyond the measured round trip of the triggering request | **Flatters** slightly |
| Tape prints lost within covered windows | **Flatters** (fewer fills); tape_polls watermarks bound it |
| Mid mis-calibration of about 1c at the 300 s mark | Either way; the settlement secondary checks it |
| Kalshi's actual scoring code, abuse determinations, MMA/DLP exclusion from the target test | Either way; revocation only reduces |
| Post-only rejection of book-cross fills (LIGHT drops them, HEAVY keeps them) | Bracketed |
| Unobserved seconds, closed seconds, withdrawn programs, truncated final periods | **Understates** (conservative by design) |
| Capital cost and interest earned on collateral | ≈0 |
| E1–E3 and the $2/h floor (55% of pool dollars); time-of-day window | Scope, not bias: the verdict does not extend to them |

---

## 9. Implementation plan

**Isolation.** Nothing the maker recorder imports at runtime is modified: `recording/recorder.py`, `schema.py`, `writer.py`, `providers/kalshi.py`, `HttpClient`, `maker_experiment.py`, `maker_study.py`, `scripts/record_maker_day.sh`.

**Reused by import:**
- `AppendOnlyLog`, `verify_log`
- `KalshiProvider.get_orderbooks_synchronized` (with an injected client), `parse_orderbook`
- `kalshi_maker_fee`, `ceil_cent`
- `maker_study._interval`, `MIN_CLUSTERS_FOR_INTERVAL`
- `replay_cli._load_payouts`, `record settlements`

**New package** `packages/market-normalization/src/pmvl_markets/lip/`:
- `__init__.py`: `LIP_VERSION = "lip-1.0.0"`, `LIP_ANALYSIS_VERSION`.
- `programs.py`: `ProgramPeriod` (R from centi-cents, T from fp, `n_total_seconds` with floor/ceil); `is_excluded` (E1–E3, frozen regexes); `in_sample(event, salt, f)`; `SAMPLE_SALT`, `SAMPLE_FRACTION = 0.20`, `MARKET_CAP = 300`.
- `grid.py`: grid steps from `price_ranges`; `steps_between(price, ref, reading)`.
- `scoring.py`: `qualifying_set`, `reference_price(reading)`, `multiplier`, `counterfactual_book`, `score_second(b_minus, b_plus, our_orders, program, reading) -> SecondCredit` (v, eligible, pivotal, displaced), `period_payout`, `allocate_to_days`.
- `queue.py`: `OrderState`, `FillScenario` (HEAVY/LIGHT), `apply_print`, `apply_book`, `apply_tape_gap`.
- `policy.py`: `LipPolicy` and the four declared instances (J1, I1, J5, I5), plus the gate, reprice, inventory and write-budget rules.
- `simulator.py`: `simulate(market_period, books, prints, status, tape_coverage, policy, scenario) -> SimPath` (order ledger, fill ledger, per-second resting state); `score_path(path, ..., reading)`.
- `valuation.py`: 300 s markout with fallbacks, fees, settlement secondary.
- `study.py`: `ratio_cluster_bootstrap` (by day and by series), envelope min/max, `LipStudyReport`, the verdict table, the `INTERIM_NOT_A_DECISION` label.
- `http.py`: client wrapper with `RateLimiter(5, burst=5)`, per-minute counters and a 429 log.
- `store.py`: compact serializer (bids-only, truncated, change-only plus keyframes), gzip segment rotation with chain continuity, reader.
- `recorder.py`: `LipRecorder` with concurrent 1 Hz book loop, 2 s global-tape loop with watermark log, 30 s status loop, program-list loop, window refusal.

**Also new or changed:**
- `services/worker/.../record_cli.py`: add `record lip` and `record lip-preflight`. These are new commands only; existing commands are untouched.
- `replay_cli.py`: add `replay lip-days` and `replay lip-settlement`.
- `scripts/record_lip_day.sh`.
- `docs/lip-preregistration.md`, committed before any study data.

**Tests aimed at the ways this could flatter the strategy** (`tests/test_lip_{scoring,queue,simulator,study,recorder}.py`):

*Scoring*
1. The help-center worked example: 20% × $100 × 8,000/10,000 = $16.
2. Scores across all traders sum to 2 on an eligible snapshot.
3. A best bid at 99c empties that side.
4. **Pivotal:** a side at T only because of our order gives v = 0. A book shaped like KXTEMPMIAH (YES side empty) gives 0 even with our 1,000-lot.
5. LOW ≤ HIGH for every second and every cluster (property test over random books and paths).
6. Truncated credit ≤ whole-level credit.
7. Our insertion strictly shrinks others' qualifying depth when it should.
8. An order live from k + 0.2 s earns nothing in second k under LOW.
9. Credit after a partial fill uses the reduced q.
10. Bracketing books more than 3 s apart make the second UNOBSERVED.
11. A closed second stays in N_total.
12. Period timestamps with microseconds use floor/ceil.
13. $0.99 pays $0; the payout is floored to the cent; the cap applies; day allocations sum to the payout.

*Fills and the simulator*

14. A print inside the latency window never fills a new order but does fill a cancelled one, and credit stops at the cancel send.
15. JOIN sits behind displayed size.
16. HEAVY's cancel-ahead depletion versus LIGHT's cap.
17. A through print fills min(q, size).
18. An IMPROVE order is filled by the opposite side coming down to its price with no print (HEAVY only).
19. Tape-gap fills (HEAVY only).
20. Block trades are ignored.
21. A replenished child order joins at the back.
22. Inventory never exceeds S and uses decrease, not cancel.
23. No quoting while the book without us is below T.
24. The gate's hysteresis (10 to enter, 3 to exit).
25. No self-cross on 2-step spreads.
26. The write budget delays reprices.
27. The dead-man cancel fires after a 10 s gap.
28. Stop at 30 minutes before close and at 01:45.
29. Markouts use the book without our orders, with the LOW fallback when no mid exists.
30. Unknown fee type is charged under LOW.
31. Tape matching agrees with `MakerQuote.at_level`, `through` and `is_relevant` on shared fixtures.

*Study*

32. A market-period with zero activity lowers the rate: it is counted.
33. Fewer than 10 clusters gives no interval.
34. The family α is 0.0125.
35. Before day 20 every report is `INTERIM_NOT_A_DECISION`.
36. An exogenous-stop scenario evaluates the table once, at the count reached.
37. **Canaries:**
    - a synthetic market where every fill drifts 2c against us and the pool is small must never reach `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY`;
    - a no-taker market with a large pool reaches it only through the LOW share.

*Recorder*

38. Sampling depends only on (salt, event_ticker) and is invariant to book, price and outcome fields.
39. No line is stamped 1.1.0, and `schema.STRATEGY_VERSION` is still 1.1.0.
40. The recorder refuses to run inside 01:50–12:30.
41. The limiter never exceeds 5 req/s.
42. The hash chain verifies across gzip segments.
43. The program-list diff catches late-listed and withdrawn programs.

Universe numbers come from two read-only scripts I ran over the saved program JSON (`lip.json` and `lip_history.json` in the scratchpad):
- /private/tmp/claude-501/-Users-xuao-Documents-2025-----AI-Projects-prediction-market-value-lab/6306567b-fa12-4743-9bb1-c4f3881d7852/scratchpad/lipstudy/design2.py
- /private/tmp/claude-501/-Users-xuao-Documents-2025-----AI-Projects-prediction-market-value-lab/6306567b-fa12-4743-9bb1-c4f3881d7852/scratchpad/lipstudy/design3.py

No repository file was changed and no Kalshi or Polymarket API was called.