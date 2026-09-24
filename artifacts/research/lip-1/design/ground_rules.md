## Kalshi LIP scoring rules for the shadow simulator

### Sources
All saved sources are in `/private/tmp/claude-501/-Users-xuao-Documents-2025-----AI-Projects-prediction-market-value-lab/6306567b-fa12-4743-9bb1-c4f3881d7852/scratchpad/`.

- **F** is the KalshiEX notice to the CFTC dated 8 Aug 2025, Appendix A (`lip_rules.txt`).
- **H** is help-center article 13823851, last modified 2026-09-03 (`lip_help.html`, `lip_help.txt`).
- **W** is help article 16076644, "Liquidity and Volume Incentive Programs: Where to Find Them" (`help_16076644-*.html`).
- **A** is the docs.kalshi.com `GetIncentivePrograms` schema (`docs_incentives.md`).
- **D** is the rest of the Kalshi docs used here: `docs_orderbook_responses.md`, `docs_order_direction.md`, `docs_fixed_point.md`, `docs_subaccounts.md`.

**Which text governs.** F says the program runs "until the earlier of August 1, 2026" or an amendment. Programs are live today and H shows "Program end: January 1, 2027", so a later notice exists that I did not read; kalshi.com/regulatory/notices is outside the allowed domains. Rule applied: follow H wherever it speaks, F where H is silent, and where both readings are open, compute both and credit our order the lower one. Every change H makes relative to F lowers our payout or leaves it the same (shown in step 6). Before pre-registering, fetch the current notice and diff it against this spec.

### Inputs for each program
- **Pool:** R = `period_reward` / 10,000 dollars.
- **Target:** T = `target_size_fp`. The snapshot has 300, 500, 1000 and 2500.
- **Discount factor:** DF = `discount_factor_bps` / 10,000. It is 0.5, or 0.4 for 20 programs.
- **Per-account cap:** `max_reward_per_account` / 10,000 when it is set. This field is in A but null in all 6,640 rows of `lip.json`.
- **Price grid:** the market's `price_ranges`.
- **Market status** over time.
- **Recorded books** and the state of our paper orders.

### 1. Snapshots
1.1. **Which seconds count.** Use every second k from floor(start) to ceil(end)−1, so N_total = ceil(end) − floor(start). F says periods start and end on whole seconds, but the API timestamps have microseconds (for example `02:02:23.636162Z` to `03:59:59.999999Z`). Taking the widest count is the conservative reading.

1.2. **When the snapshot is taken.** Both texts say one snapshot per second at a uniformly random instant inside it. Credit our order in second k only if it rests for the whole of [k, k+1): from the placement ack (`placed_at + placement_latency_ms`) until the cancel is sent or it fills. Crediting the covered fraction of the second would be the expected-value alternative; it pays more.

1.3. **Which book to use.** Use the latest recorded book with `response_received_at ≤ k`, as long as it is no older than S_max (pre-register it, for example twice the median recorder cycle). If no book is fresh enough, the second is UNOBSERVED.

1.4. **Excluded seconds.** These stay in N_total and earn us nothing:
- the market is not open (closed, paused, before it opens, after it closes, exchange maintenance);
- the status is unknown;
- the second is UNOBSERVED.

F takes no snapshot while the market is closed. H takes one and excludes it, which lowers the payout through the exclusion factor. Implement H; it is the conservative choice.

### 2. Each side: which bids qualify
**How NO bids relate to the YES book.** The YES side is the yes bids, priced in YES. The NO side is the no bids, priced in NO. F: "a yes ask is counted as a bid for no".
- A resting sell-YES at p and a buy-NO at 1−p are the same book entry: `book_side` ask ≡ no. It appears in `no_dollars` at 1−p and scores only on the NO side.
- The best NO bid is 1 − the best YES ask. Walking down the NO bids is walking up the YES asks, and ticks are counted in NO prices.
- A two-sided quote (a YES bid plus a YES ask) puts one order on each side.
- In the repo, `book.bids(Side.NO)` holds NO bids in NO prices, best first.

2.1. **Empty side.** If a side has no bids, or its best bid is at or above the highest valid price on the grid (0.99 on `linear_cent`), nothing on that side qualifies. This is from F; H is silent. Implement F; it can only exclude.

