# LIP maker shadow study `lip-1`: design

This is a paper/shadow study. No order is ever sent. The three allowed verdicts are `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY`, `INSUFFICIENT_EVIDENCE` and `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION`. Every report before a decision point is labelled `INTERIM_NOT_A_DECISION`.

## 0. Summary of the design

| Choice | Decision |
|---|---|
| Question | Do bona fide two-sided quotes on Kalshi LIP markets earn more LIP reward than their fills lose to adverse selection? The quotes either join the best bid or improve it by one tick. The answer is measured unconditionally, per eligible market-hour. |
| Universe | Liquidity programs at least 2 h long that pay at least $2/h. Live-event and real-time-observation series are removed. At most 2 markets per event, 8 per series and 200 in total. Replaying the rule on 7 days of history gives a median of **149 markets, 87 events, 74 series and $711/h of pools per session**. |
| Window | **12:30-01:30 UTC** daily, as 13 sessions of 60 minutes. It never overlaps the maker cron (02:10 to about 12:25 UTC). |
| Cadence | Every universe book is read **once per second**, in 2 concurrent batched requests of up to 100 tickers. The global tape is polled every 5 s. |
| Policies | Four: {JOIN, IMPROVE-1} × {size T, size T/5}. Both sides are always quoted. Net inventory is capped at ±size. |
| Fills | QUEUE_MODEL, applied cumulatively. We join at the back of the displayed queue. Placing and cancelling each take 410 ms. A book that crosses our price fills us. |
| LIP credit | Help-center rule. The target test uses the recorded book without our orders. The reference price is the per-second minimum of the filing and help-center rules, and whole levels qualify. Each second takes the lowest score among the books that bracket it. The $1 floor applies per market-day. |
| Primary metric | Unconditional $ per eligible market-hour = LIP payout + fill PnL marked to mid − maker fees. |
| Statistics | Date-cluster bootstrap with a series-cluster check, 10,000 draws. Bonferroni over the 4 policies. Two looks, at 10 and 20 valid days (α 0.01 and 0.04). Robustness gates in both directions. |
| First decision | Look 1 falls after the 10th valid day: about 2026-10-04 01:30 UTC if day 1 starts tonight. |

## 1. Universe

**The rule is fixed in the pre-registration.** It is evaluated at each session start using only the program list and market metadata. The selection function has no book or trade argument.

1. **Program.** `incentive_type = liquidity` and active at session start. The period is at least 7,200 s. The pool rate (`period_reward` / 10⁴ divided by period hours) is at least $2/h.
2. **Market.** Status is `open`. `close_time` is at least 60 minutes after the session ends. The price grid is the 1-cent linear grid. Sub-cent grids are rejected, which removes the tick ambiguity in the scoring rule.
3. **Series is not in a fast-information family:**
   - **F1, live-event resolution:** the ticker contains `MENTION`, or it ends in `GAME|GAMES|MATCH|SPREAD|TOTAL` and does not contain `SEASON`.
   - **F2, real-time observed measurements:** `^KX(HIGH|LOW|TEMP|RAIN|SNOW|WIND)`, `ERCOT`, `PJM`.
   - **F3, continuously traded underlyings:** `^KX(BTC|ETH|SOL|XRP|DOGE|CRYPTO|EURUSD|USDJPY|GBPUSD|INX|NASDAQ|WTI|BRENT|GOLD|SILVER|NATGAS|COPPER|PLAT|PALL)`. No program of 2 h or more matches today; the rule stays so a new one cannot slip in.
   - The 2 h minimum already removes every 15-minute, hourly-weather and in-play table-tennis period.
4. **Sampling.** Order the candidates by sha256(`study_date|ticker`). Keep at most 2 per event, then at most 8 per series, then at most 200 in total.

**Why these are excluded.** In these markets the shadow method is structurally optimistic:
- When fast makers cancel, a stale resting quote gets hit. The recorded tape cannot show a fill that only happens because our quote was there.
- The project's own evidence points the same way: the weather-source-lag study, the pivotality failure on KXTEMPMIAH, and legs that roll every 15 minutes, which one leg resolution per session cannot follow.
- Over the last 7 days these families were about **10% of long-program dollars** (F1 6.6%, F2 3.4%).
- The verdict does not extend to them.

