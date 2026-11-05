# Research collection: operations

Companion to [pit-collection.md](pit-collection.md), which covers the schema and
timestamp contract. This one covers running it.

---

## 1. Timestamp semantics

One rule above all: **`available_at` is the only admissibility timestamp.**

| Field | Meaning | Gates admissibility? |
|---|---|---|
| `event_time` | When the measured thing happened | No |
| `published_at` | When the **source** published (NWS `updateTime`) | **No** |
| `source_generated_at` | When the source generated it, where distinct | No |
| `poll_started_at` | Our request began | No |
| `poll_completed_at` | Our request finished | No |
| **`available_at`** | **When we could first have used it** = `poll_completed_at` | **Yes** |

`record_source_observation()` and `record_station_observation()` **take no
`available_at` parameter**. It is derived from the `PollWindow`. Tests assert
that passing it raises `TypeError`.

`published_at > available_at` raises: a source cannot publish after we read it.

A retried poll's `available_at` is the **successful** attempt's completion, not
the first attempt's start. Data became knowable when the request that returned it
finished.

**Never fabricated.** Where a product carries no timestamp the column is NULL and
`timestamp_absence` records why. The removed defect was
`parse_ts(updateTime) or utcnow()`, which reported zero source-to-market lag for
exactly the observations whose lag was unknown.

---

## 2. Collector schedules

| Job | Cadence | Purpose |
|---|---|---|
| `forecast_observations` | 15 min | NWS gridpoint forecasts — **the product carrying `published_at`** |
| `station_observations` | 30 min | Settlement-station observations (each poll retrieves the recent series, not one point) |
| `research_books` | 60 s tick | Archives weather books; bursts to 30 s inside an issuance window, over the near-dated subset only |
| `map_stations` | daily 05:00 UTC | Re-parses settlement rules; catches venue re-wording |

Registered in `pmvl_shared.cadence.CADENCES` — the single definition the
scheduler builds triggers from and the API reports from. There is no second copy.

**Two execution paths, and they are not equivalent:**

| Path | Mechanism | What it can achieve |
|---|---|---|
| Resident worker | `pmvl schedule` (APScheduler) | Every cadence above, including 30 s in-window |
| Actions publisher | hourly cron in `pipeline.yml` | One pass per hour. Station observations meet their gate; **30 s in-window is impossible** |

`effective_cadence_seconds()` reports the second honestly rather than printing
the desired interval.

Run one by hand:

```bash
pmvl collect-forecasts
```

```bash
pmvl collect-observations --limit-per-station 36
```

```bash
pmvl collect-books --max-seconds 120
```

---

## 3. Issuance-window logic

A source-lag study needs the market's reaction *shortly after* a forecast
publishes. At 180 s the shortest measurable markout is ~15 minutes, so the
interesting part — the first few minutes — is unobservable. Sampling faster
everywhere would multiply venue load sixfold for twenty-four hours a day to catch
information that exists for a few minutes.

So cadence follows the source's schedule: **±30 minutes around each publication,
at 30 s**, baseline 180 s elsewhere.

