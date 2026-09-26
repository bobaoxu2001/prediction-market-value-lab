# Pre-registration: the LIP maker shadow study, `lip-1.0.0`

**Status.** Committed 2026-09-24, before any LIP study data exists. Day 1 cannot start before every gate in §8 is closed. The study is research, paper and shadow only: it never sends an order, never calls a write endpoint and never uses an API key. The allowed verdicts are `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY`, `INSUFFICIENT_EVIDENCE` and `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION`. Every report before the final analysis is labelled `INTERIM_NOT_A_DECISION`.

**Where this design comes from.**
- **Base:** the "realism" design, which had the highest summed judge score (60.5, against 58.0 for rigor and 44.5 for operational).
- **Taken from rigor:**
  - the family fold, the per-family cap and hash admission;
  - the two-way date × family bootstrap and the leave-one-family-out check;
  - TRADED_THROUGH as a third fill scenario;
  - the 1-hour mark plus a terminal exit charge;
  - stale-price credit and the admission log.
- **Taken from operational:**
  - one HTTP client per lane, a skip-if-in-flight scheduler, a final tape drain and the tape audit;
  - the per-market-day $1 floor;
  - admitting only markets on the 1-cent grid;
  - a separate CLI that the maker cron never imports;
  - waiting for the maker lock before starting.
- Every flaw the judges raised is mapped to its fix in Appendix A.

**Question.** A bona fide maker quotes both sides of Kalshi LIP markets, joining or improving other traders' best bid. Does the LIP reward it earns exceed what the same resting orders lose on their fills (adverse selection, fees, exit cost)?

**Estimand.** For policy p and reading combination c, θ_{p,c} = ΣV / ΣH.
- **H (admitted market-hours):** every second of the daily quoting window (12:30:00 to 00:45:00 UTC) in which an admitted (market, program period) pair is live. A second counts whether or not the recorder was healthy, the market was open, the gate was open, or the policy was quoting or filled.
- **V:** the verified LIP payout (after the cap, the $1 floor per market-day and the cent floor), plus fill value under the valuation, minus maker fees, minus exit costs.
- An unfilled quote earns 0 fill value. A second that is not counted, not observed or not credited earns 0 LIP.

**Two envelopes.** Every open point is resolved twice:
- **LOW** resolves it against the strategy. It is the only envelope that can support a positive verdict.
- **HIGH** resolves it in the strategy's favour. It is the only envelope that can support a negative verdict.
- For every reading combination, LOW ≤ HIGH holds per market-day by construction, and a test enforces it.

**Versions.** `LIP_RECORDER_VERSION = "lip-1.0.0"` and `LIP_SIM_VERSION = "lip-sim-1.0.0"`. The maker study keeps `STRATEGY_VERSION` "1.1.0", and nothing it imports is modified.

---

## Amendment 1 (2026-09-24, before day 1): gates G1 and G2 closed

**G1 - the governing rule text.** The notice now in force is KalshiEX's *Amendment to August
2025 Liquidity Incentive Program*, filed with the CFTC on 2026-07-15
(https://www.cftc.gov/filings/orgrules/rules07152610358.pdf; an earlier amendment of
2026-02-11 is https://www.cftc.gov/sites/default/files/filings/orgrules/26/02/rules02112639183.pdf).
Diffed against §1:

- **Reference price.** The amendment strikes "the highest yes bid price ... is assigned to the
  Reference Yes Price" and sets it instead to the level at which the qualifying total first
  reaches one fifth of the Target Size. That is Ref_H. **Ref_F is no longer a reading of the
  rule and is removed from both envelopes**: §1 step 6, §5.6 and the O_max / M_min
  constructions use Ref_H only.
- **Exclusion and payout.** "Snapshots will be excluded if a market is not open for trading or
  if there is not two-sided liquidity (i.e., resting orders sufficient to meet the Target Size
  on each side of the market)", and the payout is multiplied by "the ratio of non-excluded
  snapshots to total snapshots". That is H's payout. **G_F is removed from HIGH** (§1 step 9,
  §5.8 item 5): both envelopes use G_H.
- **Closed seconds.** The text both takes snapshots only "if the market is open" and excludes
  snapshots "if a market is not open". Whether closed seconds sit in the denominator stays
  ambiguous, so §1 step 2 is unchanged: LOW keeps them in N_total, HIGH does not.
- **Multiplier.** "If the price of the Qualifying Yes Bid is greater than the Reference Yes
  Price, N is set to zero", as §1 step 7 already implements.
- **Bounds.** Time Period at most 31 days; Target Size above 100 and below 20,000; Discount
  Factor at most 1.00; Time Period Reward from $1 to $1,000 per calendar day.
- **Eligibility.** The filing excludes affiliates, Market Maker Agreement holders, and IB/FCM
  customers transacting through them. The help center's further exclusion of non-US users
  does not apply: the user confirmed a US account on 2026-09-24.
- **Pivotality (§1 step 4) is unchanged and remains a project choice, stricter than the
  rule.** The rule counts all resting orders toward two-sided liquidity; this study never
  counts its own, in either envelope, so a snapshot our order would make count earns
  nothing.

The HIGH envelope is therefore tighter than first written. That can only make the negative
verdict harder to reach and cannot help the positive one, which is set by LOW.