**Size, from replaying the rule on 7 days of history in this window:**

| | p10 | p50 | p90 |
|---|---|---|---|
| Markets | 120 | 149 | 164 |
| Events | 65 | 87 | 98 |
| Series | 52 | 74 | 82 |
| Pool per session-hour | $640 | $711 | $889 |

- The mean pool is $5.19/h per market.
- T is 1,000 for 99% of markets; the rest use 500 or 2,500.
- The window holds 68% of long-program dollars.

**How selection avoids choosing on outcomes:**
- Every candidate is written to `universe.jsonl` as accepted or rejected, with the reason.
- The set is frozen before the session's first book is read.
- Nothing is removed afterwards.
- The only time a book affects participation is the policy's session gate (section 2). Gated-out hours stay in the denominator as zeros.

## 2. Quote policies

| id | Placement | Size per side (S) |
|---|---|---|
| J-T | join the best bid | T |
| J-T5 | join the best bid | T/5 |
| I-T | improve by 1 tick | T |
| I-T5 | improve by 1 tick | T/5 |

T/5 (200 when T is 1,000) is the smallest size at which an improving order sets the help-center reference price on its own.

**Rules common to all four:**

- **Both sides.** A YES bid and a NO bid (the NO bid is a YES ask at 1 − p).
- **Session gate.** A market is quoted for the hour only if its first recorded book of the session is open and has at least T on both sides, excluding our orders. The gate is never re-checked inside the hour, because a reactive gate would amount to pulling quotes.
- **Price at each recorded book (1 Hz).** Our orders are excluded when reading the book.
  - JOIN quotes at the best bid.
  - IMPROVE quotes one tick above the best bid if the spread is at least 3 ticks. Otherwise it joins, because at 2 ticks two improved sides would lock.
  - A quote is never placed at or through the opposite best.
  - `min_spread` is set to 0.01. The `QuotePolicy` default of 0.02 would never quote a 1-tick book.
- **Reprice.** Whenever the target price differs from the resting price, in either direction, the order is repriced. This includes the case where our join level empties.
  - The cancel and the new order both take effect 410 ms after the decision.
  - The old order can be filled until then.
  - Queue priority is never carried over to the new order.
- **Replenish.** At the next decision after a fill, the side is topped up to its allowed size. The top-up is a new child order at the back of the queue.
- **Inventory.** Net position N = YES − NO (Kalshi nets the two).
  - Allowed YES size = clamp(S − N, 0, S); allowed NO size = clamp(S + N, 0, S).
  - This keeps |N| ≤ S.
- **Stop.** All quotes are cancelled when the program ends, the market is not open, the exchange is inactive, or the session ends. The cancel takes effect 410 ms later. Inventory is flat at 01:30 UTC.
- **Excluded by construction.** Cancelling when aggressive orders approach, completing a starved side, and deliberately one-sided quoting.

## 3. Fill model

**The unit is an episode.** Each order is a `MakerQuote` episode: it goes live 410 ms after the decision and ends 410 ms after its cancel decision, or when it is fully filled.

- **Queue.**
  - JOIN: `queue_ahead` is the whole displayed size at our price in the decision book, plus the remaining size of any earlier child order of ours at that level.
  - IMPROVE: `queue_ahead` is 0.
  - The queue is static. Cancellations ahead of us never move us forward.
- **Prints.** Only prints where the aggressor took the other side count; block trades are excluded; the print must fall inside the episode.
  - New `fill_path` gives the fill over time: filled(t) = min(S, max(B(t), A(t) + B(t) − Q)).
  - A is volume at our price, B is volume through it, and Q is the queue ahead.
  - This is the existing QUEUE_MODEL formula, applied cumulatively.
  - Partial fills are points on the path. The remaining size drives LIP credit.
- **Cross fills.** If a recorded book shows the opposite side at or through our price while we rest, our remaining size fills, up to the crossing size, at our price at that book's time.
- **TOUCHED is never used.**

