Scope: read-only. No repository file was edited and no Kalshi or Polymarket API was called. The only files written are scratch files under /private/tmp/claude-501/-Users-xuao-Documents-2025-----AI-Projects-prediction-market-value-lab/6306567b-fa12-4743-9bb1-c4f3881d7852/scratchpad/lipstudy/: classify.py and classify2.py (ticker-pattern bucket rules), fivemin.json and hourly.json (concurrency series), and ../series_cat.json (a read-only export from data/pmvl.db).

## Findings that change the plan

1. **A third process already uses the Kalshi budget.** The launchd job `com.pmvl.collector` has been running `pmvl schedule` for about 3 days. It has its own `KalshiProvider` with a 9 req/s limiter and a burst of 18, and it runs:
   - an orderbooks job every 180 s. The docstring of `ingest/runner.py:refresh_orderbooks` puts a full-board refresh at "~125 Kalshi requests".
   - an ingest job every 600 s. Each run lasts 200-600 s and pulls about 7k markets, 700 events, 250 books and about 1k trades.

   Adding the limiter caps during maker hours gives 9 (maker) + 9 (collector) + 5 (crypto recorder, until 07:03 UTC today) = 23 req/s, which is over the 20 req/s basic tier. Feasibility therefore rests on actual usage, and nobody has measured it.

2. **The maker recorder actually uses about 2 req/s, not 9.**
   - `SynchronizedRecorder.capture_once` awaits each event's book, then each tape poll, one after another. At a round trip of about 410-490 ms, that caps a sequential loop near 2 requests per second. `record burst` shows the same ceiling: `edge_watch_burst.csv` records 2.033 sweeps/s.
   - A cycle is 8 book requests plus about 11 tape requests (a 5 s budget at about 0.45 s each) every ~10.3 s, which is about 1.9 req/s. This is inferred, not counted.
   - Consequence for LIP: holding 1 Hz needs concurrent requests. A sequential "books then tape" loop cannot do it.

3. **The LIP recorder must not touch recorder.py, schema.py or `STRATEGY_VERSION`.**
   - `docs/maker-multiday-preregistration.md` makes only days recorded under 1.1.0 eligible. Any change to those files would require a version bump, and that bump would disqualify every later maker day.
   - `EventSnapshot.as_dict` and `TradeRecord.as_dict` stamp "1.1.0" on every line. LIP lines written through them would claim the maker recorder's version, so LIP needs its own serializer and its own version constant (for example `lip-1.0.0`) in a separate module.

4. **lip.json is a biased view of where the money goes.**
   - Long programs are listed up to 3 weeks ahead. Short ones are listed about a day ahead (15-minute programs run only to 09-25 04:00) or just in time (KXTEMPMIAH has 10 programs in the snapshot against 1,660 in the preceding 7 days).
   - By scheduled dollars in the snapshot, intraday programs are 3.5%. By the realized 7-day flow they are 25%.
   - Choose the universe from history plus snapshot, not from the snapshot alone.

## 1. What a continuous two-sided resting-quote simulator with LIP scoring can reuse

### Reuse unchanged
- **recording/writer.py**: `AppendOnlyLog` (fsync per line, which is fine at one line per batch request), `read_log`, `verify_log`.
- **providers/kalshi.py**:
  - `KalshiProvider.get_orderbooks_synchronized`: at most 100 arbitrary tickers per request (`MAX_BATCH_TICKERS`), repeated `tickers` parameter, rejects unrequested tickers, timing measured around the socket.
  - `parse_orderbook`.
  - `KalshiProvider(client=...)` accepts an injected `HttpClient`, so the LIP process can set its own rate without editing the provider.
- **maker_experiment.py**:
  - `FillEstimator` and `ADMISSIBLE_ESTIMATORS`.
  - `MakerQuote` (`yes_price`, `is_relevant`, `at_level`, `through`).
  - `ReplayIndex.trades_for`.
  - `settlement_pnl_per_contract`.
  - `QuotePolicy` fields: `tick`, `placement_latency_ms` (300), `maker_fee_rate`, `taker_fee_rate`, `improve_ticks`, `size`. One parameter must change: `min_spread` defaults to 0.02, which never quotes a one-tick spread. Deep LIP books typically sit at one tick, so LIP must declare 0.01.
