# Point-in-time collection: timestamp semantics and the research archive

Two rules govern everything here. Both are enforced in code, not by convention.

> **1. `available_at` is the only admissibility timestamp.**
> **2. Nothing is ever fabricated. A timestamp the source does not provide is NULL, and the NULL is documented.**

---

## The six instants

| Field | Meaning | Gates admissibility? |
|---|---|---|
| `event_time` | When the measured thing happened. A 14:00 temperature reading is 14:00 forever. | No |
| `published_at` | When the **source** made it public (NWS `updateTime`). | **No** |
| `source_generated_at` | When the source says it produced the product, where distinct from publication. | No |
| `poll_started_at` | When our request began. | No |
| `poll_completed_at` | When our request finished. | No |
| **`available_at`** | **When this system could first have used it.** Derived from `poll_completed_at`. | **Yes — only this one** |

### Why `available_at` comes from `poll_completed_at`, not `poll_started_at`

The value could have changed at any moment inside the request. Claiming availability at the start would grant up to a full round-trip of look-ahead on every single observation.

### Why `published_at` must never become `available_at`

A backtest gated on `published_at` would trade on an NWS forecast at the instant NWS issued it — possibly hours before any poll ran — and would report an edge that is a property of the clock rather than of the market.

`record_source_observation()` **takes no `available_at` parameter at all**. It is derived from the `PollWindow`. A caller who could pass it could pass `published_at`, and the resulting dataset would be silently un-backtestable with nothing marking which rows were affected. A test asserts the parameter does not exist.

### Why `published_at` exists anyway

It is the field the whole source-lag hypothesis is measured *from*. The lag is:

```
market_reaction_time − published_at        ← the hypothesis
market_reaction_time − available_at        ← what you measure without published_at
```

The second conflates a slow market with a slow collector. Those imply opposite trades: one is alpha, the other is a latency deficit you are on the wrong side of.

---

## Never fabricate

`WeatherThresholdModel._max_temperature_f` used to compute:

```python
issued = parse_ts(props.get("updateTime")) or utcnow()   # removed
```

A response with no `updateTime` therefore reported a publication time equal to the poll time — **zero source-to-market lag, for exactly the observations whose lag is unknown**. That is a bias toward finding the market slow, in the one measurement the programme turns on.

It now reads:

```python
published_at = parse_ts(props.get("updateTime"))         # None when absent
```

and a missing timestamp is recorded as NULL with an entry in `timestamp_absence`:

```json
{"missing": {"published_at": "gridpoint_forecast does not expose a publication
  time in its payload; NOT defaulted to the poll time"}}
```

The absence is a documented fact about the source, not an unexplained gap a later reader might "repair."

`coverage_report()` reports `published_at_coverage` so a dataset that cannot test the hypothesis on most of its rows says so.

---

## Settlement station ≠ forecast grid cell

`source_location` holds the NWS grid cell (`OKX/33,37`). `settlement_station_id` holds the instrument the contract settles on (`KNYC`). They are separate columns on `source_observations` and `markets`.

They are different places. A gridpoint forecast covers a ~2.5 km cell; the contract settles on one station's official observation. **This repository has already shipped an 8 °F Los Angeles error from treating them as equivalent** — a grid cell forecasting 89 °F against a settlement station forecasting 81 °F.

The failure mode this guards is subtle and expensive: a persistent per-station offset is indistinguishable from a tradeable divergence. It is stable, it has the right sign, and it will look like a beautiful signal for as long as nobody checks.

---

## Station observations

`station_observations` records actual conditions at the settlement station, hourly, plus the daily CLI summary Kalshi settles on.

Required to separate two things a study will otherwise conflate:

- **forecast error** — the forecast was wrong
- **market lag** — the market was slow to price a correct forecast

Without observations there is no way to tell them apart, and the headline result is whichever dominates.

It also captures the **intraday path**. For a daily-maximum contract the realised max becomes a hard floor as the day progresses — usually settled by mid-afternoon local. A forecast-only model therefore degrades through the day in a way that correlates perfectly with time-to-expiry. That pattern looks exactly like an alpha regime and is not one.