**Why the model is not optimistic:**
- We join at the back of the queue and get no credit for cancellations ahead of us.
- Nothing fills during the 410 ms placement latency.
- Everything fills until the cancel lands.
- Through-volume fills only what actually traded.
- Cross fills are added.

**Where it could still flatter the strategy, and how that is bracketed:**
- **The static queue undercounts JOIN fills.** Gate A1 (`FRONT_OF_QUEUE`) sets Q = 0.
- **Stale-quote hits are invisible on the tape.** When others cancel without trading, the tape shows nothing. Gate A2 (`ORPHAN_FILL`) covers this: when our order is observed more than its `improve_ticks` above the best other bid, its whole remaining size is treated as filled at our price.
- **The lower bracket is B1 (`TRADED_THROUGH`):** fill = min(S, B).

**Reuse check.** For the same `MakerQuote`, the last point of `fill_path` must equal `evaluate_quote(...).filled_size` under QUEUE_MODEL and under TRADED_THROUGH.

## 4. LIP credit

**Inputs per program.**
- R = `period_reward` / 10⁴.
- N_total = ceil(end) − floor(start), in seconds.
- Each second is worth R / N_total.

**For each whole second k in the program period that falls inside a session:**

1. **Order state.** Our order counts only if it rested for all of [k, k+1). Its size is the remaining size at the end of second k.
2. **Books for second k.** Take every book whose socket window intersects [k, k+1), plus the last book received at or before k and the first book sent at or after k+1.
   - If that bracket spans more than 3.5 s, the second is UNOBSERVED and earns 0.
   - Otherwise v_k is the minimum score over those books, with our state inserted into each.
3. **Does the snapshot count?** This is decided on the recorded book, without our orders. The market must be open: the status polls on both sides of k say open, the exchange is active, and k is before `close_time`. Both sides must reach T walking whole levels, and each best bid must be below the top of the grid. Otherwise the snapshot scores 0 for everyone. This is the pivotality rule.
4. **Qualifying set, with our orders inserted.** Whole levels count from the best bid until cumulative size is at least T. Our size can push deeper levels out of the set. That is allowed: it is bona fide size at the best bid.
5. **Reference price and multiplier.**
   - Compute our credit under both reference rules and keep the smaller.
   - Filing: the reference is the best bid including ours.
   - Help center: the reference is the first level where cumulative size, including ours, reaches T/5.
   - Multiplier = DF^N, where N is the number of 1-cent steps below the reference.
6. **Share.** share_side = our score / all qualifying score on that side. v = share_YES + share_NO, between 0 and 2. Ineligible traders' depth (designated LPs, Market Maker Agreement holders) stays in the denominator.
7. **Accrual** in second k = (R / N_total) · v_k / 2.
8. **Payout.** Accrual is summed per (market, program period, study day). It is floored to the cent and paid only if at least $1, with a cap applied if one is set. It is then allocated back to sessions in proportion to their raw accrual. A daily floor pays no more than a period floor, so this is conservative.

**How 1 Hz approximates Kalshi's random snapshots.** Requests go out on every wall-clock second, so each second has one book read inside it (about 0.2 s in) and one on either side. Kalshi's uniformly random instant falls inside that bracket. Taking the minimum means coarse sampling can only cost us credit, never add it. Unobserved seconds are never extrapolated.

**Rule variants, used only as gates:**
- **A3 `TRUNCATED` credit.**
  - Our credited size is q_eff = clamp(T − A, 0, q), where q is our remaining size.
  - A is the size at better prices plus our queue ahead.
  - This zeroes JOIN behind a deep queue; IMPROVE is unaffected.
- **B2 `FILING` rule.**
  - The reference is the best bid, each side is scored on its own, and there is no two-sided exclusion.
  - The per-second payout is R · v_k / (N_total · q̄), where q̄ is the mean number of qualifying sides.
- **B3 period floor.** The $1 floor is applied once per program period, over all observed days.

Pivotality applies in every variant.

## 5. Valuation and metrics