- **pricing/fees**: `kalshi_maker_fee`, `taker_fee`.
- **maker_study.py**: the pure functions `_cluster_bootstrap` and `_interval`, plus `MIN_CLUSTERS_FOR_INTERVAL = 10`, `BOOTSTRAP_ITERATIONS = 10,000`, `BOOTSTRAP_SEED` and the three-verdict vocabulary.
- **Readers**: `EventSnapshot.from_dict`, `TradeRecord.from_dict`, `reader.load_recording` (reads any version string; `single_version` refuses mixed files).
- **record_cli.py `settlements`**: works unchanged if the LIP recorder writes `<day>/session-*/snapshots.jsonl` with a `books` map, because it collects tickers from those books and looks them up 100 at a time via `/markets?tickers=`. Pass `--out artifacts/forward/lip-settlements.jsonl`.
- **replay_cli.py `_load_payouts`**.

### Needs extension
- **`evaluate_quote`**:
  - Today it returns only the total filled size per estimator and `first_fill_at`. A continuous simulator needs the fill path over time, e.g. a new `fill_path(quote, window) -> [(traded_at, cumulative_filled)]` for `QUEUE_MODEL` and `TRADED_THROUGH`. It drives both inventory and the resting size that still earns LIP credit. `explore_recorded.py` accrues full-size credit until the first fill and ignores partial fills.
  - Resting must be modelled as consecutive episodes, one per price. A reprice starts a new queue. Within an episode, the existing static `queue_ahead` plus the at-level-plus-through arithmetic can be kept.
- **`ReplayIndex`**:
  - `book_at` returns the first book at or after a time. Per-second scoring should bracket with a new `book_before` as well.
  - The index holds one pydantic `OrderBook` per market per snapshot. At 1 Hz for about 80 markets that is about 288k objects per hour, so it needs a compact per-market array form.
- **`_quote_from_book`**: its join/improve/never-cross price rule is reusable as a helper. `generate_quotes`, which places non-overlapping, one-sided, fixed-60-second quotes, is not reusable.
- **`MakerExperimentReport`, `pool_sessions`, `pool_settlement_holding`**: all are hard-wired to `SessionResult.report.outcomes` and per-quote PnL. They need a generic "value per cluster" input. The LIP unit is the (market, program period), because payout and the $1 floor apply per period, and it clusters by date.
- **`reader.load_recording`**: must also load programs.jsonl, universe.jsonl and tape_polls.jsonl, decode bids-only books (re-derive asks the way `parse_orderbook` does), and read gzip segments. `writer` needs segment rotation with chain continuity.
- **`recorder._pull_trades`**: keep the `min_ts` watermark and cursor loop, but add a variant for the global (no-ticker) feed and log every poll's watermark.
- **`HttpClient`**: `RateLimiter(rate, burst)` supports a burst, but `HttpClient` never passes one, so capacity is always 2× rate. It also needs per-minute request counters written to a log.

### Must be new
- **`lip_scoring.py`**: one tested implementation of the rule, replacing the two exploratory ones, which disagree. `share_now.side_scores`/`share` use whole-level inclusion; `explore_recorded.side_credit` caps our order at `min(size, target - ahead)` and computes others' credit without our order. It must cover:
  - **Side qualification**: walk whole levels from the best bid until cumulative size reaches the target; a 99c top bid does not qualify.
  - **Reference price**, two variants: FILING puts it at the best bid; HELP_CENTER puts it at the first level where cumulative size reaches target/5.
  - **Credit**: discount factor raised to the number of ticks below the reference; normalized share per side; a snapshot score of at most 2.
  - **Exclusion**: the market is not open, or either side is below target.
  - **Payout**, two variants:
    - HELP_CENTER: pool × Σ(my score) / (2 × N_total seconds). This is additive per second, which is what the exploratory scripts assume.
    - FILING: pool × Σ(mine) / Σ(all) over the whole period. This needs the book for the entire period, including seconds when we are not quoting.
  - **$1 minimum payout** per user per period, rounded down to the cent. The exploratory scripts ignore it. It bites on short programs: a $20 15-minute period needs at least a 5% score share to pay anything.
  - **Pivotality guard**, to encode the out-of-scope decision: in any second where our order is what lets a side reach target, credit is zero. This makes KXTEMPMIAH (YES side empty in every recorded snapshot) unscoreable by construction.
  - Tests should include the help-center example (20% × $100 × 8,000/10,000 = $16).