Append-only. A corrected observation is a new row with a later `available_at`, never an update.

---

## Poll telemetry

`poll_attempts` records every scheduled attempt, **including failures**.

A gap in an observation series has two causes:

- the source did not change — information about the world
- our collector was down — information about us

Only this table distinguishes them. Without it, downtime becomes "the market was quiet" in every downstream statistic. And downtime is not missing-at-random: it correlates with deploys, load, and quite possibly with the volatile periods a strategy most wants to observe.

`served_from_cache` is recorded because the HTTP client caches gridpoint responses for 900 s. A cached response has an `available_at` describing an *earlier* poll, and treating it as fresh understates observed lag.

---

## Raw payload preservation

Content-addressed store at `data/payloads/`, path = `sha256/ab/cd/<digest>.json.gz`.

- **Immutability is structural.** The path is the hash, so there is no filename to overwrite.
- **Integrity is checkable without a manifest.** Re-hash and compare. `get()` verifies on every read and raises rather than returning corrupt bytes.
- **Deduplication is free.** An unchanged gridpoint polled hourly stores once. JSON is serialised with `sort_keys` so key ordering cannot break the hash.
- **`mtime=0`** in the gzip header, so identical content produces identical files.

A normalised value is a parser's opinion of a payload. Every future factor will want a field today's parser did not extract, or will need to know whether a field was absent or merely unparsed — and neither is recoverable from a stored float. The alternative is reconstructing history from whatever the current parser happens to produce, which is retroactively inventing the past.

`prune_unreferenced()` exists but is deliberately awkward to call and is **not scheduled**. Payload retention is the one thing here that cannot be rebuilt.

---

## Research archive vs product snapshot

These have opposite requirements and are now **structurally separate files**.

| | Product snapshot | Research archive |
|---|---|---|
| Path | `data/pmvl-snapshot.db.gz` | `data/research-archive.db` |
| Ceiling | **40 MiB, hard** | none |
| Retention | aggressive, deterministic | **none — append only** |
| Tracked in git | yes | no (gitignored) |
| Scope | whole venue, pruned | research universe, complete |

### What the product snapshot destroys

The pipeline is a stateless recompute: each run seeds its operational database from the last **published** snapshot. So whatever the builder prunes is gone from the next run's input, and from every run after that.

- every order book except the **latest per market** — no book time series can survive one cycle
- `raw_payload` on every book
- trades beyond 20/market, price snapshots beyond 12/market

Each is correct for an artefact that must fit a function bundle, and fatal for research. The resolution is not a compromise retention setting — it is a second store.

### Why a separate file is the whole safety argument

The snapshot builder cannot prune what it has no connection to. No future edit to the retention policy can reach across a file boundary. A test asserts that neither `build_snapshot.py` nor `snapshot_retention.py` mentions the archive, and that `ResearchArchive` exposes no `delete`, no `prune`, and contains no `DELETE FROM`.

**Back it up separately. Nothing in git will do it for you.**

---

## What future research this preserves

The audit's eventual test is whether a source revision predicts future market innovation *after conditioning on* current price, recent movement, spread, liquidity, time to expiry, time of day, lifecycle stage, and realised station observations.

The residualisation model is deliberately **not** implemented — it is a research decision, not a collection one. What is preserved is every input it will need:

| Control | Preserved by |
|---|---|
| current price, spread, depth | `archived_books` + full `archived_levels` ladder |
| recent price movement | book time series (impossible in the product snapshot) |
| liquidity | full ladder, both sides |
| time to expiry | `expected_resolution_time` on each archived book |
| time of day | `observed_at` |
| market lifecycle | `market_status`, `last_trade_at`, `archived_trades` |
| realised station observations | `station_observations` |
| source timing | `published_at`, `poll_started_at`, `poll_completed_at` |
| coverage / missingness | `poll_attempts` |
| reconstructing any future factor | `raw_payload_ref` → content-addressed store |