**Value of each (market, session) unit, per policy:**
value = allocated LIP payout + trading cash flows + change in inventory mark − maker fees

- **Inventory mark.** Inventory is marked at the recorded mid at each session boundary and at window end. The calibration study found the mid accurate to about 1c. Each day starts flat, so days are independent. Kalshi's netting realises every YES + NO pair at 1 − p_YES − p_NO.
- **A4 `EXECUTABLE_EXIT` gate.** Inventory left at window end or at universe exit is sold by walking the recorded opposite side, paying the taker fee at the series multiplier.
- **Maker fee.** Fills in `quadratic_with_maker_fees` series pay the maker fee at the 0.0175 rate. If a series' fee type is unknown, the fee is charged.

**Primary metric.** E_π = Σ value / Σ eligible market-hours.
- Eligible time is every universe market-second in which the program is live and the market is open.
- That includes gated-out, unobserved, unfilled and inventory-suspended time.
- Unfilled time earns LIP only.

**Secondary metrics, reported with no verdict attached:**
- Decomposition into LIP, spread capture, markout and fees; LIP on its own.
- Fill rate and filled contracts per hour; markouts at 30/60/300 s per filled contract.
- Share of seconds that counted, and mean v.
- Capital committed (resting collateral plus inventory cost), and $ per $1,000 of capital per hour.
- Order actions per second needed (p50 and p99), against the account's write budget.
- Hold-to-settlement value for markets that have settled.
- Breakdown per family and per series; cadence sensitivity (using every 5th book).

**Rough scale.** In the exploratory snapshot (82 universe-like programs), a 1,000-lot joining both sides earned a median of $2.30/h. That used a looser rule than this one. The maker study's adverse selection was 1.5-2c per filled contract. Together these put break-even at roughly 100 filled contracts per market-hour.

## 6. Statistics

**Units and clusters.**
- The unit is (market, session), weighted by its eligible hours: about 1,940 market-hours per day per policy.
- The cluster is the study day D, running from 12:30 UTC on D to 01:30 UTC on D+1.
- A check clustering by series runs alongside.

**Bootstrap.**
- Whole clusters are resampled: 10,000 iterations, seed 20260924.
- The estimator is the ratio Σ value / Σ hours, through a new `_cluster_bootstrap_ratio` next to `_cluster_bootstrap`.
- Intervals are percentile intervals.
- Empty clusters are kept.
- No interval is produced from fewer than 10 clusters (`MIN_CLUSTERS_FOR_INTERVAL`).

**Valid day.** All four conditions must hold:
- at least 10 of the 13 sessions ran;
- at least 90% of eligible market-seconds were observed;
- the tape chain is complete for at least 99% of the window;
- the tape audit shows no unexplained mismatch.

Invalid days are listed, not replaced and not analysed. A market-session that overlaps an unrecoverable tape gap is removed from both numerator and denominator, and counted.

**Looks.**
- Look 1 comes at 10 valid days.
- Look 2 comes at 20 valid days, or on 2026-10-21 (28 days), whichever is first.
- α₁ = 0.01 and α₂ = 0.04.
- The positive test uses a two-sided confidence level of 1 − α_L/4: 99.75% at look 1 and 99% at look 2.
- The negative test uses 1 − α_L: 99% at look 1 and 96% at look 2.
- There is no other stop, and no extension beyond look 2.

**Decision table.** Every condition in a row must hold.

| Verdict | Condition |
|---|---|
| `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY` | For **some** policy: the date-level lower bound at the positive level is > 0; the series-level lower bound at the same level is > 0; and the point estimate is > 0 under each of A1 `FRONT_OF_QUEUE`, A2 `ORPHAN_FILL`, A3 `TRUNCATED` and A4 `EXECUTABLE_EXIT`. |
| `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION` | For **every** policy: the date-level upper bound at the negative level is < 0; the series-level upper bound is < 0; and the point estimate is < 0 under each of B1 `TRADED_THROUGH`, B2 `FILING` and B3 period floor. |
| `INTERIM_NOT_A_DECISION` | Any report before look 1, or look 1 when neither row above holds. |
| `INSUFFICIENT_EVIDENCE` | Look 2 when neither holds, or fewer than 10 valid days by the hard stop. |