2.2. **Walking the levels.** Add our paper orders to the book at their prices. Then, starting from the best bid, add WHOLE levels (every order, at full size) until the cumulative size is ≥ T. If the whole side is < T, no bid on it qualifies. The last level counts in full, so qualifying size can exceed T.
- F spells this out.
- H's wording ("every resting order that helps reach the Target Size") is ambiguous.
- W's grey dot ("better-priced depth already reaches the Target Size") is a rule about levels.
- **Primary:** whole levels.
- **Conservative bound:** credit our order only q_eff = clamp(T − A, 0, q_remaining). A is the size at strictly better prices plus our queue-ahead at our own level: QUEUE_MODEL's static `queue_ahead`, less trades at the level, capped at the level's current size. Other traders keep their full whole-level sizes in the denominator.
  - This bound is ≤ both the whole-level reading and a consistent price-time reading, because x·w/(C + x·w) increases with x.
  - It matters for JOIN quotes. A join behind ≥ T at the best level gets 0 under price-time and full credit under whole levels. An IMPROVE quote has A = 0.

2.3. **The target test ignores our orders.** Decide whether each side reaches T, and so whether the snapshot is eligible, on the recorded book, which does not contain our paper orders. This carries out the out-of-scope decision: completing a starved side earns nothing. Steps 2.2, 3 and 4 do include our orders.

### 3. Reference price and distance multiplier
3.1. **Reference price (Ref).**
- H: the first level, walking down from the best bid with our orders included, where cumulative size is ≥ T/5. That is 200 contracts for T = 1000 and 60 for T = 300.
- F: the best bid.
- **Implement H.**
- **Conservative choice:** credit our order the per-snapshot minimum of the H and F results. This is valid because the payout adds up snapshot by snapshot (step 6).
- For an order at the best bid, H never pays more than F: other traders' multipliers rise and ours stays 1. If someone has improved past us, H can pay us more, which is why the minimum is taken.