The 30 s burst covers markets resolving within 48 hours — about 84 of 252 — not
the whole board, because 252 per-market fetches at the client's 9 req/s consume
28 s of a 30 s period and leave nothing for contention. Measured at 58 s before
the horizon and 29 s after. Coverage of the far-dated markets is unchanged;
they keep their baseline captures. See
[collection-runbook.md](collection-runbook.md#window-universe).

Where the schedule comes from, in order of authority:

1. **Observed `published_at`** values. Once ≥ 4 exist, the schedule is a fact
   about this source.
2. **The documented NDFD cycle** (02/08/14/20 UTC) as a bootstrap.

Every window records its `derivation`, so a reader can tell which were scheduled
from evidence and which from a default. The cycle is expressed as hours-of-day,
not dates, so it cannot silently expire. Window boundaries are **inclusive** —
exclusive edges would drop the captures closest to publication, which are the
point.

`expected_captures()` gives the denominator, so a shortfall reads as "37 of an
expected 120".

---

## 4. Settlement-station mapping

`markets.settlement_station_id` is parsed from the venue's own settlement text by
`normalize/weather_stations.py`. Deterministic: no clock, no network, no ordering
dependence. Phrases match longest-first, and the matched phrase is returned as
provenance.

**There is no fallback.** No coordinate, no nearest-station guess, no inference
from the series ticker. Unrecognised markets are listed as unmapped with a typed
reason. A wrong station trades confidently; an absent one declines.

That design earned itself in August 2026. Kalshi re-worded the board:

| | Wording | Settlement source |
|---|---|---|
| July | "highest temperature recorded in Central Park, New York" | NWS Climatological Report |
| August | "maximum temperature recorded at New York City (**CLINYC**)" | **The Weather Company** |

The parser refused 84 markets rather than mismapping them. A ticker-keyed
fallback would have kept mapping them and **nobody would have noticed the
settlement source had changed.**

The underlying value is still the NWS CLI product — `CLIxxx` are NWS product
codes — but it now reaches the venue through a vendor with its own timing and
revision behaviour. That is a real weakening of "NWS is the same source Kalshi
settles on", the assumption that made weather the first research universe.

`source_location` (grid cell, e.g. `/gridpoints/OKX/34,45`) and
`settlement_station_id` (e.g. `KNYC`) are separate columns. Conflating them
produced an 8 °F Los Angeles error here once already.

**Known open mismatch:** the probability model maps `"austin"` to Camp Mabry
(30.3210, −97.7600); the settlement rule says Austin Bergstrom (KAUS). Different
airports. `model_coordinate_disagreements()` surfaces it; fixing it is a
modelling decision, not a data-cleaning one.

---

## 5. Research cache policy

Research polls **bypass the HTTP cache**, and the bypass is structural rather
than a flag:

- `NwsObservationsProvider` and `NwsForecastProvider` construct their clients
  with `cache_ttl_seconds=0.0`, so no code path can return a cached body;
- every call site also passes `use_cache=False`;
- a test asserts `use_cache=True` appears nowhere in the module.

Production caching elsewhere is untouched — the 900 s gridpoint cache on the
probability model's client is correct for a two-hourly scoring job.

The failure this prevents: a cached body stamped with a **new** `available_at`,
describing a poll that never reached the network. That is a fabricated
availability, not a stale number, and no downstream check would catch it.

`poll_attempts.served_from_cache` records it anyway, and the health report gates
on it being zero.

---

## 6. Research archive backup

`data/research-archive.db` and `data/payloads/` are **gitignored and unbounded by
design**. Nothing in git backs them up.

They cannot be rebuilt. The product snapshot keeps only the latest book per
market and the pipeline re-seeds from the published artefact, so an order-book
time series that is lost is lost permanently.

Back both up on their own schedule. A `sqlite3 .backup` of the archive plus an
rsync of the payload tree is sufficient; the payload store is content-addressed,
so an incremental copy never rewrites an existing object.

`prune_unreferenced()` exists but is deliberately awkward to call and is **not
scheduled**.

---

## 7. Collection-health gates

```bash
pmvl collection-health --days 1
```

```bash
pmvl collection-health --days 7 --json
```

| Gate | Threshold |
|---|---|
| Station observations per station-day | ≥ 20 |
| Source poll success rate | ≥ 95% |
| Books per issuance window | ≥ 40 |
| Research cache hits | 0 |
| Unmapped settlement stations | 0 |
| Payload integrity failures | 0 |

`published_at` coverage is **reported, never gated** — the actual percentage,
with the `timestamp_absence` breakdown beside it. Gating it would create pressure
to fabricate the field.

The report is deterministic: the same database and lookback produce the same
output, with no clock-dependent term except the boundary the caller supplies.
It computes no markout, forward return, IC, Sharpe or PnL, and says so on itself.

---

## 8. Recovery after scheduler downtime

1. **Do not backfill `available_at`.** Data collected late became available late.
   Stamping it with the time it *would* have arrived is the exact fabrication
   this system is built to prevent.
2. Run the collectors normally. `station_observations` retrieves the recent
   series per poll, so a gap of a few hours is partially recovered on the next
   successful poll — the observations arrive with a late but *honest*
   `available_at`.
3. Confirm the gap is visible: `poll_attempts` should show the failures or the
   absence of attempts. If neither, the downtime is invisible and that is worse
   than the downtime.
4. Run `pmvl collection-health --days N` covering the outage and record the
   coverage shortfall.
5. `pmvl map-stations` after any downtime spanning a venue re-wording.

---

## 9. What is irrecoverable

| Loss | Recoverable? |
|---|---|
| Order-book state during downtime | **No.** The venue publishes no history. |
| `published_at` for a forecast revision we did not poll | **No.** NWS overwrites the gridpoint product. |
| The intraday temperature path we did not capture | Partly — the observations endpoint serves a recent window; older than that is gone. |
| A raw payload not stored | **No.** Only the normalised value survives, and it is one parser's opinion. |
| Trade prints pruned from the product DB | **No**, unless archived. |
| Which minute a book was captured in | **No.** |
| Market metadata | Yes — re-ingest. |
| Settlement values | Yes — the CLI product is published retrospectively. |

The asymmetry is the reason this phase exists. Analysis can be redone at any
time; collection cannot.