- **`lip_simulator.py`**, a per-market state machine:
  - YES and NO bids resting together; a declared join or improve-1 policy; size S (300 or 1,000 against the target).
  - Reprice rules with 300 ms placement latency; cancel at close; an inventory cap per side.
  - Episodes filled against the tape; LIP credit per second computed from the remaining resting size.
  - PnL reported three ways: LIP reward after the floor, plus fills marked at 60 s and held to settlement; the maker fee where charged; and inventory, since YES + NO fills lock $1 pairs.
  - Unconditional over every eligible market-period, with zero for periods that earn nothing.
- **`lip_recorder.py`** and a `pmvl record lip` command (section 3).
- **Replay commands** `pmvl replay lip-days` and `lip-settlement`, and **`docs/lip-preregistration.md`**: rule variant, universe rule, policy grid and family size, metrics, decision rule. It must be committed before any data exists.
- **A daily guard** that checks the maker sessions' snapshot counts and skew against their pre-LIP baseline.
- **Known limit:** own-order impact cannot be measured in shadow. It matters more at 300-1,000 lots than at the maker study's 50.

## 2. What the current recorder captures, and whether it is enough

### Cadence
- `record sessions` (via `scripts/record_maker_day.sh`): 20 sessions of 30 minutes, 8 events each (from the frozen 21-series universe), one batched `/markets/orderbooks` request per event per cycle, 10 s interval.
- On 2026-09-24 the per-event snapshot gap was 10.0 s at p50, 10.3-10.8 s at p90, 14.5 s at max, for 1,280-1,440 snapshots per session.
- `skew_bound_ms` (the request round trip) was 315-660 ms at p50 and 2.5-8.5 s at max.
- Kalshi samples the book once per second, so this is 10× under-sampled.
- The tape shares a 5 s budget per cycle (`trade_budget_fraction` 0.5) and exhausts it in almost every cycle (172 of 172, 160 of 160, and so on).
- 2026-09-24 session-05 had 6.7 productive tape polls per cycle, so each watched ticker was polled roughly every 74 s. Pagination (`TRADE_PAGE_LIMIT` 200, `TRADE_MAX_PAGES` 10) meant no poll hit the row limit that day. Day 3 had 13-37 such polls per session.

### Book depth
- Full L2 bids on both sides are recorded as `yes_bids`/`no_bids`, with `yes_asks`/`no_asks` derived redundantly.
- The `depth=25` parameter does not truncate: sides with up to 76 levels appear, and only 170 of 32,760 sides had exactly 25.
- Sizes are aggregated per price level. That is enough to compute our share under the filing's whole-level rule. Under a per-order reading of the help center, the level that crosses the target would need queue order; aggregate L2 can only bracket that (include or exclude our order).

### Tape and timestamps
- The tape has `taker_side` (yes/no), fractional `size` (`count_fp`), `yes_price`, `traded_at` to the microsecond, `trade_id` and `is_block_trade`. That is sufficient for `QUEUE_MODEL`.
- Books are stamped with the socket window only. `OrderBook.source_timestamp` is parsed but `EventSnapshot.as_dict` drops it, so any venue timestamp in the response is lost.

### What is missing for LIP
- **No program list is recorded.** Programs change: 1,254 had end ≤ start in the history, and upcoming programs appear late.
- **No per-second market status**, which the exclusion rule needs.
- **Legs are resolved once per session** (`prepare()` calls `/markets?event_ticker` once). 15-minute and hourly markets roll during a 30-minute session, so the recorder cannot follow them.
- **Coverage is 10× too coarse** for the intraday programs.
- **Tape completeness cannot be proven.** Per-ticker watermarks (`_trade_floor`) live only in memory, `run()` does no final tape drain, and `replay_cli._day_sessions` calls `run_experiment` without `tape_complete_through`.
  - Side finding for the maker study itself: quotes in roughly the last 2 minutes of each session can have missing prints, which pulls fills toward zero.
- **Overlap with LIP markets is small**: the maker recordings cover only 64 LIP markets (54 rain, 10 Miami temperature).

## 3. Recording plan for LIP markets

