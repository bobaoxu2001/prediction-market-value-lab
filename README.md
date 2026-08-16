# Prediction Market Value Lab

A read-only research and paper-trading platform that scans **Kalshi** and
**Polymarket** for executable value and arbitrage, then publishes an immutable,
auditable track record of what it recommended and how those calls actually resolved.

> **Research and information only.** Not investment advice, not a solicitation, and
> not an offer to trade. This platform holds no funds, stores no wallet keys, and
> places no orders. Simulated performance does not indicate future results.

---

## The surface that always has an answer

Every analytical page here except one is downstream of a probability estimate,
and the independence rule below refuses to produce one for most markets. That is
correct, and it is why the ranked list is usually empty — which leaves a reader
with nothing on the majority of visits.

`/cost` asks a question that needs no probability: **what can entry cost under
stated assumptions.** Every market with a quote has an answer.

Because a binary contract pays exactly $1, the cost estimate per contract maps to
the break-even probability under those assumptions. A contract quoted at 34¢ with
an estimated entry cost of 37.2¢ needs the event to happen 37.2% of the time, not
34%, to break even on that basis.

Size is where this bites. Kalshi ceils its fee to the whole cent **on the whole
order**, so on a 1¢ contract the fee alone is a full cent:

| Order size | Estimated cost each | Premium over quote |
|---:|---:|---:|
| 1 | 2.00¢ | **+100%** |
| 10 | 1.10¢ | +10% |
| 100 | 1.07¢ | +7% |

Same contract, same instant, a fourteen-fold spread in premium. Neither venue
displays this, and it does not depend on any forecast — which is why the cost
surface covers the categories the models decline: politics, sports and macro all
have a cost even when they have no opinion.

**Observed inputs and assumptions are itemised everywhere.** The headline figure
combines observed ask depth and the venues' published fee schedules and rounding
rules with disclosed configuration assumptions for Polygon transfer amortisation
and capital cost to resolution. The slippage pad
(`tick_size × SLIPPAGE_TICKS`) is an assumption about market impact and is
reported beside the headline, never inside it — at a 1¢ tick it exceeds every
observed or rule-derived cost combined on a cheap contract, so a blended number would make the
product's central claim an artefact of a config default.

The live overlay is packaged as a deterministic Chrome beta at `/extension`. Its
first-run guide stays inside the extension, asks for no account, and sends no
install or browsing event to PMVL. The website records only a small allowlist of
anonymous action totals (download intent, Founding Lifetime interest, successful
share, and watchlist add); those counts are not installs, unique users,
first-result activations, or retention. Chrome Web Store install totals and a
separately consented first-result signal are still required before claiming a
1,000-activated-user milestone.

---

## What makes this different from a scanner that prints numbers

Three rules are enforced in code, not just documented:

**1. Prices are executable, never nominal.**
Entry price is the volume-weighted average of the *ask ladder* at the size you
actually want. Last trades and midpoints are never used as entry prices. Kalshi
publishes bids only, so asks are derived (`YES ask = $1 − best NO bid`) and the
derivation is verified against Kalshi's own `yes_ask_dollars` field. On Polymarket,
YES and NO are separate ERC-1155 tokens with independent books, so both are fetched;
a missing side stays empty rather than being synthesised into fake liquidity.

**2. The model may not trade against its own input.**
If every component of a probability estimate derives from the target market's own
price, the estimate carries no information the market lacks, and the difference
between them is noise. Such markets are flagged `has_independent_prior = false` and
**cannot produce a recommendation** — they appear on a separate watchlist instead.
This is the single most important gate in the system, and it is why the live Top-10
is often empty.

**3. "Arbitrage" is a claim with a definition.**
`executable` requires every leg fillable at current depth, all fees and slippage
deducted, settlement rules *identical* (not merely similar), and net profit still
strictly positive. Everything else is labelled — Rule Mismatch Risk, Execution Risk,
Stale Quote, Insufficient Liquidity, Not Guaranteed, Logical Mispricing — with the
specific reason attached.

---

## Quick start

Requires Python 3.11+ and Node 22+. No Docker, no PostgreSQL, no API keys.

```bash
make setup          # venv, dependencies, database migrations
make ingest         # pull real markets + orderbooks from both venues
make rank           # score markets and publish the Top-10 per horizon
make arbitrage      # run all five arbitrage scanners
make dev            # API on :8000, site on :3000
```

Open <http://localhost:3000>.