3.2. **Multiplier.** m = DF^N, where N = max(0, number of grid steps from the bid's price up to Ref). N is a distance in price, not a count of occupied levels. Bids at or above Ref get 1.0 (H). F's printed exponent "(Reference Price − Price(bid))" is shorthand; F defines N as ticks.

3.3. **Sub-cent grids.** On tapered or `deci_cent` grids, "tick" could mean grid steps or cents.
- **Primary:** grid steps from `price_ranges`.
- **Conservative choice:** take the minimum over the two readings, or tag and exclude snapshots where any qualifying level sits in a sub-cent band.
- On every listed grid except `deci_cent`, the step is 1c between 0.10 and 0.90.

### 4. Normalising each side
score(bid) = m × remaining size. Sizes are fixed-point with 0.01 contract granularity.

share_side = Σ(our qualifying scores) / Σ(all qualifying scores on that side).

- **Remaining size:** the order's size minus QUEUE_MODEL fills up to the start of second k.
- **Crossed quotes:** if the recorded opposite side trades through our paper price or crosses it, the order stops resting for reward purposes from that record on.
- **Ineligible traders' orders:** keep them in the denominator. This covers Market Maker Agreement holders, affiliates and non-US users. Kalshi runs designated liquidity providers in KXTEMP*H, KXRAIN and the crypto and FX 15-minute series.
  - Neither text says whether these orders are dropped before normalising, and public data cannot identify them. Keeping them is conservative: their unpaid share is not handed back to us.
  - Keeping them in the target test (step 5) is not conservative. That part is an unmodelled risk.

### 5. Both sides must reach target
- H: a snapshot counts only if the market is open AND both the YES and NO sides reach T (tested as in 2.3). Otherwise it counts for nobody. **Implement H.**
- F: each side scores on its own, so a one-sided snapshot totals 1.0.

### 6. From snapshots to a payout
6.1. **Our score for second k:** v_k = 1[k counts] × (share_YES + share_NO), between 0 and 2. On every counted snapshot all traders' scores sum to exactly 2.

6.2. **Period score:** P = Σv_k / (2 · N_nonexcluded).

6.3. **Gross payout (H):** G = P × R × (N_nonexcluded / N_total) = **R · Σv_k / (2 · N_total)**.
- The payout adds up second by second: each second is worth R / (2·N_total) per unit of share.
- A one-sided quote earns only its own side's share.
- **Under F:** G_F = R · Σv_k / Σ_k(q_YES + q_NO), where q = 1 if that side qualifies, and there is no exclusion factor. With the reference rule held fixed, G_F ≥ G_H always, so H is the conservative choice.

6.4. **Cap:** if a per-account cap is set, G = min(G, cap).

6.5. **Minimum and rounding.** If G ≥ $1.00, pay floor(100·G)/100; otherwise pay 0.
- The $1 test applies to each program separately (W: "a final reward below $1 for an individual program is not paid").
- Apply it after the exclusion factor and the cap. That is conservative; F applies it to P × R.
- Accumulate over the whole program period and apply the threshold only when the period ends; payment comes after the period.
- The threshold needs the mean v_k ≥ 2/R over the whole period. That is 0.10 for a $20 period and 0.02 for $100; the median period pool is $100 and the p10 is $20.

6.6. **Overlapping periods** are scored separately, each with its own threshold (F allows overlaps; none exist in `lip.json` today).

### What counts as the same user, and who is eligible
- **Same user.** F sums all bids "submitted by a single user", across both sides and any number of orders. A caps per "account". Sub-users' orders belong to the account (D). Neither text says whether subaccounts 0–63 are pooled.
  - Simulate one account and one subaccount (0).
  - If orders were ever split across subaccounts, the conservative choice is the $1 test and cent flooring per subaccount, with the cap on the total.
  - Multiple accounts are out of scope.
- **Eligibility** is the union of both lists. Excluded:
  - affiliates and employees;
  - Market Maker Agreement holders (F only; the Volume Incentive article also excludes them);
  - IB and FCM customers;
  - non-US users (H).

  An SSN is required above the IRS threshold (H). The user has confirmed a US account.
- **Abuse clause.** Both texts let Kalshi revoke participation. Therefore:
  - only bona fide join and improve quotes;
  - the same paper orders carry both the fills and the adverse selection;
  - a quote rule that cancels when aggressive orders approach is banned;
  - the pivotal rule in 2.3.

### Where the two texts differ, and what to implement
| # | Point | F | H | Implement | Conservative choice |
|---|---|---|---|---|---|
| 1 | Reference price | best bid | first level where cumulative size ≥ T/5 | H | per-snapshot min of F and H |
| 2 | Two-sided requirement | none | both sides must reach T | H | H |
| 3 | Exclusion factor | none | × non-excluded / total | H | H |
| 4 | Market closed | no snapshot | snapshot excluded, still in total | H | H |
| 5 | Qualifying bids | whole levels | ambiguous | whole levels | our size truncated |
| 6 | Best bid at the highest price | side empty | silent | F | F |
| 7 | Tick on sub-cent grids | silent | silent | grid steps | min, or exclude |
| 8 | Ineligible traders' depth | silent | silent | count it | count it in the denominator |
| 9 | Our order makes the target | pays | pays | exclude (project decision) | exclude |
| 10 | $1 minimum | on P×R | on the payout, per program | after factor and cap | same |
| 11 | Rounding | floor to the cent | floor to the cent | floor | floor |
| 12 | Period timestamps | whole seconds | silent | floor start, ceil end | same |
| 13 | Snapshot instant | random in the second | random in the second | whole second covered | same |

### Gaps in the existing exploratory scripts
Both are in `/Users/xuao/Projects/prediction-market-value-lab/artifacts/research/lip-1/`.

**`share_now.py`**
- Tick is hard-coded at 0.01 with `int()` flooring.
- It adds our order before testing whether a side reaches target, so up to 60 of the 400 programs may be counted only because our 1,000-lot brings a side to target.
- It has no check for a best bid at the highest price.
- Its `rate·Σshares/2` formula matches 6.3.

**`explore_recorded.py`**
- It also tests the target with our order included.
- It already truncates our order as in 2.2 and works out other traders' qualifying levels without our order, which is more conservative still.
- It weights by time rather than by whole seconds and has no staleness limit.
- Tick is again hard-coded at 0.01.

### Unmodelled
- Other liquidity-seeking traders reacting to our quotes, for example improving past them.
- Kalshi's own scoring cannot be checked without real orders.
