{
 "scores": [
  {
   "design": "rigor",
   "validity": 7.5,
   "realism": 7.5,
   "feasibility": 5,
   "conservatism": 8,
   "total": 28
  },
  {
   "design": "realism",
   "validity": 6.5,
   "realism": 8,
   "feasibility": 6,
   "conservatism": 7,
   "total": 27.5
  },
  {
   "design": "operational",
   "validity": 5,
   "realism": 5,
   "feasibility": 7,
   "conservatism": 5,
   "total": 22
  }
 ],
 "winner": "rigor",
 "fatal_flaws": [
  "[operational, FATAL] The write budget is not enforced. The simulator reprices 150-200 markets x 2 sides at 1 Hz and only reports the order actions per second it needed. A real account under the basic-tier write limit would rest stale for longer, so it would take more adverse and orphan fills and earn fewer credited seconds. The simulated profit may not be achievable. Fix: rigor's or realism's FIFO token-bucket write queue, charged to latency.",
  "[operational, FATAL] The positive verdict's intervals use the most flattering readings: a static back-of-queue QUEUE_MODEL (cancels ahead never advance us, so fills are undercounted while fills lose money), whole-level credit and a mid mark. The pessimistic variants A1 FRONT_OF_QUEUE, A2 ORPHAN_FILL, A3 TRUNCATED and A4 EXECUTABLE_EXIT only need a point estimate > 0. So POSITIVE can be declared when none of the adverse-fill-heavy readings is significantly above zero.",
  "[operational, FATAL] Look 1 comes at 10 valid days, using the repo's percentile cluster bootstrap (maker_study._interval, a plain order-statistic percentile) at 99.75%. With G=10 the percentile interval ignores the t9 tail and the (G-1)/G variance shrink. The one-sided error is roughly 0.9% per policy against a nominal 0.125%, so over 4 policies the family-wise false-positive rate at look 1 alone is several times the stated alpha.",
  "[operational, serious] Days are selected on data quality in a way that correlates with outcomes. A day is invalid, and not analysed, unless tape-chain completeness is at least 99% and the tape audit shows no mismatch. Market-sessions overlapping an unrecoverable tape gap are removed from both numerator and denominator. Tape gaps and page-cap hits happen in high-print-volume and 429 periods, which are the high-activity periods where adverse selection concentrates. This drops worst-case periods and contradicts 'losers and rejects are kept'.",
  "[operational, serious] The scoring, fills, simulator and study code are written during days 1-3, after recording has started. The prereg doc comes first, but implementation choices (orphan threshold, fill-path details, variant arithmetic) are made after data exists. The code freeze guards only against replay, not against health-data knowledge. It also starts today, with the prereg, recorder and tests due in about 5.5 h (now 06:53 UTC).",
  "[rigor, FATAL until fixed] POSITIVE can be declared from SETTLE alone ('for some metric m in {MARK_1H, SETTLE}'). SETTLE drops markets not settled by final+7 days from the numerator and denominator. For long-dated markets, settling early is driven by the outcome (for example, 'by date' markets resolve YES early), so a decision metric is computed on an outcome-selected subset. Fix: make SETTLE a co-condition (AND) restricted to markets whose scheduled close falls inside the study, or make it descriptive.",
  "[rigor, serious] Tape-gate failures drop market-seconds from all metrics, up to 10% of admitted market-seconds. That is the same outcome-correlated exclusion as operational's, at a smaller scale: incomplete cursor chains occur exactly when print volume is high. Replace with realism's handling: keep every second in the denominator and let the HEAVY-style estimator infer fills from unexplained level shrink during gaps.",
  "[rigor, serious] It records 24/7, including maker hours, where the limiter caps sum to 9+9+4 = 22 req/s against a 20 req/s tier. Feasibility rests on unmeasured actual usage. The maker guard trips only after a day of possibly degraded data in the pre-registered 1.1.0 maker study. When it trips it retroactively restricts the LIP MARK_1H analysis to 12:15-02:05 on all dates, which changes the estimand mid-study.",
  "[rigor and realism, FATAL look-ahead until fixed] Both fire concurrent /markets/orderbooks requests through one client. HttpClient.last_request_window is a single shared attribute (providers/http.py:130/270/308), and KalshiProvider reads it after the await (providers/kalshi.py:715). Concurrent requests therefore overwrite each other's socket window. A book stamped earlier than it actually arrived lets the policy act on data before it existed, which is look-ahead of up to one round trip, and it corrupts the per-second b-/b+ bracketing. Operational's one-client-per-lane design avoids this.",
  "[realism, serious] The primary metric marks fills at mid+300 s only; settlement is descriptive, and inventories of up to S = 1,000 contracts can be held for hours. A POSITIVE verdict can rest on the one horizon that may miss slower adverse selection. It also has the smallest variance, so it is the easiest horizon on which to pass. Fix: require positivity under a long-horizon mark as well (rigor's MARK_1H plus terminal exit, or operational's day-end mark with A4 executable exit).",
  "[realism, rule risk] The reactive withdraw gate (3 consecutive books with either side below T in the book without us) is a de facto pull-on-depletion rule. Our side thins mostly when takers consume it, so the strategy exits during informed sweeps and trims continued adverse fills. It sits close to the banned 'cancel when aggressive orders approach' and to the bona fide requirement. Operational's once-per-hour non-reactive gate, or rigor's no-gate design, is safer.",
  "[realism, serious clustering] The series bootstrap treats the state gas ladders as independent clusters. lip.json has 36 GAS series, including KXAAAGASD and state variants such as KXAAAGASDMI and KXAAAGASDCA, all driven by one national price. The 20% whole-event sample leaves about 19 events live, with daily energy ladders at 59% of $/h. The effective sample is tiny, the series interval is anti-conservative, and there is no leave-one-family-out check.",
  "[operational, clustering] Its series-level check has no family fold either. The 2-per-event / 8-per-series cap admits about 2 markets per state gas series, up to about 100 correlated gas markets out of 200.",
  "[all three, statistical] Percentile cluster bootstraps at 10-20 clusters under-cover in the extreme Bonferroni tails. maker_study._interval also quantizes bounds to the cent, while per-market-hour effects are cents. Use t_{G-1}-calibrated or wild cluster bootstrap-t intervals. Rigor's two-way pigeonhole design partly offsets this; the others do not.",
  "[all three, unmodelled] Nobody can observe whether Kalshi's target test excludes designated-LP or MMA depth. If it does, fewer snapshots count than simulated. This is optimistic for the LIP term in every design and should be listed as a bias that no design brackets."
 ],
 "best_ideas_to_graft": [
  "From realism: quote and record only 12:30-01:50 UTC as a declared scope, so the design never overlaps the maker cron. This removes the 22 req/s cap overlap and rigor's mid-study estimand switch when the maker guard trips.",
  "From realism: keep every admitted second in the denominator even when the recorder or tape is unhealthy. During tape gaps, infer fills from level shrink the prints cannot explain (HEAVY), instead of dropping market-seconds or days.",
  "From realism: in the conservative envelope, have depth our order displaced re-join at the best level behind us, and run the target test on a counterfactual book that adds back the prints we absorbed.",
  "From realism: settle these before the prereg is committed: diff the governing regulatory notice; get the token cost of a 100-ticker batch (rewrite the prereg, not patch it, if it is not about 1); get the write limit; count the collector's actual req/s; run a synthetic replay day and a 30-minute dry run.",
  "From realism: a seeded \u00b1250 ms phase jitter on the 1 Hz book grid, so our samples do not alias with bots that act on second boundaries. Report how often v(b-) != v(b+) as a diagnostic.",
  "From realism: canary tests. A small-pool market where every fill drifts 2c against us must never reach MAKER_POSITIVE; a large-pool market with no takers may reach it only through the LOW share.",
  "From operational: one KalshiProvider and HttpClient per lane (books x2, tape, status). HttpClient.last_request_window is shared, so concurrent requests on one client mis-stamp socket windows. Also use RateLimiter(rate, burst=1), a skip-if-in-flight tick scheduler, and a final tape drain.",
  "From operational: a tape audit that compares the per-ticker /markets/trades feed with the global feed for 2 hash-chosen universe tickers every 300 s.",
  "From operational: A2 ORPHAN_FILL (whole remaining size filled when we are left above others' best bid) and A4 EXECUTABLE_EXIT (walk the recorded opposite side at window end and pay the taker fee) as extra stress readings a positive claim must survive. Also a day-end inventory mark as the long-horizon co-condition.",
  "From operational: the $1 floor applied per market-day, with a property test that the daily floor never pays more than the period floor. Optionally restrict to 1-cent linear grids, which removes the sub-cent tick ambiguity.",
  "From operational: do not register lip_cli in cli.py until the study ends, so the maker cron's import graph is byte-identical.",
  "Fixes within rigor itself: make SETTLE an AND co-condition limited to markets with scheduled close inside the study, or descriptive only. Replace percentile intervals with t_{G-1}-calibrated or wild cluster bootstrap-t intervals on the two-way (date x family) design. Keep the family fold, the 5-per-family hash cap, the three-estimator intersection and leave-one-family-out; these are the design's strongest defences against universe and clustering failure."
 ]
}