- **Process and output**
  - New module with its own version and its own process, launched by its own mkdir lock. The maker script's `pgrep -f "pmvl record sessions"` guard will not match it, so the two can run side by side.
  - Start after 07:03 UTC today and after the pre-registration commit.
  - Output to `artifacts/forward/lip-daily/<UTC date>/session-HH/`: 24 hourly sessions, with a `study.json` in the maker manifest shape so `settlements` works unchanged.
  - Files per session: programs.jsonl (diffs, plus a full list hourly), universe.jsonl, snapshots.jsonl, trades.jsonl, tape_polls.jsonl, markets.jsonl, status.jsonl.
  - HttpClient at rate 5 with burst 5.
- **Universe rule** (frozen on program attributes, not a series list, because the intraday series change week to week): Tier A is every program with a pool of at least $10/h; Tier B is $2-10/h. Program start and end times come from the recorded list.

| Item | Cadence | Requests | Rate (req/s) |
|---|---|---|---|
| Program list: `/incentive_programs?status=active&type=liquidity&limit=10000` | every 300 s | 1, plus `status=upcoming` every 900 s | 0.004 |
| Tier A books (p50 71, p90 107, max 192 markets; about 30 events) | 1 Hz | 1-2 concurrent batch requests per second | 1-2 |
| Tier B books (about 860 at p50, 1,030 at p90) | every 10 s | 9-11 requests | 0.9-1.1 |
| Tape: global `/markets/trades?min_ts=<watermark>` with cursor, polled every 2 s; each poll's watermark logged; only LIP-universe prints kept | every 2 s | max(0.5, prints per second / 200) | 0.5-1.5 if the exchange prints 100-300/s |
| Tier A market status via `/markets?tickers=`, plus metadata for about 70 new tickers per hour | every 30 s | 1-2 per call | ≤0.07 |
| Daily `pmvl record settlements` | once a day | tens | negligible |

- **Global tape: calibrate first.** Per-ticker polling of Tier A every 20 s would cost 3.5-5.4 req/s, so the global feed is preferred. Measure the exchange's print rate in a 10-minute read-only pre-flight before choosing. `TRADE_PAGE_LIMIT` is 200; if the endpoint accepts 1,000, the tape costs 5× less, but that is unverified.
- **LIP total**: about 2.5-3.5 req/s at p50 and about 4.7 at p90, under a cap of 5.
- **Budget at limiter caps (maker hours)**: 9 + 9 + 5 = 23 until 07:03 UTC, then 18. Adding LIP's 5 gives 23. Caps alone do not prove feasibility.
- **Budget at estimated actual usage**:
  - maker about 2
  - collector about 1-3: 125 requests per 180 s is about 0.7/s from the orderbooks job alone, plus ingest
  - LIP 2.5-4.7
  - total about 6-10 req/s, well under 20.
- **Conditions to pre-register before relying on those estimates**:
  - Instrument per-process request counts for a day first.
  - Optionally lower or pause the collector's Kalshi jobs (the user's call; nothing here changes config).
  - On any 429, drop Tier B to 30 s for 10 minutes and log it. Tier A gaps are logged, never hidden.
  - Guard the maker study: if any maker session falls below 1,200 snapshots (baseline 1,264-1,447) or its skew p50 rises above 700 ms, LIP stops running during 02:10-12:10 UTC from then on.
- **Zero-interference alternative**: run LIP only 12:30-01:50 UTC. That keeps 59% of intraday dollars and 81% of daily gas-price dollars, at the cost of a declared time-of-day selection.
- **Storage**:
  - The maker format costs 1,823 bytes per leg-snapshot. Tier A at 1 Hz would be 11-17 GB/day and Tier B at 10 s 13-16 GB/day, which would fill the 275 GB free in about 8-11 days.
  - Fixes (all new code): write bids only (asks are derived, so this halves size), truncate at cumulative size of 2× target, write a leg only when it changes with a 60 s keyframe, and gzip hourly segments. Aim for 5 GB/day or less over 20 days.
- **Tier A loop**: it must fire its 1-2 requests concurrently at each tick, separate from the tape loop.
- **Unit of analysis and duration**: the (market, program period), clustered by date. At least 10 dates are needed before any interval is reported; plan 20, matching the maker study.

## 4. Distribution facts relevant to choosing the universe