The live Top-10 will very likely be **empty** on a first run. That is the correct
result, not a bug — see [Why is the list empty?](#why-is-the-list-empty). To see the
full surface populated:

```bash
make seed-demo      # synthetic, clearly-labelled demo history
make backtest
```

then visit any page with `?mode=demo`.

### All commands

| Command | What it does |
|---|---|
| `make setup` | venv, install packages, run migrations |
| `make setup-web` | install frontend dependencies |
| `make dev` | API (:8000) + web (:3000) together |
| `make api` / `make web` | run one service |
| `make worker` | run the recurring scheduler |
| `make ingest` | markets, events, orderbooks, trades from both venues |
| `make orderbooks` | refresh books only (fast, high-frequency job) |
| `make score` | run the probability ensemble without publishing |
| `make rank` | score → rank → publish Top-N per horizon |
| `make arbitrage` | all five arbitrage scanners |
| `pmvl retrodict` | replay the models against settled markets: do they beat the price? |
| `pmvl readiness` | how far the live track record is from supporting its claims |
| `pmvl calibrate` | fit a calibration map on settled recommendations, or record why not |
| `make settle` | sync settlements, grade past recommendations |
| `make snapshot` | freeze today's batch into the immutable record |
| `make backtest` | walk-forward backtest across 10 strategies |
| `make status` | job health and row counts |
| `make test` | full test suite (no network required) |
| `make seed-demo` | synthetic demo history (provenance=demo) |
| `make purge-demo` | delete every demo row; live data untouched |
| `make reset-db` | drop and rebuild the local database |

`pmvl pipeline` runs ingest → rank → arbitrage → snapshot → settle → backtest in one
shot.

---

## Architecture

```
prediction-market-value-lab/
  apps/web/                        Next.js 16 + TypeScript + Tailwind
                                   13 research pages + 10 public-site pages
  services/
    api/                           FastAPI read-only research API
    worker/                        typer CLI + APScheduler scheduler
  packages/
    shared/                        config, Decimal money core, ORM, schemas
    market-normalization/          providers, matching, pricing, probability,
                                   arbitrage, backtest, demo seeder
  tests/                           1,250+ tests + recorded venue fixtures
  data/                            SQLite database and scratch data
  docs/
```

**Storage.** SQLite by default so `make setup` needs no infrastructure; set
`DATABASE_URL` to a `postgresql+psycopg://` DSN for PostgreSQL (see
`docker-compose.yml`). A custom `Money` column keeps `Decimal` exact on both — SQLite
has no `NUMERIC` affinity and would otherwise hand back floats.
Production bundles the same immutable SQLite bytes as a deterministic gzip artefact;
the API verifies both identities and opens an atomic read-only `/tmp` copy. See
[the Snapshot publication contract](docs/compressed-snapshot-publication.md).

**Money is `Decimal` everywhere.** Binary contracts live on a $0.001–$0.01 lattice
where float error changes arbitrage verdicts. `float` appears only inside statistical
formulas (Brier, log-loss, GBM) where precision cannot change a conclusion. The API
serialises money as **strings** so the browser never parses it into a double.

**Scheduling.** APScheduler, at the cadences shown on `/system`. Every job is
`max_instances=1` — an overrunning ingest skips its next tick rather than doubling
load on a venue that is already slow. Every run writes a `job_runs` row so a silently
dead job is visible instead of looking like "no opportunities today".

---

## Data sources

All market data comes from **public, unauthenticated** endpoints.

| Source | Auth | Used for |
|---|---|---|
| Kalshi Trade API v2 | none | markets, events, series, orderbooks, trades, candlesticks, settlement |
| Polymarket Gamma | none | market/event discovery, rules, fee schedule, negative risk |
| Polymarket CLOB | none | per-token orderbooks, midpoint, spread, price history |
| Polymarket Data API | none | public trade prints |
| Coinbase Exchange | none | spot + realised volatility for the crypto model |
| NWS (weather.gov) | none | gridpoint forecasts for the weather model |
| Cleveland Fed | none | inflation nowcast + its own realised-error history, for CPI boards |
| ESPN (site API) | none | fixtures and completed-game results, for the sports model |
| Anthropic | key | optional research agent, **disabled by default** |

The `KALSHI_*` variables in `.env.example` exist only for a future, isolated
execution service. The research pipeline never authenticates to a venue.

### Venue specifics handled

- Kalshi `_dollars` fixed-point strings (4 dp) and `_fp` fractional contracts (0.01
  granularity) — integer share counts are never assumed.
- Per-market tick size from `price_ranges` (`linear_cent`, `deci_cent`,
  `tapered_deci_cent`).
- Per-series `fee_type` / `fee_multiplier` read from the API.
- Cursor pagination with per-endpoint page caps (`/events` rejects pages above 200).
- Polymarket `negRisk` events, condition IDs, token IDs, `acceptingOrders`, and UMA
  oracle latency added to `endDate` for expected resolution.

---

## The models

### Fair probability

Components are pooled in **log-odds space**, weighted by confidence. Aggregate
confidence starts from the best single component, adds diminishing corroboration, and
is scaled *down* by inter-component disagreement.

*Independent of the target market (can support an edge):*
- Cross-platform consensus — verified-equivalent matches only
- Sibling coherence on mutually exclusive, exhaustive events
- **Crypto**: driftless GBM on Coinbase spot + realised vol, with the volatility
  standard error propagated into the interval
- **Weather**: NWS gridpoint forecast + a lead-time-dependent Gaussian error model —
  the same source Kalshi settles on
- **Inflation**: the Cleveland Fed's published nowcast, turned into a bucket
  probability with a dispersion measured from the nowcast's own 158 months of past
  errors rather than assumed. Keyless
- **Sports** (head-to-head games only, **off by default**): Log5 over win-loss
  records from ESPN's keyless feeds. Enabled only on evidence that it beats the
  market price — see below
- Research agent (capped at 0.35 confidence, weight earned from dated novel sources)

*Not independent (display only):*
- Target-market reference prior
- Extreme-price sanity anchor

*Not modelled:* Politics, and every sports contract that is not a head-to-head
game — golf finishing positions, motorsport, tournament futures, player props, which
between them are most of that board. These return **no opinion** and name the
credentialed feed they would need. A guess would still be weighted, would still move
the estimate, and would still generate edge that is pure noise.

### Do the models actually beat the market?

`pmvl retrodict` replays the models against **already-settled** markets at past
instants and scores them against both the outcome and the venue's own price at that
moment. It is deliberately a separate path from the backtest: the backtest reads a
frozen artefact so look-ahead is impossible by construction, while this reaches
backwards through live endpoints and rests on defences that can be wrong. The two
must never be pooled into one number.

The first real result, on 92 crypto forecasts across four lead times:

| | Brier |
|---|---:|
| Independent estimate | 0.07415 |
| The market's own price | 0.07167 |
| **Improvement** | **−0.0025** |

Close to a tie, slightly worse. A narrow sample — two days of settlements, one
category — and the only real evidence there is. It is why the paid pilot was
withdrawn rather than repriced (see
[ADR 003](docs/adr-003-withdraw-the-pilot.md)), and why the sports model ships
behind `SPORTS_MODEL_ENABLED=false`.

The interval is deliberately **not** presented as a formal confidence interval — the
components are not independent draws from a common distribution.

### Value

```
gross_expected_profit  = P(win) − executable entry price
net_ev_per_contract    = P(win) − total executable cost
conservative_net_ev    = fair_probability_low − total executable cost   ← the gate
```

Total cost = entry VWAP + platform fee + fee rounding + slippage (measured book
impact + latency pad) + transfer cost (Polygon bridge, amortised) + capital cost to
resolution + execution-risk penalty.

For a **NO** recommendation the conservative bound is `1 − fair_probability_high`.
Using `1 − low` would be the *optimistic* bound and would systematically overstate
NO-side edge.

Ranking is multiplicative: conservative edge scaled by realisable capacity, then
discounted for liquidity, spread, confidence, freshness and time to resolution.
Ranking on ROI alone would let a 1-cent contract with $3 of depth top the list
forever.

### Fees — validated against published schedules

| | Formula | Check |
|---|---|---|
| Kalshi taker | `ceil_to_cent(0.07 × mult × C × P × (1−P))` | reproduces the published $0.07–$1.75 per-100 range at both endpoints |
| Kalshi maker | `ceil_to_cent(0.0175 × mult × C × P × (1−P))` | reproduces $0.02–$0.44 per 100 |
| Polymarket taker | `round_5dp(C × rate × P × (1−P))` | matches the published per-100 table at every listed price; rate read per market |
| Polymarket maker | zero | "makers are never charged fees" |

Kalshi ceils the fee to the whole cent **on the whole order**, so the per-contract fee
is size-dependent: a 1-contract order pays several times the per-contract rate of a
100-contract order. The model reflects this rather than quoting the large-order rate.

---

## Backtest and track record

The backtest reads **only** `recommendation_snapshots`. It never queries live markets,
never re-runs the model, and never re-prices an entry — look-ahead is structurally
impossible rather than avoided by convention. Selection is applied *within* each
publication day so a later day's rankings cannot influence an earlier day's picks.

Every trade records how its fill was derived, and a run's `data_quality` is the
**worst** quality of any trade in it. A candlestick-derived fill is never presented as
an orderbook-derived one.

Ten strategies (Top 1/3/10, $10/$25 fixed, fractional Kelly, high-confidence only,
24h only, Kalshi only, Polymarket only, combined) reporting win rate, ROI, max
drawdown, profit factor, Sharpe-like per-bet, Brier, log loss, reliability diagram,
and breakdowns by platform / horizon / confidence band.

The headline metric is **Brier improvement versus the market's own implied
probability**. A model that cannot beat the market price adds no information,
regardless of its P&L.

`/track-record` shows every recommendation exactly as published, including losers,
with no filter that can hide them.

---

## Why is the list empty?

An empty live Top-10 is the expected first-run result, for stackable reasons:

1. **No independent prior.** Most markets have no matched counterpart on the other
   venue and fall outside the crypto/weather models, so nothing independent of their
   own price is available. They are listed under "Scored but not recommendable".
2. **No conservative edge after the configured cost stack.** Both venues are
   efficiently priced. Once fees, spread, slippage and the disclosed capital-cost
   assumption are deducted, apparent edges usually vanish.
3. **The conservative bound is used, not the mean.** A wide uncertainty band cannot
   produce a recommendation even if the mean looks attractive.

The same applies to arbitrage: finding nothing is normal, because both venues are
actively arbitraged by faster participants.

Use `?mode=demo` to see how the surface looks when populated.

---

## Demo data

`make seed-demo` writes a synthetic history so the backtest, calibration and
track-record pages are reviewable on day one. The contract:

- Every row carries `provenance = demo`.
- **Live mode is the default on every endpoint**; demo rows are returned only when
  explicitly requested.
- Every demo response carries a `demo_notice` and the UI renders a loud banner.
- The simulated forecaster is deliberately **imperfect** — overconfident in the tails,
  producing a visibly non-diagonal reliability curve and strategies that lose money.
  A flawless demo would misrepresent what the platform can do.
- `ALLOW_DEMO_DATA=false` makes the write path refuse outright.
- `make purge-demo` removes demo rows and leaves live data untouched.

---

## Configuration

Copy `.env.example` to `.env`. Every value there is a placeholder; `.env` is
gitignored and must never be committed. Secrets live only in the process environment,
are reduced to presence booleans on `/system/config`, and are stripped from logs by a
redaction filter.

Key knobs: `MIN_CONSERVATIVE_NET_EV`, `SLIPPAGE_TICKS`, `CAPITAL_COST_ANNUAL_RATE`,
`POLYMARKET_TRANSFER_COST_USD`, `KELLY_FRACTION`, `MAX_QUOTE_AGE_SECONDS`,
`DAILY_SNAPSHOT_HOUR_UTC`, `ALLOW_DEMO_DATA`, `SPORTS_MODEL_ENABLED`.

---

## Deployment

**Docker Compose** (Postgres + API + worker + web):

```bash
docker compose up --build -d
```

**Manual**: run migrations (`alembic upgrade head`), serve
`uvicorn pmvl_api.main:app`, run `pmvl schedule` as a long-lived process, and
`npm run build && npm start` in `apps/web` with `NEXT_PUBLIC_API_BASE` pointing at the
API. Set `API_CORS_ORIGINS` to the site's origin.

---

## Testing

```bash
make test              # everything
make test-unit         # no database
make test-integration  # database, pipelines, API
```

Over 1,250 Python tests, with no network access - outbound sockets are blocked in
conftest, so an accidental live call fails loudly instead of flaking. Provider tests
run against real responses captured from both venues into `tests/fixtures/`, so
payload-shape regressions are caught without the suite depending on the venues being
up. The web app adds 241 vitest tests and the extension 111 (conformance, escaping,
message boundary, race and packaging suites), all executed in CI.

---

## Security and compliance

- No user funds, no wallet keys, no automated trading, and no execution service.
- Read-only API: no write endpoint and no credential intake.
- Circuit breaker, exponential backoff with jitter, token-bucket rate limiting, and
  request timeouts on every provider.
- `/system/eligibility` exists as a gate for a *future* execution surface. Research
  access is unrestricted; reading published market data is not a regulated activity.
- Confirm your own eligibility with each venue before trading anywhere.

---

## Known limitations

See `/methodology` for the live list. The significant ones:

- **No model has yet been shown to beat the market price.** The one real
  measurement puts the crypto model 0.0025 Brier *worse* than the quote. Nothing
  here should be read as a demonstrated edge.
- Politics has no independent model, and neither does any sports contract that is
  not a head-to-head game — which is most of that board. They are covered by
  `/cost`, which needs no model, but are never ranked as opportunities.
- The sports model is off by default. Win-loss record ignores lineups, injuries and
  rest, all of which the market prices, so it is expected to lose to the quote until
  measured otherwise.
- There is no live track record yet: `pmvl readiness` reports 0 settled live
  recommendations against the 60 needed to quote a Brier-versus-market figure.
- Genuinely rule-identical pairs across these two venues are rare, so cross-platform
  arbitrage rarely has anything to report.
- Polymarket expected resolution adds a fixed oracle-latency estimate to `endDate`;
  actual UMA settlement time varies.
- Slippage beyond measured book impact is a fixed tick pad, not a fitted market-impact
  model.
- The backtest cannot model queue position or partial fills.
- WebSocket streaming is specified but not implemented; orderbooks are polled.


See [docs/deployment.md](docs/deployment.md) for deployment and preview wiring.
