# Deployment

Two Vercel projects, both git-connected to this repository, both deploying from the
same commit.

| Project | Root directory | Serves | Production branch |
|---|---|---|---|
| `pmvl-api` | `.` | FastAPI on a bundled read-only SQLite snapshot | `main` |
| `pmvl-web` | `apps/web` | Next.js frontend | `main` |

## How a preview happens

```
push branch
  → GitHub Actions (tests, snapshot integrity, frontend build, secret scan)
  → pmvl-api preview      → https://pmvl-api-git-<branch-slug>-<team>.vercel.app
  → pmvl-web preview      → https://pmvl-web-git-<branch-slug>-<team>.vercel.app
  → web preview reads the API preview via a branch-scoped env var
  → visual verification
  → merge to main
  → both production deployments
```

Both projects expose the commit they were built from at `/system` under `deployment`
(`commit_sha`, `commit_ref`, `vercel_env`). Check it before trusting a preview URL:
the web project once served a commit two merges old with nothing on the page saying so.

## The two settings that made this break

**`pmvl-web` was not git-connected.** It had only ever been deployed by CLI, so no
branch ever produced a preview and production drifted behind `main`. Fixed with
`vercel git connect` run from the repository root, since `apps/web` is not itself a
git root.

**Root Directory was `.`.** A git-triggered build would have built the repository
root, which contains the *API's* `vercel.json`. It must be `apps/web`. The CLI cannot
set this; use the dashboard, or:

```bash
curl -X PATCH \
  -H "Authorization: Bearer $VERCEL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"rootDirectory":"apps/web"}' \
  "https://api.vercel.com/v9/projects/<projectId>?teamId=<teamId>"
```

## API base URL

`NEXT_PUBLIC_API_BASE` is inlined at build time, so it must be set per environment
*before* the build:

| Environment | Value |
|---|---|
| Production | `https://pmvl-api.vercel.app` |
| Preview (per branch) | that branch's API preview alias |
| Development | `http://localhost:8000` |

A preview with no Preview-scoped value falls back to `http://localhost:8000` and every
page renders the API-unavailable state. Set it per branch:

```bash
cd apps/web
API_ALIAS=$(vercel inspect <api-preview-url> 2>&1 | grep -o 'https://pmvl-api-git-[^ ]*')
printf '%s' "$API_ALIAS" | vercel env add NEXT_PUBLIC_API_BASE preview <branch-name>
```

Find the API's branch alias with `vercel inspect <deployment-url>`. It is far more
stable than the per-deployment URL, but **not permanent** — the hash segment has been
observed to change for the same project and branch, at which point the old alias
returns `DEPLOYMENT_NOT_FOUND` and the web preview silently loses its backend. Re-read
it from `vercel inspect` rather than reusing a remembered URL, and re-point the env
var when it moves.

## Verifying a preview is really wired to its own API

Do not assume. Confirm a field that exists only on the branch:

```bash
curl -s "$API_PREVIEW/markets?limit=1" | grep -q yes_ask_depth_usd && echo "branch API"
curl -s "$WEB_PREVIEW/markets" | grep -q "Ask depth" && echo "web reads branch API"
```

If the web preview renders production-only output, the branch-scoped env var is
missing or the build predates it — env changes require a **rebuild**, not a redeploy
of the same build.

## Snapshot timestamps

A snapshot deployment has several different times. They are hours to weeks apart and
must never be presented as one "snapshot timestamp" — doing so produced a banner that
reported the single freshest observation as the capture time for all 1850 markets,
when only 12 were that fresh and 1516 were more than a day older.

`/system` returns them under `snapshot_timing`, each named for the question it answers:

| Field | Means |
|---|---|
| `market_ingest_started_at` | when the ingest job began asking the venues |
| `market_ingest_finished_at` | when it finished |
| `freshest_quote_observed_at` | the single most recent `markets.quote_observed_at` — **one** market, not the dataset |
| `median_quote_observed_at` | the middle of the distribution; the honest headline for "how old is this data" |
| `oldest_quote_observed_at` | the tail |
| `arbitrage_scan_at` | when the cross-platform scan ran |

Two are reported as `null` with a stated reason in `snapshot_timing.unavailable`,
because nothing records them and approximating either would be inventing provenance:

- **`snapshot_artifact_built_at`** — no job writes it. The `snapshot` job name belongs
  to the daily recommendation snapshot used by the track record, not to building this
  file, and a committed binary's mtime reflects checkout.
- **`deployment_created_at`** — Vercel exposes only commit SHA, ref, URL and
  environment to the process. It is readable from the Vercel API externally.

The UI banner says `Latest captured quote: …` and `Arbitrage scan: …` as separate
labelled values, followed by the note that individual markets may be older. Never
reintroduce a single "quotes captured" timestamp, and never render a snapshot
timestamp through a helper that measures from `Date.now()` — `/system` previously
showed "2d old" growing while the reader sat on the page.

`scripts/validate_snapshot.py` fails the build if the timing block is missing, if
freshest equals oldest, or if either unavailable field is ever populated.

## The snapshot

The API serves a committed SQLite snapshot (`data/pmvl-snapshot.db`). Three
deployment outages traced to it, so all three are now checked by
`scripts/validate_snapshot.py` in CI:

1. **Missing from the bundle.** It was gitignored, and Vercel's uploader honours
   `.gitignore`. `.vercelignore` now re-includes it explicitly.
2. **WAL mode.** A WAL database cannot be opened read-only on a serverless mount —
   SQLite wants to create a `-shm` sidecar on a read-only filesystem. Opening the file
   through SQLAlchemy silently sets WAL, so the builder and the validator both restore
   rollback-journal mode and assert it from the file header.
3. **Schema drift.** The committed snapshot predated a migration, so the API returned
   500 on a column that did not exist. The validator now serves the real routes
   against the committed file.

Rebuild with `make snapshot-build`; never hand-edit the binary.
