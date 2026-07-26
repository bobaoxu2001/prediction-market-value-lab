# What is not done, and what to build next

## Not implemented in this release

| Area | Status | Why |
|---|---|---|
| WebSocket orderbook streaming | Not implemented | Polling at 1–3 min is sufficient for the current scan cadence. Specified in `.env.example` (`POLYMARKET_WS_BASE`) and the provider interface has room for it. |
| Sports / macro / politics models | **No opinion**, by design | Every usable feed is credentialed. These categories return `no_opinion` and name the feed they would need, rather than guessing. |
| Isotonic / Platt / Beta calibration | Metrics computed, fitting not wired | Calibration *measurement* (reliability curve, Brier vs market) is live. Fitting a calibrator needs a walk-forward validation set of real settled recommendations, which does not exist until the platform has run for weeks. |
| LLM-assisted market matching | Interface present, unused | `MarketMatch.llm_assisted` exists. Deterministic verification is the gate, so an LLM can only propose candidates — worth adding once there is a candidate backlog to triage. |
| Live Top-10 with real recommendations | Structurally working, empty in practice | The independence gate is doing its job. See below. |
| Trading execution | Deliberately absent | Out of scope for v1. `/system/eligibility` exists as the gate for a future, isolated service. |

## The three highest-value next steps

### 1. Widen the independent-prior coverage
This is the binding constraint on the whole platform. Today a market can only be
recommended if it has a verified cross-venue counterpart or falls into the crypto /
weather models — which in practice is a small minority of the universe. Concretely:

- **Add an equity/index threshold model.** Same driftless-GBM machinery as crypto,
  pointed at a free delayed-quote source. Immediately unlocks Kalshi's large
  `KXINX`/`KXNASDAQ` families and Polymarket's index markets.
- **Add an economics model** behind a FRED key: release calendar plus consensus and
  the Cleveland Fed nowcast covers CPI/NFP/GDP markets on both venues.
- **Improve matching recall.** The proper-noun guard is correctly strict, but recall
  is now the bottleneck — an embedding-based candidate generator feeding the same
  deterministic verifier would raise matches without weakening the gate.

### 2. Accumulate a real track record and fit calibration on it
Everything downstream of the snapshot is built and tested, but has only ever run on
synthetic history. Run `pmvl schedule` continuously for 4–6 weeks, then:

- Fit isotonic or beta calibration on a walk-forward split of real settled
  recommendations and version it in `model_versions.calibration`.
- Re-check **Brier improvement versus market** on live data. If it is not positive,
  the honest response is to say so on `/backtest` and narrow the model's scope — the
  plumbing to show that already exists.

### 3. Orderbook coverage and freshness
Only ~250–400 of ~6,900 ingested markets currently get a book fetch per cycle, and
arbitrage detection is bounded by that.

- WebSocket subscriptions for the top few hundred markets, falling back to polling.
- Event-complete fetching is implemented but budget-limited; raising the budget for
  negative-risk events directly increases multi-outcome arbitrage coverage.
- Dynamic cadence: markets inside 6 hours of resolution should refresh far more often
  than 30-day markets.

## Known risks

- **Matching false positives are the most dangerous failure mode.** A wrong pair
  becomes a wrong "independent prior" and manufactures edge. Current defences: the
  proper-noun guard, three-way threshold comparison, source-family comparison, and a
  0.6 confidence floor for priors. Anything that loosens matching must be tested
  against the Brennan/Kim case in `tests/test_matching_and_time.py`.
- **Polymarket expected resolution is an estimate** (`endDate` + fixed oracle lag).
  A market can sit in the 24h bucket and settle materially later.
- **Slippage beyond measured book impact is a fixed tick pad**, not a fitted impact
  model. It will understate cost for large orders in thin books.
- **The backtest cannot model queue position or partial fills**, so it flatters any
  strategy that assumes it gets the whole top level.
- **Demo data is one config flag away from the production surface.** The provenance
  filter defaults to live everywhere and is covered by tests, but any new endpoint
  must apply `apply_provenance` explicitly.