**Precision.** Let σ_d be the day-to-day standard deviation of E. The half-width of the positive interval is about 3.02·σ_d/√10 ≈ 0.95·σ_d at look 1, and about 2.58·σ_d/√20 ≈ 0.58·σ_d at look 2.

**Versions.** `lip-1.0.0` (recorder) and `lip-sim-1.0.0` (simulator). Any rule change after day 1 bumps the version, and results under both versions are reported.

## 7. Recording plan

**Process and output.**
- A new process, `python -m pmvl_worker.lip_cli record-day`, with its own lock at `/tmp/pmvl-lip-day.lock.d`.
- Output goes to `artifacts/forward/lip-daily/<D>/session-HH/`.
- Each session writes `programs.jsonl`, `universe.jsonl`, `snapshots.jsonl`, `trades.jsonl`, `tape_polls.jsonl`, `status.jsonl`, `markets.jsonl` and `lanes.jsonl` (per-minute counts of requests, 429s and skipped ticks).
- Each day writes `study.json` (the pre-registration commit hash, versions and resolved config) and `health.json`.

**Request arithmetic.**

| Lane | Endpoint | Cadence | req/s |
|---|---|---|---|
| Books (2 lanes) | `/markets/orderbooks`, repeated `tickers`, up to 100 per request | every wall-clock second; a tick is skipped if the previous request is still in flight | 2.0 (1.0 if the universe has 100 markets or fewer) |
| Tape | global `/markets/trades?min_ts=<watermark − 1 s>&limit=L` with cursor | every 5 s; each poll covers about 6 s, so 6λ rows at λ = 100-300 prints/s | L = 1000: 0.2-0.4; L = 200: 0.6-1.8 |
| Status | `/markets?tickers=` (up to 100) and `/exchange/status` | every 30 s | 0.10 |
| Programs | `/incentive_programs?status=active&type=liquidity&limit=10000`, plus `upcoming` once per session | every 300 s | 0.004 |
| Series metadata and fee type | `/series/{s}` | once per series per day | < 0.002 |
| Tape audit | per-ticker `/markets/trades` for 2 hash-chosen universe tickers, compared with the global feed | every 300 s | 0.007 |
| **Total** | | | **2.3-2.5 with L = 1000; 2.7-3.9 with L = 200** |

- **Lane caps.** Books 2 × 1.25, tape 1.8, the rest 0.3: at most 4.6 req/s.
- **Burst.** The limiter on each injected client is replaced with `RateLimiter(rate, burst=1)` in the new code; `http.py` is not edited.
- **Separate clients.** Each lane has its own `KalshiProvider(client=HttpClient(...))`, because `last_request_window` is a single shared attribute and concurrent requests on one client would mis-stamp each other.
- **Other processes during the window.** The maker recorder is finished (the runner waits for its lock). The crypto recorder ends at 07:03 UTC today. The collector's limiter cap is 9 req/s, with estimated actual use of 1-3. Even with both at their caps, 4.6 + 9 = 13.6, under the 20 req/s basic tier.

**Daily schedule (UTC):**

| Time | What runs |
|---|---|
| 02:10 to about 12:25 | Maker cron, unchanged |
| 12:25 (20:25 local) | LIP cron fires. It waits while `/tmp/pmvl-maker-day.lock.d` exists or `pgrep -f "pmvl record sessions"` matches, and gives up at 13:30 with the day logged as skipped. |
| 12:30-01:30 | 13 sessions. None starts after 00:30; hard stop at 01:30. |
| 01:30-01:45 | Final tape drain, health report (counts only), settlement lookup into `artifacts/forward/lip-settlements.jsonl`, `verify_log`, then gzip of the closed sessions |
| 01:45-02:10 | Slack before the maker cron |