**G2 - rate limits** (https://docs.kalshi.com/getting_started/rate_limits). The Basic tier is
200 read and 100 write tokens per second; most requests cost 10 tokens, so 20 reads/s and
**10 write actions/s: W_LOW = 10**. Individual cancels cost 10 tokens; batch cancels 2 per
order; batch creates 10 per order. The cost of a 100-ticker `/markets/orderbooks` call is not
listed as an exception on the public page; the exceptions are served only by the
authenticated `GET /account/endpoint_costs`, which this study does not call. It is therefore
taken as the default 10 tokens (one request), and the §8 rule stands as the check: any 429
at the planned rates moves the book cadence to 2 s for the whole study.

**Gate log.**

- **G1, G2** - closed above, 2026-09-24.
- **G3** - closed 2026-09-24: the user confirmed that none of a Market Maker Agreement with
  Kalshi, trading through an introducing broker or FCM, or being a Kalshi affiliate or
  employee applies to the account.
- **G6** - approved 2026-09-24: the user agreed to a second daily cron line for the LIP
  recorder at 12:25 UTC, to be installed only after G4 and G5 close, with the exact line
  shown to the user.
- **Still open before day 1:** G4 (code, tests, synthetic replay, tag), G5 (pre-flight and
  `cadence.json`), and the installation under G6.

---

## Amendment 2 (2026-09-25, before day 1): implementation decisions

Recorded before any study data exists. Each item resolves a point the text above left open,
or replaces a mechanism with one that is at least as strict against the strategy. The code is
`pmvl_markets.lip` at the commit tagged `lip-sim-1.0.0`.

1. **Write budget, REG_LOW.** One shared bucket across ~160 markets cannot be replayed market by
   market in reasonable time. Each market instead gets an equal static share, 10 / (the day's
   peak number of admitted markets) actions per second, with no burst beyond one token; and at
   the instants every market acts together (a segment start, a gate opening) market *i*, in
   admission order, waits *i* / 10 s - its place in the shared queue. REG_HIGH is unlimited.
2. **The counterfactual book (5.3).** Volume our fills absorbed is added back to others at the
   print price for 60 s after the fill; a HEAVY cross fill is removed from the opposite side
   once, best level first; an orphan fill counts as absorbed volume on our side.
3. **Clock (4.2).** The day's offset is the median `sntp` sample; prints are moved into local time
   by it; ε = |offset| + 50 ms widens HEAVY's fillable windows and narrows THROUGH's and FIFO's.
   A day with no clock sample uses ε = 250 ms.
4. **Status.** Each market's status, and the exchange flag, is stamped when its response
   arrived, not when the poll began. A decision reads only the latest status known at or before
   it, and stops quoting only on an explicitly non-open status or an inactive exchange - an
   unknown status stops nothing. LOW credit needs both bracketing polls known and open (a failed
   poll, or a market the poll did not answer for, is unknown and counts as not open); HIGH
   counts unknown as open.
5. **Denominators.** LOW keeps closed seconds in N_total; HIGH leaves the ones it observed
   closed out. Every admitted second stays in ΣH in both.
6. **Counted day (7), as written.** A day counts if successful reads cover at least 80% of the
   window's ticks, counted at the day's recorded book cadence. A tick is successful only if every
   book lane that had admitted markets due at it read them. A tick the recorder slept through,
   paused over, or had nothing to read at is not successful - so a day with nothing admitted, or
   one the machine slept through, does not count.
7. **The cap (5.8).** A program's per-account cap is applied across its days when results are
   pooled: each day pays at most the cap less what its earlier days paid. Because HIGH accrues
   more and exhausts the cap sooner, LOW's pay on a day is held at or below HIGH's.
8. **Days that were not recorded.** The calendar runs from day 1 (in `lip-gates.json`) to the 20th
   counted day, day 1 + 29, or the seventh consecutive day with no admitted market due at any
   tick (the exogenous stop), whichever comes first. A day skipped by the recorder, or never
   recorded at all (the machine slept), contributes every pair live in its window at its
   admitted hours and $0. A recorded day without its replay, or a day directory holding
   recordings but no `study.json`, stops the final analysis.
9. **Verification and withdrawal.** A program is verified only if later seen `paid_out` and every
   row ever recorded for it carries the same terms. Withdrawal is read from the recorder's
   within-day registry and from complete active listings across days.
10. **TAPE_PT.** If the pre-flight selects it: each recorded market's own tape every 60 s with its
    own watermark and coverage, one book lane, and a total cap of 100. When `min_ts` is not
    honoured, pages are read until one reaches back past the watermark.
11. **Exit valuation.** When the day-end flatten has no two-sided mid to be measured against, its
    charge (proceeds less quantity times a reference, less the fee) takes, among the reference
    book's mid or bid, the walk book's bid and the last two-sided mid, the *highest* reference in
    LOW - and never less than the sale's own average price, so a sale is never booked as a gain -
    and the lowest in HIGH. HIGH's fallback marks use the last two-sided mid.
12. **Integrity.** Every stream of a recorded day is read against the entry count and final
    digest the recorder wrote into `health.json`; a day without them, or a stream that ends early,
    is an integrity failure, not a quiet day.
13. **Isolation.** Every lane's requests are logged per minute, and the off-day commands
    (`preflight`, `programs-paid`) refuse 02:00-12:25 UTC and the maker lock and log their
    requests; any of them within 60 s of a maker session is a breach.

The adversarial review that led to these (35 reproduced findings) and the re-review of its fixes
(19 more) are recorded in `docs/lip-review.md` and are part of gate G4.

---

## 1. Scoring rule as implemented

**Sources.** Public documents; the design record (grounding reports, the three designs, both judges) is in `artifacts/research/lip-1/design/`.
- **F:** the KalshiEX CFTC notice of 8 Aug 2025, Appendix A (`lip_rules.txt`).
- **H:** help-center article 13823851, modified 2026-09-03.
- **W:** help-center article 16076644.
- **A:** the `GetIncentivePrograms` schema.
- **D:** the order-book, fixed-point and subaccount docs.

**Which text governs.** H wherever it speaks, F where H is silent. Where both readings stay open, LOW takes the reading worse for us and HIGH the better one. F's own term ended on 2026-08-01, so gate G1 (§8) fetches the notice that now governs and diffs it against this section:
- a difference found before day 1 is an amendment to this section;
- a difference found after day 1 means both scorings are computed, LOW takes the lower and HIGH the higher.

**Program inputs** (from the recorded `programs.jsonl`):
- R = `period_reward` / 10,000 USD (the field is in centi-cents).
- T = `target_size_fp` (contracts, 0.01 granularity).
- DF = `discount_factor_bps` / 10,000.
- cap = `max_reward_per_account` / 10,000 when it is not null. It is null in all 6,640 rows of the snapshot.
- N_total = ceil(end) − floor(start), in whole seconds. The timestamps carry microseconds.
- Price grid: admission (§2) requires the linear 1-cent grid. One step is therefore $0.01, the highest valid price is 0.99 and the lowest is 0.01. This removes the sub-cent tick ambiguity.

**Steps.**
1. **Snapshot seconds.** There is one snapshot per whole second k ∈ [floor(start), ceil(end)), taken at an unknown, uniformly random instant inside [k, k+1). All N_total seconds are in the payout denominator.
2. **Open test.** A snapshot is excluded if the market is not open. An excluded snapshot scores 0 for everyone and stays in N_total. "Not open" means any of:
   - the market status is not `open` or `active` (`initialized`, `paused` and unknown count as not open in LOW);
   - `/exchange/status` reports trading inactive or maintenance;
   - k ≥ `close_time`.

   This is H's treatment. F takes no snapshot at all while the market is closed, which pays more, so F's treatment is used in HIGH only.
3. **Sides.** The YES side is YES bids in YES prices. The NO side is NO bids in NO prices. A YES ask at p is the same book entry as a NO bid at 1 − p and scores only on the NO side. A two-sided quote is one YES bid plus one NO bid.
4. **Target test, with the pivotality rule.** The test uses other traders' orders only; our paper orders are never included. A side qualifies iff:
   - it has at least one bid;
   - its best bid is below 0.99;
   - the cumulative size of whole levels, walked from the best bid, reaches T or more.

   A snapshot counts only if both sides qualify (H). This carries out the project decision that completing a starved side earns nothing: our orders never enter this test, in either envelope.
5. **Qualifying bids.** Insert our orders at their prices with their remaining size. Walk whole levels from the best bid until cumulative size ≥ T. Every order on those levels qualifies at its full remaining size, and the last level counts in full. Orders deeper than that earn 0. §5 adds a price-time truncation in LOW.
6. **Reference price.**
   - Ref_H is the price of the first level, walking down from the best bid with our orders included, where cumulative size reaches T/5.
   - Ref_F is the best bid, with our orders included.
7. **Multiplier.** m = DF^N, where N = max(0, (Ref − price) / 0.01) is a distance in grid steps, not a count of occupied levels. Bids at or above Ref get m = 1.
8. **Share.** Each order scores m × remaining size. share_side = Σ(our scores) / Σ(all qualifying scores on that side), and v_k = share_YES + share_NO, which lies in [0, 2]. On a counted snapshot all traders' scores sum to exactly 2.
9. **Period payout (H).** G = R · Σ_k v_k / (2 · N_total). This equals P × R × (N_nonexcluded / N_total), so the formula already includes H's exclusion factor and can be added up second by second. F's payout is G_F = R · Σ_k v_k / Σ_k (q_YES,k + q_NO,k), where q = 1 if that side qualifies; F counts one-sided snapshots and has no exclusion factor. With the reference rule held fixed, G_F ≥ G_H, so G_F is used in HIGH only.
10. **Cap.** G ← min(G, cap) when a cap is set.
11. **Minimum and rounding.** Pay floor_to_cent(G) if G ≥ $1.00, otherwise pay 0. The study applies this per (market, program period, study day) (§5.8). That can never pay more than applying it once per period.
12. **Payment verification.**
    - LOW counts a payout only if, by the verdict date, the program has been observed with `paid_out = true` and with `period_reward`, `target_size_fp` and `discount_factor_bps` unchanged.
    - HIGH counts it unless the program vanished from the active list before its end without ever showing `paid_out`.
    - For scale, among programs that ended more than 7 days before the snapshot, 182,676 show `paid_out = true` and 6,702 show false.
13. **Who is scored.**
    - One account on subaccount 0. Each policy is a separate hypothetical account, and policies are never summed.
    - Eligibility: the account is US (confirmed by the user); it must also not belong to an affiliate or employee, not hold a Market Maker Agreement, and not be an IB or FCM customer (gate G3).
    - Depth of ineligible traders (MMA holders, designated LPs, non-US users) stays in both the target test and the denominator, because public data cannot identify it. §9 lists the target-test side of this as an optimistic bias that nothing here brackets.
14. **Abuse clause.** The strategy uses only bona fide join and improve prices. It has no rule that cancels when aggressive orders approach, no reactive depth gate, and it applies step 4.

---

## 2. Universe rule

**Inputs.** Only the recorded program list, market metadata (`status`, `close_time`, `price_ranges` / `price_level_structure`, `event_ticker`), the series ticker and wall-clock time. The admission functions take no book, trade, price, volume or settlement argument, and a test enforces this.

**2.1 Program-period eligibility** (all must hold):
- `incentive_type = liquidity` and `end > start`;
- `end − start ≥ 21,600 s` (6 h);
- pool rate R / hours ≥ $2.00/h;
- the series ticker (the market ticker before its first `-`) matches none of the fast-information patterns below. These are frozen here, and the list of series they matched in the saved history is committed next to the pre-registration so any drift is visible.

| id | Family | Pattern (Python `re.search` on the series ticker) |
|---|---|---|
| X1 | Intraday | `15M$`; `^KXTEMP[A-Z]*H$` |
| X2 | In-play and live-event resolution | `^KX(TT[A-Z]*MATCH\|MIXEDDOUBLESMATCH\|SQUASHMATCH\|DARTSMATCH\|CHESSMATCH\|CHESSGAME\|WRESTLINGMATCH\|CRICKET[A-Z0-9]*MATCH\|VALORANTGAME\|LOLGAMES\|CBAGAME\|NCAA[A-Z]*GAME\|MLBFTGAME\|CLUBFGAME\|EFLCUPGAME\|BALLERLEAGUEGAME\|PICKLEBALLGAMES\|PPLGAMES)$`; `MENTION`; `SAY$`; `^KXTRUTHSOCIAL`; `(GAME\|GAMES\|MATCH\|SPREAD\|TOTAL)$` unless the ticker contains `SEASON` |
| X3 | Real-time observed measurements | `^KX(HIGH\|LOW\|TEMP\|RAIN\|SNOW\|WIND)`; `ERCOT`; `PJM` |
| X4 | Continuously traded underlyings | `^KX(BTC\|ETH\|SOL\|XRP\|DOGE\|CRYPTO\|EURUSD\|USDJPY\|GBPUSD\|INX\|NASDAQ\|WTI\|BRENT\|GOLD\|SILVER\|NATGAS\|COPPER\|PLAT\|PALL)` |

**Why these are excluded.** In these markets the fill model is known to flatter the strategy:
- Informed flow reprices within about a second (`weather-source-lag.md`; spot-driven 15-minute markets; points scored in in-play matches).
- Makers ahead of us cancel before informed takers arrive, so a queue model fed at 1 Hz undercounts toxic fills.
- KXTEMPMIAH also fails the pivotality rule.

These markets are out of scope; they are not merely down-weighted.

**2.2 Market eligibility**, checked when the pair is admitted:
- status `open` or `active`;
- a linear 1-cent grid: every `price_ranges` step is 0.01 over [0.01, 0.99], or `price_level_structure = linear_cent` with no ranges;
- `close_time` at least 60 minutes after admission;
- no other admitted program on the same market. A second overlapping program is logged and never credited.

**2.3 Family.** The family is the series ticker, with one fold: if the ticker ends in a US state or DC code and the remainder is a series that has ever had a program, the family is the remainder. For example, `KXAAAGASDMI` folds to `KXAAAGASD`.
- "Ever had a program" means the saved history (`lip_history.json` ∪ `lip.json`) plus every recorded program list up to the moment of admission.
- The family label written to `universe.jsonl` at admission is the one used in the analysis.

**2.4 Admission.** It is evaluated at every refresh of the program list (every 300 s) between 12:25 and 00:45 UTC.
1. Candidates are eligible (market, program) pairs that are live now, or that start before 00:45, and that have not already been decided.
2. Sort them by `sha256("pmvl-lip-1.0.0|" + market_ticker + "|" + program_id)`, ascending. The salt is a plain fixed string, so it cannot have been searched for.
3. Admit in that order while the family has fewer than 5 admitted live pairs and fewer than 200 pairs are admitted in total.
4. An admitted pair stays admitted until its program period ends. It is never dropped because it is quiet, one-sided, losing, paused, closed or withdrawn. A withdrawn program pays 0 in LOW.
5. Every candidate is written to `universe.jsonl` with its decision and reason: `duration`, `rate`, `X1`–`X4`, `grid`, `status`, `close_time`, `overlap`, `cap_family` or `cap_total`.
6. Admission state is persisted across days, so a multi-day program keeps its place.
7. **Admitted seconds (H):** [max(start, first admission instant), end) intersected with each day's quoting window.

**2.5 What the rule selects.** These numbers come from replaying the rule over the saved history for the 7 days to 2026-09-24 02:50 UTC, inside the quoting window only. The grid filter is not applied in the replay, because the local database knows the grid for only 773 of 6,742 eligible program-markets (all 773 are `linear_cent`). Script: `artifacts/research/lip-1/design/universe_replay.py` (a design-time record; see its header).

| | p10 | p50 | p90 / max |
|---|---|---|---|
| Eligible live markets in the window, before the caps | 772 | 828 | 959 |
| Eligible families live | 29 | 51 | 60 |
| **Admitted markets** (5 per family, 200 in total) | 121 | **163** | 173 / 181 |
| Admitted series | | 55 | |
| Admitted pool, $/h | $641 | $698 | $835 |

- **Admitted market-hours:** about 1,900 per day per policy, or about 38,000 over 20 days.
- **Over the whole week:** 117 families, 143 series and 4,740 markets.
- **Program shape:** duration p10/p50/p90 of 16/16/24 h; pool $100 per period from p10 to p90; T = 1,000 for 6,720 of 6,742 programs.
- **Scope of the verdict:**
  - eligible programs carry 41.5% of all LIP dollars;
  - the window keeps 63.2% of those;
  - so the study speaks for about 26% of the LIP dollar flow;
  - among programs of 6 h or more paying at least $2/h, X2 and X3 each remove 3.6% of the dollars;
  - all intraday programs are removed by the duration rule.

---

## 3. Policy family

The family has four policies, and every policy quotes both sides.

| id | Placement on each side | Size per side S |
|---|---|---|
| J1 | Join others' best bid | T |
| I1 | Improve by 1 step when others' spread is ≥ 3 steps, otherwise join | T |
| J5 | Join | T/5 |
| I5 | Improve under the same rule, otherwise join | T/5 |

T/5 (200 when T = 1,000) is the smallest size at which an improving order sets Ref_H on its own.

**Declared constants.**

| Constant | Value |
|---|---|
| Quoting window | 12:30:00 to 00:45:00 UTC, in 13 segments: 12 of 60 minutes starting at 12:30, then 00:30 to 00:45 |
| Gate | the first 10 recorded books of a segment |
| Improve condition | others' spread ≥ 3 steps (at 2 steps, two improved sides would lock) |
| `min_spread` | 0.01. The `QuotePolicy` default of 0.02 would never quote a 1-tick book. |
| Close buffer | 30 minutes before `close_time` |
| Dead-man switch | 10 s without a fresh book |
| Replenish interval | at most once per 5 s per side |
| L_LOW | max(0.410 s, round trip of the book request that triggered the decision) |
| L_HIGH | 0.300 s |
| W_LOW | min(the documented basic-tier write limit (G2), 10) actions/s, burst W; every place, cancel and decrease costs 1 |
| W_HIGH | unlimited |

**Regimes.** REG_LOW = (L_LOW, W_LOW) and REG_HIGH = (L_HIGH, W_HIGH). Both regimes are simulated for every policy and fill scenario.

**3.1 Books and decision instants.** A decision is taken at the `received_at` of each recorded book for the market, using only data received at or before that moment. "Others' book" is always the recorded book, which never contains our paper orders, so the policy never prices off itself.

**3.2 Target price on each side.**
- **JOIN:** others' best bid on that side.
- **IMPROVE:** others' best bid + 0.01 when others' YES spread (best YES ask − best YES bid) is ≥ 0.03; otherwise join.
- **Hold exception (IMPROVE):** when others' best bid equals our resting price, because others have joined us, the target stays at our resting price.
- **Never cross:** the target must be below others' best ask on that side. With both sides improved, our own YES bid and YES ask stay at least 1 step apart.

**3.3 When a side is quotable.** Only when others have at least one bid on it and their best bid is below 0.99.

**3.4 Segment gate, which is not reactive.** At each segment start, and again after admission or after a dead-man recovery, the market is quoted for the rest of the segment only if the first 10 recorded books all show:
- both sides of others' book at T or more;
- best bids below 0.99;
- status open at the latest status poll;
- the program live;
- now before `close_time − 30 min`.

The gate is not re-checked inside the segment. Seconds it gates out stay in H as zeros.

**3.5 Order actions.**
- **Reprice.** When the target differs from the resting price, cancel every order on that side and place one order at the target with the allowed size. The new order is a new episode at the back of the queue.
- **Replenish.** At the first decision instant at or after t_fill + L (when the fill would become known), and at most once per 5 s per side, place one child order for the quantity filled since the last replenishment, at the current target, at the back of the queue, within the inventory limit. The parent order keeps its queue place.
- **Inventory.** N = YES held − NO held, because Kalshi nets the two. Allowed resting size is YES ≤ clamp(S − N, 0, S) and NO ≤ clamp(S + N, 0, S), so |N| ≤ S. Resting size above the allowance is cut with a decrease, which keeps queue priority. Nothing is taken as a taker before the day-end exit.

**3.6 Stops.** These are the only cancels:
- reprice;
- inventory decrease;
- `close_time − 30 min`;
- end of the program period;
- a status poll showing the market not open;
- the dead-man switch (no fresh book for 10 s), with re-entry through the gate;
- window end at 00:45:00.

It is banned to cancel or reprice because of trades, because the opposite side approaches, or because depth changes in any way other than a change in the target price.

**3.7 Latency and write budget.** Each policy account has a token-bucket write queue: FIFO across all markets, with coalescing, so a queued action whose desired state changes is replaced in place and never duplicated.
- An action decided at t is sent at t + queue delay and lands at t + queue delay + L.
- A new order is fillable and eligible for credit from its landing time.
- An order being cancelled stays fillable until the cancel lands. For credit, it stops at the decision time under LOW scoring and at the landing time under HIGH scoring.

**3.8 End-of-day flat.** A market's participation for the day ends at the earliest of 00:45:00, `close_time − 30 min` and the program period's end. At that point all orders are cancelled through the queue, and any residual |N| is sold at participation end + queue delay + L by walking others' recorded bids on the held side and paying the taker fee at the series multiplier. Each day's simulation for a market starts flat, so study days are independent.

---

## 4. Fill and queue model

**4.1 Order state.** Each order carries its side, its price p on that side's grid, its remaining size q, its queue ahead A, `live_from`, `fillable_until`, `credit_end` and its parent id. Partial fills reduce q in place, and later credit uses the reduced q.

**4.2 Eligible prints.** A print can fill our order only if:
- the aggressor took the other side (`MakerQuote.is_relevant`);
- it is not a block trade;
- its `traded_at`, corrected by the logged clock offset, falls inside the order's fillable window.

Prices are compared in YES coordinates through `MakerQuote.yes_price`, `at_level` and `through`. A NO bid at p is a YES ask at 1 − p. Prints inside [decision, landing) never fill the new order, but they can fill the old order until its cancel lands.

The clock uncertainty is ε = |measured offset| + 50 ms. HEAVY widens every fillable window by ε; THROUGH and FIFO narrow it by ε.

**4.3 Three fill scenarios.** Each is simulated under both regimes, giving 6 paths per policy.

| | THROUGH | FIFO | HEAVY |
|---|---|---|---|
| Initial queue ahead A₀ | not used | max(others' size at our price in the decision book, the same in the first book received ≥ `live_from`); arrivals during our latency are ahead of us | min of the same two |
| At-level relevant print of size x | no fill | consumes A first, then q | consumes A first, then q |
| Print through our price | fills min(q, x) | fills min(q, x); A → 0 | same |
| Level shrinks by more than the prints explain (tape covered) | not used | shrink was behind us: A = min(A, others' level size) | shrink was ahead of us: A −= excess |
| Unexplained shrink inside a tape gap or an audit mismatch | not used | no fill | counts as at-level prints |
| Others' opposite side at or through our price in a book | no fill | no fill | fills min(q, crossing size) at our price, dated at the previous book's `received_at` |
| Orphan: others' best bid on our side is below (our price − our improve offset) while we are live | no fill | no fill | the whole remaining q fills at our price, dated at the previous book's `received_at` |
| Growth at our level | queues behind us | queues behind us | queues behind us |

`TOUCHED` is never a fill. Its count is reported only so the gap is visible.

**4.4 Why this covers both directions.**
- In this strategy, resting time is paid and fills probably lose, so under-counting fills is the error that flatters. HEAVY is the adverse-fill stress case: cancels ahead advance us, the book can cross us, a best bid left behind is picked off, and fills are inferred during tape gaps.
- If fills turn out to win, fewer fills is the conservative reading, and THROUGH covers it.
- The decision takes every scenario under both regimes (§7).

**4.5 Consistency with the maker study.** On a fixture where no book update changes A, the last point of FIFO's `fill_path` must equal `evaluate_quote(...).filled_size` under `QUEUE_MODEL`, and THROUGH must equal `TRADED_THROUGH`.

---

## 5. LIP credit computation

This is computed for each admitted market, each admitted second k, each policy, each of the 6 paths and each envelope.

**5.1 Bracketing books.**
- B(k) is the union of three sets: the last book with `received_at` ≤ k; every book whose socket window [`sent_at`, `received_at`] intersects [k, k+1); and the first book with `sent_at` ≥ k+1.
- The span is `sent_at`(last) − `received_at`(first). With cadence c, the span limit is 2c + 1.5 s, which is 3.5 s at 1 Hz. If the span exceeds the limit, or either end is missing, the second is UNOBSERVED.
- Kalshi's snapshot instant lies inside B(k)'s coverage. LOW takes the minimum over B(k), so coarse sampling can only cost us credit.

**5.2 Observed and open.**
- **LOW:** an UNOBSERVED second earns 0. The market must be open at both bracketing status polls, the exchange must be active, and k must be before `close_time`; unknown counts as not open.
- **HIGH:** an UNOBSERVED second carries the last observed v forward, and unknown status counts as open.
- Fills keep applying during unobserved seconds, because the orders are still resting.

**5.3 Others' counterfactual book (LOW).** Starting from the recorded book:
- every print volume the path allocated to us (at-level, through, gap-inferred) is added back to other traders at the print's price, because in the counterfactual they would still be resting;
- on the opposite side, the quantity our cross fills and orphan fills consumed is removed, floored at 0.

**5.4 Target test.**
- **LOW:** both sides must reach T, and both best bids must be below 0.99, in both the recorded others-book and the counterfactual others-book.
- **HIGH:** the recorded others-book only.
- If the test fails, the second earns 0 (pivotality).

**5.5 Our credited order on each side.**
- **Coverage:**
  - LOW credits an order only if it is credit-eligible for all of [k, k+1), that is `live_from` ≤ k and `credit_end` ≥ k+1, at the smallest remaining q in the second;
  - HIGH credits the covered fraction of the second, times the time-averaged q.
- **Price (LOW):** min(actual price, the policy's target computed on b⁻). An order left above the book by others' cancels is credited at the target price.
- **Size (LOW):** q_eff = clamp(T − A_better − A_queue, 0, q). A_better is others' counterfactual size at strictly better prices; A_queue is this path's current queue ahead of us.
- **Size (HIGH):** the whole remaining q, on the whole-level reading.

**5.6 Share, per book b in B(k).**
- **LOW:**
  - M_min = min over {Ref_H, Ref_F} of DF^N × q_eff, at the credited price.
  - O_max = the maximum over {Ref_H, Ref_F} × {two constructions} of others' qualifying score, where the two constructions are:
    - (i) the counterfactual others-book without us;
    - (ii) the counterfactual others-book with us inserted, where any of others' depth our insertion pushes out of the qualifying set rejoins at the best level behind us with m = 1.
  - share_lo = M_min / (M_min + O_max), which is at or below every consistent reading because x/(x+y) rises in x and falls in y.
  - v_LOW(k) = min over b ∈ B(k) of [share_lo(YES) + share_lo(NO)].
- **HIGH:**
  - our full q at our actual price, recorded others' depth, displaced depth drops out;
  - the maximum share over the two references;
  - v_HIGH(k) = max over b ∈ B(k).

**5.7 Accrual.** a_k = R · v_k / (2 · N_total). Seconds outside the window, before admission or after participation ends stay in N_total and earn 0. The accrual is therefore exactly what a strategy quoting only in this window would earn.

**5.8 Payout per (market, program period, study day d).**
1. Sum the day's accrual: G_d = Σ_{k∈d} a_k.
2. Apply the cap: G_d ← min(G_d, cap − Σ_{d'<d} pay_{d'}).
3. pay_d = floor_to_cent(G_d) if G_d ≥ $1.00, otherwise 0.
4. Verification (§1 step 12): if the program is not verified, pay_d = 0 in LOW.
5. HIGH computes pay_d from max(G_H, G_F). The F denominator Σ(q_YES + q_NO) is estimated as N_total × the observed mean.

**5.9 Diagnostics, never decisive.**
- fraction of seconds counted, observed and credited;
- mean v while quoting;
- how often v(b⁻) ≠ v(b⁺);
- displaced depth, and credit taken from other traders;
- dollars lost to the $1 floor and to failed verification;
- share of pivotal seconds;
- the gap between the H and F scorings.

---

## 6. Metrics and valuation

Each (market, study day, policy, path, envelope) gets a value:

V = pay_d + fill value − maker fees − exit charge

Two valuations are computed on every path.

**MARK_1H.** It estimates the adverse selection of fills.
- **Each fill** of size q at price p on side s: q × (mid_s(t_f + 3,600 s) − p). mid_s is the side-s mid of others' recorded book, so there is no self-marking.
- **The market closes before t_f + 3,600 s:**
  - use the settlement payout if the market has settled by the verdict date;
  - otherwise LOW uses the held side's last best bid (0 if there is none) and HIGH the last two-sided mid.
- **No two-sided book in [t_f + 3,600 s, t_f + 3,900 s]:** LOW uses the held side's best bid (0 if there is none); HIGH uses the last mid.
- **Exit charge** at participation end t_e: (proceeds of walking others' bids on the held side for |N|) − |N| × mid_held(t_e) − taker fee.
  - Walk proceeds come from the first book received at or after t_e + delay + L.
  - Any remainder the walk cannot fill is priced at 0 in LOW and at mid in HIGH.

**DAY_EXIT.** The literal cash result of the daily-flat strategy:
- Σ(−p × q) over all fills;
- plus $1 for every YES/NO pair that nets;
- plus the proceeds of walking the book for the residual at participation end, minus the taker fee;
- if the market closed while holding inventory: the settlement payout if settled by the verdict date, otherwise the held side's last best bid (0 if none) in LOW and the last mid in HIGH.

**Fees.**
- **Maker fee:** `pricing.fees.kalshi_maker_fee` on every fill event in series whose recorded `fee_type` is `quadratic_with_maker_fees` (rate 0.0175 × the multiplier). An unknown fee type is charged at 0.0175 in LOW and not charged in HIGH.
- **Taker fee on the exit:** `taker_fee` at the recorded series multiplier.

**Reading combinations.** C = {THROUGH, FIFO, HEAVY} × {REG_LOW, REG_HIGH} × {MARK_1H, DAY_EXIT}, which is 12 per policy. Each combination is scored in LOW and in HIGH.

**Primary metric.** θ_{p,c,e} = ΣV / ΣH over all cells, with H from §2.4. Every admitted second counts, including seconds gated out, unobserved, closed, unquoted or unfilled. No second, hour or day is ever dropped.

**Secondary metrics.** These are descriptive only and are never decisive:
- LIP only, and fill value only, per market-hour;
- fills per market-hour;
- markouts per filled contract at 5, 30, 60, 300 and 3,600 s;
- the TOUCHED count;
- SETTLE: LIP plus Σ q × (payout − p) minus fees, restricted to markets whose scheduled close falls inside the study and which settled by the verdict date; markets left out are counted;
- the §5.9 diagnostics;
- reprices per hour, the distribution of write-queue delay, coalesced actions, and how often the budget binds;
- the latency distribution;
- how often the gate was open, and how often the I-policies had to join;
- capital: time-averaged Σ(resting q × p) + |N| at cost, per market, and dollars per $1,000 of capital per hour. Capital is not charged;
- a per-family table; break-even filled contracts per market-hour; cadence sensitivity (using every 5th book);
- unobserved-second, tape-gap and audit-mismatch fractions.

---

## 7. Statistics and decision table

**Cells.** A cell is (study day d, family f). It holds ΣH and, for each policy, reading combination and envelope, ΣV. A study day D runs from 12:30 UTC on D to 01:50 UTC on D+1.

**Point estimate.** θ̂ = ΣV / ΣH.

**Bootstrap.** A two-way pigeonhole bootstrap (Owen 2007) over dates and families: 10,000 draws, seed 20260924. Each draw takes date multiplicities c_d and family multiplicities c_f independently (multinomial, with replacement) and computes θ* = Σ c_d·c_f·V_df / Σ c_d·c_f·H_df. All (policy, combination, envelope) series share the same draws. SE is the standard deviation of θ*. Everything is computed in floats with no cent rounding, so `maker_study._interval` and `quantize_usd` are not used.

**Intervals.** They are t-calibrated for the small number of clusters: θ̂ ± t_{G−1, q} × SE, where G = min(counted days, families with H > 0).
- **Positive claims:** per-policy two-sided α = 0.0125, a Bonferroni correction over the 4 policies (98.75%). At G = 20, t₁₉ = 2.759.
- **Negative claims:** an intersection-union test over policies at a 95% two-sided level. At G = 20, t₁₉ = 2.093.
- The 12 reading combinations enter as an intersection (all must pass), so they need no further correction.
- Percentile intervals are reported descriptively.

**Leave one family out.** For each (p, c), remove the family with the largest ΣV_LOW and recompute θ̂_LOW.

**Counted day.** A day counts if the book lanes returned successful responses on at least 80% of the 44,100 one-second ticks in the quoting window, read from `lanes.jsonl`. This is outcome-blind. Days that do not count are still analysed with their zeros; they only fail to count toward the stopping point and toward G.

**Length and looks.**
- Day 1 is the first 12:30 UTC window after all the gates in §8 are committed.
- The study ends at the 20th counted day, or 30 calendar days after day 1, whichever comes first. There is no extension.
- **Exogenous stop:** a change in the rule text or program terms, 7 consecutive days with no admitted markets, loss of rate access, or a maker-isolation breach (§8). The study then ends, and the table is evaluated once at that point.
- **Interim reports** at counted days 5, 10 and 15 contain health and coverage only: no LIP credit and no dollar figures. They are labelled `INTERIM_NOT_A_DECISION`.
- Before the final analysis, replay code may be run on study days only as `--health-only`, which returns counts and exceptions.
- The verdict date is the last study day + 14 days, to allow settlements and `paid_out` checks.

**Minimum detectable effect.** Reported after the fact as (t_{G−1, 0.99375} + t_{G−1, 0.80}) × SE_LOW, which is ≈ 3.62 × SE at G = 20.

**Decision table.** It is evaluated once, at the final analysis.

| Verdict | Condition |
|---|---|
| **Precondition** (if it fails: `INSUFFICIENT_EVIDENCE`) | final analysis after the verdict date; at least 20 counted days; at least 10 families with H > 0; one version (`lip-1.0.0` / `lip-sim-1.0.0`); gates G1–G6 committed before day 1; isolation checks passed |
| `MAKER_POSITIVE_UNCONDITIONAL_EXPECTANCY` | For **some** policy p, and for **every** one of the 12 reading combinations c: the 98.75% t-calibrated two-way lower bound of θ_LOW(p, c) is > 0, **and** the leave-one-family-out θ̂_LOW(p, c) is > 0 |
| `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION` | For **every** policy p and **every** c: the 95% t-calibrated two-way upper bound of θ_HIGH(p, c) is < 0 |
| `INSUFFICIENT_EVIDENCE` | anything else |

**Why the two claims cannot both hold.** For every combination, LOW ≤ HIGH per market-day, so a LOW lower bound above 0 implies θ̂_HIGH > 0, which rules out the negative row. A test enforces this.

**Rules against gaming.**
- Analysis code is pinned by the `lip-sim-1.0.0` tag.
- If a bug fix after day 1 changes results, both results are reported. A positive verdict needs both to pass; a negative verdict needs both to pass.
- Any change to a declared parameter after day 1 bumps the version, and data from different versions are never pooled.
- There are no subgroup verdicts.

---

## 8. Recording plan with rate arithmetic and schedule

**Gates before day 1.** Recording before day 1 is `SHAKEDOWN`: it goes under `artifacts/forward/lip-shakedown/`, may be replayed to find bugs, and is never scored or pooled.

| Gate | Content |
|---|---|
| G1 | Fetch the current LIP regulatory notice (kalshi.com/regulatory/notices; this web fetch needs the user's OK), diff it against §1, and commit the diff and any amendment. |
| G2 | From docs.kalshi.com/getting_started/rate_limits: the read-token cost of a 100-ticker `/markets/orderbooks` call, the basic-tier write limit, and the cost of each write action. The write limit sets W_LOW. If a batch call costs more than 2 tokens, this recording plan is rewritten, not patched. |
| G3 | The user confirms the account holds no Market Maker Agreement, is not an IB or FCM customer, and is not an affiliate or employee. The US account is already confirmed. |
| G4 | All §10 code merged, tests green, a synthetic replay day passing, and the tag `lip-sim-1.0.0`. |
| G5 | A pre-flight of at least 60 minutes inside the window, then `cadence.json` committed by the ladder below. |
| G6 | The user installs the cron line. That is a configuration change, and it needs the user's approval. |

**Process.**
- Command: `.venv/bin/python -m pmvl_worker.lip_cli record-day`.
- `lip_cli` is not registered in `cli.py`, so the maker cron's import graph is byte-identical.
- Lock: `/tmp/pmvl-lip-day.lock.d` (mkdir).
- At start it waits while `/tmp/pmvl-maker-day.lock.d` exists or `pgrep -f "pmvl record sessions"` matches. It gives up at 13:30 UTC and logs the day as skipped.
- The code refuses to record between 02:00 and 12:25 UTC.

**Lanes.** Each lane has its own `LaneClient` (a subclass of `HttpClient` defined in the LIP package: `cache_ttl_seconds=0`, `_limiter = RateLimiter(rate, burst=1)`, one attempt for book lanes, per-minute counters, and a 429 log) and its own `KalshiProvider(client=...)`. `last_request_window` is a single attribute per client, so concurrent requests on a shared client would stamp the wrong socket window.

| Lane | Endpoint | Cadence | Limiter | req/s at p50 (max) |
|---|---|---|---|---|
| book-A | `/markets/orderbooks` with repeated `tickers`, first ≤ 100 admitted markets | 1 s grid with seeded ±250 ms phase jitter; a tick is skipped if the previous request is still in flight | 1.25, burst 1 | 1.0 |
| book-B | the same for the remaining ≤ 100 | same | 1.25, burst 1 | 1.0 (0 when 100 or fewer are admitted) |
| tape | global `/markets/trades?min_ts=⌊watermark⌋−5&limit=L` with cursor paging; deduplicated by `trade_id`; admitted tickers kept; every poll's watermark and page chain logged | every 2 s, plus pages | 1.5, burst 1 | 0.5 + 2λ/L; 0.5-1.5 at λ = 100-300 prints/s with L = 200 |
| status | `/markets?tickers=` (≤ 100 per request) and `/exchange/status` | every 30 s | 0.3, burst 1 | 0.1 |
| control | programs `status=active` every 300 s and `upcoming` every 900 s; metadata for new candidates; `/series/{s}` once per series per day; tape audit (per-ticker `/markets/trades` for 2 hash-chosen admitted tickers every 300 s); `paid_out` and `closed` program lists and settlements at 01:50 | | 0.3, burst 1 | ≈ 0.05 |
| **LIP total** | | | caps sum to **4.6** | **≈ 2.6-3.6** |

**Combined load.**
- The windows are disjoint, so the maker recorder is not running.
- The hourly crypto recorder ends at 07:03 UTC today.
- The collector's cap is 9 req/s.
- At the caps: 4.6 + 9 = 13.6 req/s, under the 20 req/s basic tier. Estimated actual use is 4-7 req/s.

**Pre-flight ladder**, committed to `cadence.json`:
- L = 1000 if `limit=1000` returns more than 200 rows; otherwise L = 200.
- If `min_ts` is not honoured, page by cursor until the first row older than the watermark − 5 s.
- **Fallback TAPE_PT**, applied if the tape's p95 cost exceeds 1.5 req/s, or if the pre-flight audit finds any print missing from the global feed:
  - per-ticker tape every 60 s with per-ticker `min_ts` watermarks;
  - the total cap cut to 100 (the family cap stays 5);
  - one book lane;
  - about 2.9 req/s in total.
- If any 429 appears at the planned rates, or G2 finds a cost of more than 1 and up to 2 tokens per batch call, the book cadence is 2 s for the whole study (span limit 5.5 s).

**During the study.**
- **429s.** Any 429 halves that lane's rate for 10 minutes, and the change is logged. If 429s exceed 1% of a 10-minute window's requests, the book lanes pause until the end of the hour; those seconds become UNOBSERVED zeros. The tape lane never polls less often than every 5 s.
- **Tape coverage.** An interval is complete iff each poll started at or before the previous poll's newest print and its cursor chain ended with no error and without hitting the page cap. Incomplete intervals and audit mismatches are tape gaps; they trigger HEAVY's gap rule and never drop any data.
- **Clock.** The offset is measured with `sntp` every 10 minutes and written to `clock.jsonl`.

**Files.**
- Layout: `artifacts/forward/lip-daily/<D>/segment-HH/`.
- Contents: `programs.jsonl` (diffs plus a full list each hour), `universe.jsonl`, `books.jsonl`, `trades.jsonl`, `tape_polls.jsonl`, `status.jsonl`, `markets.jsonl`, `series.jsonl`, `lanes.jsonl`, `clock.jsonl`.
- `books.jsonl` has one line per request: lane, `sent_at`, `received_at`, round trip, and bids only, truncated at cumulative 2T with the count and size of dropped levels. A market is written only when it changes, with a keyframe every 60 s.
- Every line is stamped `lip-1.0.0`.
- Logs are hash-chained through `writer.AppendOnlyLog`, gzipped every hour, and the chain continues across segments.
- Per day: `study.json` (the pre-registration commit, the versions, the hash of `cadence.json`, and a shakedown or study tag) and `health.json`.
- Storage is estimated below 1 GB/day; an alarm fires at 5 GB/day; 275 GB are free.

**Daily schedule (UTC; the cron runs on local time, UTC+8).**

| Time | Job |
|---|---|
| 02:10 to about 12:25 | Maker cron, unchanged |
| 12:25 (20:25 local) | `scripts/record_lip_day.sh`: takes the lock, waits for the maker lock and process to clear, gives up at 13:30 |
| 12:25-12:30 | Program lists, admission, status for candidates |
| 12:30-00:45 | Recording in the quoting window; admission continues |
| 00:45-01:50 | Recording continues (1-hour marks for the last fills); no new admissions |
| 01:50-02:00 | Final tape drain; `paid_out` and `closed` program lists; settlement lookups written to `artifacts/forward/lip-settlements.jsonl`; `verify_log`; gzip; `health.json`. Hard stop at 02:00. |
| 02:00-02:10 | Slack before the maker cron |

**Maker isolation guard.** Every day, check that no LIP request in `lanes.jsonl` falls within ±60 s of any maker session in the maker manifests. A breach stops the LIP study (the exogenous stop rule in §7 applies). The maker sessions' snapshot counts (baseline 1,264-1,447) and skew are also reported.

---

## 9. Not measured, with bias direction

| Omission | Direction |
|---|---|
| Kalshi's target test may leave out designated-LP, MMA or non-US depth, so fewer snapshots would count than we simulate | **Flatters LIP; no envelope brackets it** |
| Competitors reacting to our size or improvement: other traders add depth or improve past us | Flatters our share; LOW models only displaced depth rejoining behind us |
| Our own order drawing extra, more informed taker flow | Flatters; HEAVY's orphan and cross fills bracket part of it |
| Revocation under the abuse clause, program edits, discretionary non-payment | Flatters; partly mitigated by LOW's `paid_out` check |
| Book changes between reads (more than one change inside a bracket) | Probably flatters; LOW's minimum covers only the states we observed |
| Tail latency beyond the measured round trip; order rejects; post-only rejects | Flatters slightly; LOW enforces the documented write limit |
| Prints missed inside intervals the tape marks as covered | Flatters (fewer fills); bounded by the audit |
| Mid not a martingale beyond 1 h | Could flatter MARK_1H; DAY_EXIT is literal cash and is required too |
| Cost of capital, taxes, SSN withholding | Flatters; not charged; capital is reported per $1,000 |
| Ineligible depth kept in the denominator; minimum over bracketing books; whole-second credit; floor per market-day; unverified payouts pay 0; unobserved and closed seconds counted | Understates the reward (conservative) |
| REST polling and 410 ms latency instead of a WebSocket feed | Pessimistic against a faster real system; REG_HIGH brackets part of it |
| Recorder outages kept as zeros | Shrinks the estimate toward 0 in both directions |
| X1-X4, programs under 6 h or under $2/h, sub-cent grids, time outside 12:30-00:45 UTC (about 74% of LIP dollars) | Scope, not bias: the verdict does not extend to them |

---

## 10. Implementation plan and test list

**Must stay unchanged.** A test compares the sha256 of each of these against the pre-registration commit, and `STRATEGY_VERSION == "1.1.0"`:
- `recording/recorder.py`, `schema.py`, `writer.py`, `reader.py`;
- `providers/http.py`, `providers/kalshi.py`;
- `arbitrage/maker_experiment.py`, `maker_study.py`;
- `services/worker/.../cli.py`, `record_cli.py`, `replay_cli.py`;
- `scripts/record_maker_day.sh`.

**Reused by import:**
- `AppendOnlyLog`, `verify_log`;
- `KalshiProvider.get_orderbooks_synchronized`, `parse_orderbook`, `tick_from_price_ranges`;
- `recording.recorder._parse_trade`;
- `MakerQuote` (`yes_price`, `is_relevant`, `at_level`, `through`) and `evaluate_quote` (for the cross-test);
- `pricing.fees.kalshi_maker_fee`, `taker_fee`.

**New package `packages/market-normalization/src/pmvl_markets/lip/`:**
- **`version.py`:** `LIP_RECORDER_VERSION`, `LIP_SIM_VERSION`, `SAMPLE_SALT = "pmvl-lip-1.0.0"`.
- **`params.py`:** a frozen `LipParams` holding every constant in §§2-8, plus `FAST_INFO_PATTERNS` (X1-X4 verbatim).
- **`programs.py`:**
  - `ProgramPeriod` (id, market_ticker, series, start, end, R: Decimal, T: Decimal, DF, cap, paid_out) with `.from_api(row)`, `.n_total_seconds()` (floor and ceil) and `.rate_per_hour()`;
  - `family_of(series, known_series)`;
  - `ProgramRegistry`, which diffs program lists (added, removed, terms changed, `first_seen_at`, `paid_out_seen_at`).
- **`universe.py`:**
  - `fast_info_class(series) -> str | None`;
  - `program_eligible(p) -> (bool, reason)`;
  - `market_eligible(meta, at) -> (bool, reason)`;
  - `rank_key(p)`;
  - `AdmissionState.refresh(programs, market_meta, at) -> list[AdmissionEvent]`, whose signature takes no books or trades; and `.admitted_seconds(day)`.
- **`grid.py`:** `is_linear_cent(meta)`, `steps(price, ref)`, `HIGHEST = 99` (prices are integer cents).
- **`http.py`:** `LaneClient(HttpClient)` and `lane_provider(name, rate) -> KalshiProvider`.
- **`store.py`:** a bids-only, truncated, change-only-plus-keyframe serializer; `SegmentWriter` (wraps `AppendOnlyLog`, gzip rotation, the chain carried across segments); `SegmentReader`.
- **`recorder.py`:** `LipRecorder` with the lanes `book_lane(i)`, `tape_lane()`, `status_lane()` and `control_lane()`; `TickScheduler` (1 s grid, seeded jitter, skip if in flight); a window guard; `final_drain()`; a clock sampler.
- **`reader.py`:** `load_day(dir) -> DayData`. For each market:
  - `BookSeries`: numpy arrays `sent`, `recv`, `rtt`, plus CSR level arrays (`yes_px int16`, `yes_sz float64`, `no_px`, `no_sz`, offsets), with `.before(t)`, `.after(t)` and `.bracket(k)`;
  - `TradeSeries`: `ts`, `yes_px`, `size`, `taker_yes`, `block`, `trade_id`;
  - `TapeCoverage`: complete intervals, with audit mismatches marked;
  - `StatusSeries`.
- **`scoring.py`:**
  - `side_qualifies(levels, T)`, `qualifying_set(levels, T)`, `ref_price(levels, T, rule)`, `multiplier(px, ref, DF)`;
  - `counterfactual_others(book, absorbed, crossed)`;
  - `share_low(...)`, `share_high(...)`;
  - `second_credit(bracket, state, program, envelope) -> SecondCredit(k, observed, open, counted, v, pivotal, displaced)`;
  - `daily_payout(G_d, cap_left)`, `verified(registry, program_id, verdict_at, envelope)`.
- **`fills.py`:** `FillScenario {THROUGH, FIFO, HEAVY}`, `OrderState`, `apply_print`, `apply_book` (shrink, cross, orphan), `apply_tape_gap`, `fill_path`.
- **`policy.py`:** `LipPolicy(name, placement, size_frac)`, `POLICIES = (J1, I1, J5, I5)`, `target_price(others, side, resting)`, `allowed_sizes(N, S)`, `SegmentGate`.
- **`simulator.py`:**
  - `Regime` (REG_LOW, REG_HIGH);
  - `WriteQueue` (token bucket, FIFO, coalescing);
  - `simulate(market_day, policy, scenario, regime) -> SimPath`, an event-driven merge of books, prints, status and coverage;
  - `SimPath` holds the ledgers of orders (id, side, px, size, decided, live_from, credit_end, fillable_until, parent), fills (order, t, px, qty, kind), credit intervals, inventory, and writes (decided, sent, landed, kind, coalesced);
  - `score_path(path, day, envelope) -> per-second v`;
  - markets run in parallel under multiprocessing, with a target of 2 h or less per study day.
- **`valuation.py`:** `mark_1h(path, day, settlements, envelope)`, `day_exit(...)`, `walk_bids(levels, qty)`, fee helpers.
- **`study.py`:** `Cell(date, family, hours, V[p][c][e])`, `build_cells`, `pigeonhole_bootstrap(cells, B, seed)`, `t_interval(theta, se, G, alpha)`, `lofo`, `decide(cells, counted_days, final) -> LipVerdict`, and the `INTERIM_NOT_A_DECISION` label.
- **`health.py`:** outcome-blind health (coverage, lane rates, 429s, tape completeness, storage). It may not import `scoring`, `fills`, `simulator` or `valuation`.

**Other new files:**
- `services/worker/src/pmvl_worker/lip_cli.py`, a typer app with the commands `preflight`, `record-day`, `health`, `settlements`, `replay-day [--health-only]` and `study --final`;
- `scripts/record_lip_day.sh`;
- `docs/lip-preregistration.md` (this document) and `cadence.json`.

**Order of work.**
1. Commit the pre-registration.
2. `programs`, `universe`, `grid`, `http`, `store`, `recorder`, a minimal `reader`, `lip_cli record-day` and `preflight`, the script, and their tests.
3. Shakedown and pre-flight in tonight's 12:30 window.
4. `scoring`, `fills`, `policy`, `simulator`, `valuation`, `study`, `health`, their tests, and the synthetic replay day.
5. Close G1-G6 and tag `lip-sim-1.0.0`.
6. Day 1 is the next 12:30 UTC window, 2026-09-25 at the earliest.

**Tests.**

*Scoring* (`test_lip_scoring.py`)
1. The help-center example: a 20% share × $100 × 8,000/10,000 snapshots = $16.
2. All traders' scores sum to 2 on a counted snapshot.
3. A best bid at 99c empties that side.
4. A side at 900 from others plus our 1,000 scores v = 0. A KXTEMPMIAH-shaped book (YES side empty) scores 0 in every second.
5. Joining behind T or more at the best level: LOW gives 0, HIGH gives more than 0.
6. When others improve past us, the minimum over the two references binds.
7. A stale order above the book is credited at the target price.
8. Our insertion never lowers O_max; displaced depth rejoins behind us.
9. Absorbed prints are added back, and crossed asks can make the opposite side fail the target test in LOW.
10. Property test on random books and paths: LOW ≤ HIGH for every second and every market-day.
11. An order placed at k + 0.2 s, or cancelled at k + 0.9 s, earns nothing for second k in LOW. Credit after a partial fill uses the reduced q.
12. A span of more than 3.5 s makes the second UNOBSERVED. Closed and unknown seconds stay in N_total. N_total uses floor and ceil on microsecond timestamps.
13. An accrual of $0.99 pays 0; $1.005 pays $1.00. The cap applies. The per-day floor never pays more than the per-period floor (property test).
14. An unverified program pays 0 in LOW. A withdrawn program pays 0 in both envelopes.

*Fills and simulator* (`test_lip_fills.py`, `test_lip_simulator.py`)

15. A print inside [decision, landing) never fills the new order and does fill the old one; credit stops at the cancel decision in LOW.
16. An at-level print smaller than the queue ahead fills nothing.
17. Fills are ordered THROUGH ≤ FIFO ≤ HEAVY on the same path inputs.
18. HEAVY's cross fill, orphan fill and gap inference; FIFO and THROUGH have none of them.
19. Block trades are ignored.
20. Cross-test: the last point of `fill_path` equals `evaluate_quote` under QUEUE_MODEL and TRADED_THROUGH.
21. A reprice resets the queue; a child order joins at the back and the parent keeps its place.
22. The inventory limit is never exceeded, and it uses decrease, not cancel.
23. I-policies never self-cross (property test).
24. The hold rule when others join an improved price.
25. The gate needs 10 books and is never re-checked inside a segment.
26. No quote while others' book is below T at the gate.
27. The dead-man switch fires after 10 s.
28. Stops at `close_time − 30 min`, at the program end and at 00:45.
29. More than W actions in a second are queued and delay the orders; coalescing never duplicates an action.
30. No look-ahead: perturbing any book or print received after t leaves every decision at or before t unchanged.

*Valuation* (`test_lip_valuation.py`)

31. The 1-hour mark uses others' mid; the LOW and HIGH fallbacks.
32. The exit walk pays the taker fee; an unfillable remainder is valued at 0 in LOW.
33. DAY_EXIT cash identity: pairs net to $1.
34. An unknown fee type is charged in LOW only; a maker-fee series is charged.
35. SETTLE never reaches `decide`.

*Study* (`test_lip_study.py`)

36. A market that is never quoted adds hours and 0 value, and removing it changes θ.
37. Fewer than 20 counted days, or fewer than 10 families, gives `INSUFFICIENT_EVIDENCE`.
38. The pigeonhole SE matches the analytic two-way variance on synthetic data within tolerance.
39. t-calibration: G = 20 gives 2.759 and 2.093.
40. Every decision row is exercised, including a combination positive under only 11 of 12 readings, which must not decide, and a positive that the leave-one-family-out check reverses.
41. Every report before the final one is `INTERIM_NOT_A_DECISION` and carries no dollar field.
42. Mixed versions are refused.
43. The positive and negative verdicts are mutually exclusive.
44. **Canaries:**
    - a synthetic small-pool market where every fill drifts 2c against us never reaches POSITIVE;
    - a no-taker market with a large pool reaches POSITIVE only through LOW credit.

*Universe and recorder* (`test_lip_universe.py`, `test_lip_recorder.py`)

45. Admission is identical when book and trade files are removed or shuffled. The hash order and the family fold are deterministic.
46. Every rejected candidate is logged with its reason; caps of 5 per family and 200 in total.
47. The X1-X4 patterns match the frozen fixture list.
48. Each lane has its own client, and concurrent lanes keep their own socket windows (regression test for the shared `last_request_window`).
49. The limiter never exceeds its lane cap; the scheduler skips a tick while a request is in flight.
50. Each tape poll starts at or before the previous poll's newest print; a page-cap hit is flagged; the final drain runs.
51. Diffs plus keyframes round-trip to the full book; the hash chain verifies across gzip segments.
52. No line is stamped 1.1.0; the sha256 values of the maker files are unchanged; importing `pmvl_worker.cli` does not import `lip_cli`.
53. The recorder refuses 02:00-12:25 UTC and waits on the maker lock.
54. `health.py` cannot import `scoring`, `fills`, `simulator` or `valuation`.

---

## Appendix A: judge flaws and their fixes

| Flaw | Fix |
|---|---|
| Write budget not enforced (operational) | Token-bucket write queue with coalescing; W_LOW from G2 (§3.7) |
| The positive decision used a flattering model and pessimistic variants only needed a point estimate (operational) | All 12 combinations must have a t-calibrated LOW lower bound > 0 (§7) |
| Decision at 10 clusters with percentile intervals; cent quantization | One final look, at least 20 counted days, t_{G−1}-calibrated two-way SE, floats (§7) |
| Tape and quality gates dropped outcome-correlated data (rigor, operational) | Nothing is dropped; gaps feed HEAVY's inference; zeros stay in H (§§4, 6, 7) |
| Code written after data existed (operational) | Day 1 only after the `lip-sim-1.0.0` freeze; shakedown is never scored; interims carry no dollars (§§7, 8) |
| SETTLE could decide on outcome-selected markets (rigor) | SETTLE is descriptive; DAY_EXIT (literal cash) plus MARK_1H are required (§6) |
| 24/7 recording overlapped the maker study (rigor) | Disjoint window 12:30-01:50, maker-lock wait, isolation guard (§8) |
| Shared `last_request_window` mis-stamped concurrent books (rigor, realism) | One `LaneClient` and one provider per lane; regression test 48 |
| A 300 s mark only (realism) | 1-hour mark plus exit charge, and DAY_EXIT (§6) |
| Reactive withdraw gate (realism) | Segment gate at the segment start, never re-checked inside it (§3.4) |
| No family fold; gas ladders counted as independent (realism, operational) | Family fold, 5 per family, date × family bootstrap, leave-one-family-out (§§2, 7) |
| Others' depth after our paper fills set in our favour (rigor, operational) | LOW counterfactual book: absorbed prints added back, crossed asks removed (§5.3) |
| DLP or MMA depth in the target test unmodelled (all designs) | Listed as an unbracketed optimistic bias (§9) |
| LOW ≤ HIGH not guaranteed across regimes | Both regimes are in every combination; per-combination LOW ≤ HIGH is tested (§§6, 7) |