### The snapshot (lip.json, fetched 02:50-02:55 UTC 2026-09-24)
- 6,640 programs (5,431 active, 1,209 upcoming) across 517 series, $716,067 scheduled.
- Target size: 1,000 for 5,862 programs; 300 for 748 (15-minute FX, metals, natgas and copper, and table tennis); 2,500 for 20 politics programs; 500 for 10 weather programs.
- Discount factor 0.5 for 6,620 programs and 0.4 for 20.
- Duration: p10 0.25 h, p50 129 h, p90 336 h. 1,106 programs of 15 minutes or less hold $22k; programs longer than 7 days hold $276k.
- Pool rate per program: p50 $0.84/h, p75 $6.26/h, p90 and above about $80/h.
- Not concentrated by series: the top 10 series hold 20% of scheduled dollars, the top 100 hold 54%, and 110 series have a single program.
- At 02:52: $11,670/h live across 5,431 markets. Daily state gas-price ladders (786 markets at about $6/h) are 36% of that live rate.

### The flow (history plus snapshot, 7 days to 09-24 02:50, sampled every 5 minutes)
- Live programs: p10 3,334, p50 4,447, p90 5,232. Pay rate: p10 $6.2k/h, p50 $9.3k/h, p90 $11.3k/h, about $213k/day.

| Bucket | $/day | Share | Typical live markets | Notes |
|---|---|---|---|---|
| Energy/commodities, daily (state gas ladders) | $52.8k | 24.8% | about 616 | Live about 12:00-04:00 UTC |
| Entertainment/culture | $38.1k | 17.9% | about 1,294 | |
| Hourly weather | $23.7k | 11.1% | 10 | KXTEMPMIAH only, about $104/h per strike |
| Politics | $17.1k | 8.0% | | |
| Crypto/FX/metals 15-minute | $16.2k | 7.6% | about 9 | $80/h each |
| In-play sports | $13.1k | 6.2% | about 34 | Table tennis; target 300, median $13.3/h, 90-minute periods |
| AI/tech/company metrics | $11.7k | 5.5% | | |
| Mentions | $9.2k | 4.3% | | |
| State/industry data releases | $8.9k | 4.2% | | |
| Sports futures | $7.4k | 3.5% | | |
| Economics | $5.4k | 2.5% | | |
| Weather/climate (rain etc.) | $4.0k | 1.9% | | |

- **Intraday concentration**: hourly weather, 15-minute and in-play together are about 25% of the flow ($53k/day) through about 53 of about 4,450 live markets. Three series (KXTEMPMIAH, KXTTELITEMATCH, KXCRYPTOLEAD15M) are $45.2k/day, or 21%.
- **Rate tiers** (share of the live pay rate):

| Pool rate | Live markets (p50) | Share of live $/h |
|---|---|---|
| $50/h or more | 19 | 19% |
| $20/h or more | 38 | 24% |
| $10/h or more | 71 | 31% (32% of $/day) |
| $5/h or more | 461 | 53% |
| $2/h or more | 934 | 73% |
| $1/h or more | 1,564 | 82% |

- **Time of day**: intraday programs pay at a flat rate around the clock. 59% of their dollars fall outside the maker window.
- **Churn**:
  - the WTI, gold and silver 15-minute programs ended 09-19
  - platinum and palladium 15-minute programs started 09-17 and 09-18
  - EUR, GBP and JPY 15-minute programs started 09-23 23:02
  - KXTEMPMIAH's last scheduled end in the data is 09-24 03:00. Whether it continues is unconfirmed.
- **Maker fees**: of the 153 LIP series that data/pmvl.db covers, 3 charge makers (KXCPI, KXCPIYOY, KXLLM1, `quadratic_with_maker_fees` at a 0.0175 rate). The fee type of the other 364 series is unknown locally and must be recorded.
- **In-scope pool is smaller than it looks**: the largest single pool, KXTEMPMIAH, fails the pivotality rule (its YES side was always empty in the recordings), so the in-scope intraday pool is well below 25%.

## Unverified items to settle before pre-registering
- The rule filing's own term ended August 1, 2026, while the help center gives a program end of January 1, 2027, so a later notice governs. It needs to be fetched from kalshi.com/regulatory/notices; that web fetch was not part of this task.
- The token cost of a 100-ticker `/markets/orderbooks` call under Kalshi's rate-limit rules (docs.kalshi.com/getting_started/rate_limits).
- Whether the global trades feed honours `min_ts` and accepts `limit` above 200.
- The actual request rates of the maker recorder and the collector.
- Whether the WebSocket orderbook-delta and public-trades channels (authenticated, so an API key is the user's decision) would replace REST polling.