**Guards.**
- **429s.** If 429s exceed 1% of any 10-minute window, the book lanes drop to 0.5 Hz for 10 minutes and the change is logged. The seconds this loses become UNOBSERVED. The tape lane never degrades.
- **Maker protection.** If any maker session ever shows fewer than 1,200 snapshots, or a skew p50 above 700 ms, while an LIP process was alive, LIP stops for the rest of the study. This should be impossible, since the windows do not overlap.

**Today.**
- 12:30-12:45 UTC: a pre-flight writes `preflight.json`. Its conditional rules are fixed in the pre-registration:
  - L = 1000 if the global feed returns more than 200 rows to `limit=1000`; otherwise L = 200.
  - If `min_ts` is not honoured, the client stops paging at the first row older than the watermark.
  - If the p95 tape cost exceeds 1.8 req/s, or any 429 appears at the planned rates, the book lanes run at 0.5 Hz for the whole study (the staleness limit becomes 5.5 s).
  - The resolved config goes into `study.json` before session 1.
- Day 1 starts at 13:30 UTC with 12 sessions.

**Storage.**
- Bids only; asks are derived.
- Each side is truncated at the first level where cumulative size reaches 2T; the dropped level count and size are kept.
- One JSON line per batch request.
- Roughly 400 B per market-second, so about 2.8 GB/day raw and about 0.4 GB/day gzipped. That is about 8 GB over 20 days, against 275 GB free.

## 8. What is not measured, and which way each omission biases the result

| Omission | Bias |
|---|---|
| Takers hitting our size, which is absent from the recorded book | Flatters: fills are undercounted. Cross fills cover part of it; A2 brackets it. |
| Competitors reacting: other LPs adding size or stepping in front once we rest T at the best bid | Flatters our LIP share and queue position |
| Cancellations ahead of us in the queue | Flatters JOIN (fills undercounted); A1 brackets it |
| Stale-quote picks between 1 Hz reads | Flatters; A2 brackets it |
| Kalshi's actual treatment of designated-LP and Market Maker Agreement depth | Mixed: keeping it in the denominator understates our share; keeping it in the target test may overstate counted snapshots |
| Kalshi's discretion: revocation, and the true scoring code | Flatters: payout is assumed to be paid |
| Order write limits and acceptance | Flatters; the needed actions per second are reported |
| Mid mark at day end | Flatters relative to an exit; A4 brackets it |
| Capital cost and interest | Not charged; $ per unit of capital is reported so the reader can charge it |
| 01:30-12:30 UTC and the excluded families | Out of scope; the verdict does not extend to them |
| Programs that start mid-session, unobserved seconds, the per-second minimum over books, the daily $1 floor | Understate the reward (conservative) |

## 9. Implementation plan

**Nothing in the maker path changes.** `recorder.py`, `schema.py`, `STRATEGY_VERSION` (1.1.0), `http.py`, `cli.py`, `record_cli.py`, `replay_cli.py` and `record_maker_day.sh` stay as they are. `lip_cli.py` is not registered in `cli.py` until the study ends, so the maker cron's imports are untouched.

**New package `packages/market-normalization/src/pmvl_markets/lip/`:**
- `version.py`: `LIP_RECORDER_VERSION = "lip-1.0.0"`, `LIP_SIM_VERSION = "lip-sim-1.0.0"`.
- `programs.py`: `Program.from_api`, `n_total_seconds` (floor/ceil), `rate_per_hour`.
- `universe.py`: `FAST_INFO_PATTERNS`, and `select_session_universe(programs, markets, series_meta, session_start, session_end, study_date)`, which returns every candidate with a reason.
- `recorder.py`: the lanes, its own serializer, a skip-if-in-flight scheduler and a final drain. It reuses `KalshiProvider`, `parse_orderbook`, `AppendOnlyLog` and `recording.recorder._parse_trade`.
- `reader.py`: reads `.jsonl` and `.jsonl.gz` and runs `verify_log`. It builds compact per-market arrays with `book_before` and `book_after`, and computes tape completeness intervals.
- `scoring.py`: counting, whole-level qualification, the reference variants (FILING, HELP_CENTER, MIN), the multiplier, shares, accrual, and the payout with floor and cap. Variants A3, B2 and B3.
- `fills.py`: `Episode` (on `MakerQuote`); `fill_path` for QUEUE_MODEL, TRADED_THROUGH and FRONT_OF_QUEUE; `cross_fills`; `orphan_fills`.
- `simulator.py`: `LipPolicy` (the `QuotePolicy` fields plus size fraction, gate and inventory cap) and `simulate_market_session`.
- `study.py`: units, `_cluster_bootstrap_ratio`, looks, gates, verdict and report.

