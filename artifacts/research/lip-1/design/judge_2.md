{
 "scores": [
  {
   "design": "rigor",
   "validity": 7.5,
   "realism": 8,
   "feasibility": 6,
   "conservatism": 8.5,
   "total": 30
  },
  {
   "design": "realism",
   "validity": 8,
   "realism": 9,
   "feasibility": 7,
   "conservatism": 9,
   "total": 33
  },
  {
   "design": "operational",
   "validity": 4.5,
   "realism": 5.5,
   "feasibility": 7.5,
   "conservatism": 5,
   "total": 22.5
  }
 ],
 "winner": "realism",
 "fatal_flaws": [
  "[operational] The decision rule is built on the flattering model. The date- and series-bootstrap intervals that decide MAKER_POSITIVE are computed only on the primary model: a static queue where cancels ahead never advance us (QUEUE_MODEL), whole-level credit that counts our displacement of others' deeper levels in our favour, and a mid mark. The conservative variants (A1 FRONT_OF_QUEUE, A2 ORPHAN_FILL, A3 TRUNCATED, A4 EXECUTABLE_EXIT) only need a point estimate above 0. In this strategy fills are the loss and resting time is the income, so undercounting fills is the optimistic error; the design's reason for calling QUEUE_MODEL 'not optimistic' comes from the maker study, where the direction was the opposite. A positive verdict can therefore be issued when the lower bound under the readings that actually bind (price-time credit, front-of-queue or orphan fills, an executable exit) is at or below 0.",
  "[operational] It can stop early, positive or negative, at look 1 with only 10 date clusters, using a percentile bootstrap at a two-sided 99.75% level. With n = 10 a percentile or normal interval undercovers: the t9 quantile is about 4.3 against z of about 3.0, so the true one-sided error is several times the nominal alpha. Skewed daily fill-loss distributions make it worse, so the family-wise false-positive rate is well above the stated 0.05.",
  "[operational] The write budget is only reported, not enforced. Repricing both sides of about 149 markets at 1 Hz can far exceed a basic account's write limit, so the simulated quotes are fresher than a real account's could be. Stale quotes are exactly where adverse fills come from.",
  "[rigor] SETTLE can decide a verdict on its own ('some policy and metric m in {MARK_1H, SETTLE}'), yet it drops markets not settled by final + 7 days from both numerator and denominator. Kalshi 'by date' markets often settle early precisely because the outcome has already occurred, so which markets are included depends on the outcome. That is selection on outcome inside a verdict path, and it breaks the unconditional estimand. SETTLE must be descriptive only, as it is in realism and operational.",
  "[rigor, operational] The tape-quality gates drop market-seconds (rigor) or whole market-sessions (operational) with poll gaps or incomplete cursor chains from both numerator and denominator. Chains fall behind when print volume bursts, which is when adverse selection happens, so the drop preferentially removes toxic periods and can make a loss look like a profit. Realism keeps those seconds in the denominator and, under HEAVY, infers fills from unexplained shrink at our level.",
  "[rigor, operational] Others' depth is overstated or understated in our favour after our paper fills. Both designs use the recorded book for other traders' size at our level after our paper order absorbed prints that in reality filled those traders; in the counterfactual those orders would still be resting, so the denominator is too small and our share is overstated. Likewise, asks that would have traded against our crossing bid are left in the opposite side's target test. Only realism's LOW adds absorbed prints back and runs the target test on the counterfactual book.",
  "[rigor, realism] Concurrent book requests mis-stamp their timestamps. HttpClient.last_request_window is a single attribute that every request overwrites (providers/http.py:130/270), and get_orderbooks_synchronized reads it after the await (providers/kalshi.py:715). Two concurrent 100-ticker requests, or the tape lane, on one client can swap sent/received windows. That corrupts the per-second bracketing (b-/b+), the S_max/UNOBSERVED test and the latency-dependent fill windows, in either direction. Operational alone requires one client and provider per lane.",
  "[rigor] Feasibility hazard to another pre-registered study, not a profit-overstating flaw. Recording 24/7 overlaps the maker study's 02:10-12:10+ window. Limiter caps sum to 22 req/s against the 20/s tier, and the guard trips only after a maker day has already been degraded; its 700 ms skew threshold sits inside the maker's normal 315-660 ms p50 range and may miss subtler degradation.",
  "[realism] Abuse-clause / bona fide risk, not a simulation bias. The in-hour depth gate (withdraw after 3 consecutive books where either side is below T without us) is a reactive liquidity withdrawal. It fires when others pull depth, which often precedes informed flow, and it leaves the strategy present mainly when it is being paid. It is simulated with honest latency, so it does not inflate the model, but a positive verdict could rest on behaviour Kalshi may treat as inconsistent with the program's purpose.",
  "[realism] The series bootstrap is anti-conservative. With no family fold, the state gas ladders (about 59% of in-window $/h) count as independent series, so both the 10-series minimum and the series-level interval overstate the effective number of clusters."
 ],
 "best_ideas_to_graft": [
  "From rigor: the family fold (strip a trailing state/DC code when the remainder is a series that has had a program), a per-family cap, and a two-way pigeonhole date x family bootstrap in place of realism's one-way series bootstrap. Also add rigor's leave-one-family-out check: the positive point estimate must stay above 0 after removing the largest-contribution family.",
  "From rigor: add TRADED_THROUGH as a third fill scenario next to HEAVY and LIGHT. The positive claim takes the minimum over all three, the negative claim the maximum. This covers the case where fills turn out to be profitable, where fewer fills is the conservative reading.",
  "From rigor: make the primary mark 3,600 s (MARK_1H) instead of 300 s, and charge a terminal exit on residual inventory by walking the recorded opposite side at t_end + L, plus the taker fee. The 300 s mark leaves out slower adverse selection.",
  "From rigor: credit a stale order at min(its actual price, the policy's target price on b-). Walk others' qualifying score both with and without our order and take O_max, so our insertion never helps us in LOW. Stop quoting 300 s before close.",
  "From rigor: log every non-admitted program with a reason. Test that admission is unchanged when book and trade files are removed or shuffled. Declare that if a bug fix changes results after data exists, both numbers are reported and the verdict uses the more conservative one.",
  "From operational: give every lane its own HttpClient and KalshiProvider so socket windows are not overwritten, and use a skip-if-in-flight 1 Hz scheduler.",
  "From operational: the LIP runner waits on /tmp/pmvl-maker-day.lock.d and on pgrep for 'pmvl record sessions' before starting, because the maker day's burst, polymarket-sets and settlements tail can run past 12:10. Put the LIP commands in a separate lip_cli that cli.py and record_cli.py do not import, so the maker cron's import path is untouched.",
  "From operational: a per-ticker tape audit that compares 2 hash-chosen universe tickers against the global feed every 300 s, and a pre-registered conditional L = 1000 page size with its preflight.json.",
  "From operational: ORPHAN_FILL as an extra HEAVY-side variant: when our order sits more than its improve_ticks above others' best bid, its whole remaining size fills at our price. Also the cross-test that the last point of fill_path equals evaluate_quote().filled_size.",
  "From operational: apply the $1 floor per (market, period, study day). It is provably no more generous than the period floor and removes the cross-day coupling in realism's allocation of G to days. Admit only the 1-cent linear grid, which removes the sub-cent tick ambiguity, and require close_time at least 60 minutes after the window.",
  "From operational: replace realism's reactive in-hour depth gate with a session-level gate that is never re-checked inside the hour. Alternatively, require the positive verdict to also hold with the gate disabled.",
  "New for all three: with 10-20 clusters, use a small-sample-corrected interval (studentized or t-scaled cluster bootstrap) instead of raw percentile intervals, and never allow a decision from fewer than 20 date clusters."
 ]
}