**Also new:**
- `services/worker/src/pmvl_worker/lip_cli.py`, with commands `preflight`, `record-day`, `health`, `settlements`, `replay-day` and `study`.
- `scripts/record_lip_day.sh`, and a cron line at 20:25 local that the user installs.
- `docs/lip-preregistration.md`, which is committed first.

**Order of work.**
1. **Today, before 12:25 UTC:** the pre-registration commit, then `programs`, `universe`, `recorder`, a minimal `reader`, their tests and the script.
2. **Days 1-3:** `scoring`, `fills`, `simulator`, `study` and their tests.
3. **Code freeze:** tag `lip-sim-1.0.0` before any recorded day is replayed. Until then only health counts are read.

**Tests that would catch the ways this could flatter the strategy:**

| Test | Catches |
|---|---|
| A side that reaches T only with our order scores v = 0 | Crediting a starved side we completed (pivotality) |
| An order placed at k + 0.3 s earns nothing for second k; credit uses the remaining size after a partial fill | Credit for time not rested, or for size already filled |
| When the next book is ineligible, v_k = 0; a bracket longer than 3.5 s is UNOBSERVED, but its fills still count | Sampling that pays across gaps |
| Help-center example: 20% × $100 × 8,000/10,000 = $16. Scores sum to 2 on every counted snapshot. Closed seconds stay in N_total. N_total floors start and ceils end. | Rule arithmetic |
| $0.99 pays 0; $1.009 pays $1.00; property test that the daily floor never pays more than the period floor | Floor and rounding |
| Property tests: A3 credit ≤ whole-level credit, and MIN reference ≤ FILING and ≤ HELP_CENTER | Variants that are not actually conservative |
| A print inside the 410 ms placement latency does not fill; a print 300 ms after a cancel decision does | Latency in our favour |
| A reprice resets `queue_ahead` to the whole level | Carried queue priority |
| Prints at our level smaller than the queue ahead do not fill; TOUCHED cannot reach any verdict path | Optimistic fill models |
| A crossing opposite book fills us; the ORPHAN_FILL variant fills our whole remaining size | Invisible adverse fills |
| The last point of `fill_path` equals `evaluate_quote().filled_size` | Drifting from the reused maker model |
| A poisoned future book leaves every decision unchanged | Look-ahead |
| IMPROVE never locks or crosses (property test on random books); the inventory cap is never exceeded | Impossible quotes, unbounded inventory |
| A market that never quotes adds 0 to the numerator and its eligible hours to the denominator | Conditional expectancy |
| The universe is deterministic, takes no book argument, and logs a reason for every rejected candidate | Selection on outcomes |
| Every recorder line says `lip-1.0.0`; `STRATEGY_VERSION` is still 1.1.0 after importing `lip`; concurrent lanes keep their own socket windows | Contaminating the maker study; wrong timestamps |
| Each tape poll starts at or before the previous poll's newest print; hitting the page cap is flagged; the final drain runs | Missing prints, which undercount fills |
| An unknown maker-fee type is charged; A4 walks the depth and pays the taker fee | Missing costs |
| The verdict table at each look, including gates; `INTERIM_NOT_A_DECISION` before look 1; no interval below 10 clusters; empty clusters kept | Decision leakage |

## Before the pre-registration commit

- Fetch the current Kalshi CFTC notice from kalshi.com/regulatory/notices and diff it against this spec. The August 2025 filing's term ended 2026-08-01, so a later notice governs.
- Confirm the account holds no Market Maker Agreement and is not an IB or FCM customer. US residency is already confirmed.
- The user installs the cron line. This design does not change any configuration.