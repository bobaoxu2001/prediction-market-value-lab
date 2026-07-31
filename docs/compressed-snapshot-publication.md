# Deterministic compressed Snapshot publication

## Why the committed representation changed

The public API still serves an immutable SQLite Snapshot, but new publications
commit `data/pmvl-snapshot.db.gz` rather than the raw
`data/pmvl-snapshot.db`. The raw database contains venue identifiers with
credential-shaped byte sequences. GitHub Push Protection correctly scans binary
pushes and can block those coincidental matches; the project will not allowlist a
finding, bypass repository rules, or disable secret scanning.

Deterministic gzip is a short-term transport fix, not a secrecy mechanism and not a
claim that binary data no longer needs scanning. It preserves the exact SQLite
bytes while changing their committed representation. Moving Snapshot blobs out of
Git remains the longer-term storage decision; see the backlog item below.

## The manifest is authoritative

`data/pmvl-snapshot.manifest.json` selects exactly one representation:

- A legacy manifest with no compression fields selects the tracked raw
  `data/pmvl-snapshot.db`.
- A modern manifest with `artifact_encoding: gzip` selects only its declared
  relative `compressed_path`. Missing or corrupt gzip fails closed; the runtime
  never falls back to a raw file.
- Raw and gzip must not coexist in a published commit.

The canonical Snapshot identity remains the uncompressed SQLite bytes:

| Field | Meaning |
|---|---|
| `sha256`, `file_size_bytes` | Backward-compatible aliases for the SQLite bytes |
| `uncompressed_sha256`, `uncompressed_size_bytes` | Explicit SQLite identity |
| `compressed_sha256`, `compressed_size_bytes` | Exact committed gzip identity |
| `compression_algorithm`, `compression_level` | `gzip`, level `9` |
| `compression_deterministic` | Must be `true` |

Gzip is generated with an empty header filename and `mtime=0`. Validation
reproduces the level-9 stream and requires byte-for-byte equality, then decompresses
it and requires byte-for-byte equality with the validated raw candidate. Both
hashes and both sizes are checked independently.

## Build, handoff, and publication

The research job builds a writable candidate in scratch space, completes all
SQLite/API validation, and only then finalizes a HELD candidate containing:

```text
pmvl-snapshot.db
pmvl-snapshot.db.gz
pmvl-snapshot.manifest.json
run-report.json
```

The Actions artifact name includes workflow run ID, run attempt, and source commit.
The write-enabled publish job downloads that exact artifact and independently
checks both identities, provenance, run identity, parent Snapshot ID/checksum, HELD
status, and the full Snapshot validator.

Publication is one Git commit. On the first migration it stages exactly:

```text
D data/pmvl-snapshot.db
A data/pmvl-snapshot.db.gz
M data/pmvl-snapshot.manifest.json
```

Later publications stage exactly the gzip and manifest modifications. The workflow
rejects renames, extra staged paths, coexistence, or a manifest/file-set mismatch.
It reads the committed blobs back from Git and verifies them against the exact raw
candidate before a normal fast-forward push. Push Protection remains enabled; no
force push or CI-skipping commit is used.

Automatic publication remains off. Scheduled computation and publication use
separate repository variables:

- `PMVL_SCHEDULE_ENABLED` controls scheduled computation.
- `PMVL_SCHEDULE_PUBLISH_ENABLED` is an additional manual-publication guard and
  normally stays `false`.

## Runtime behavior

On a modern deployment, the API:

1. validates the manifest and gzip hash/size;
2. decompresses into a unique staging file under
   `/tmp/pmvl-snapshots`;
3. validates the uncompressed hash/size and SQLite `integrity_check`;
4. atomically installs
   `/tmp/pmvl-snapshots/<uncompressed_sha256>.db` read-only;
5. opens it with SQLite `mode=ro&immutable=1`.

The runtime temp root is rejected if it points inside the repository. Concurrent
cold starts can never observe a partial final database, and a verified cached file
is reused. Legacy raw commits follow the same read-only integrity checks without
decompression, which keeps rollback compatibility.

## Rollout and rollback

The rollout order is intentional:

1. Merge this runtime/workflow change while production still contains the legacy
   raw Snapshot and prove that deployment remains healthy.
2. Enable publication only for one observed manual canary run.
3. Reset the publication variable immediately after that run, whether it succeeds
   or fails.
4. Verify the resulting commit, CI, both production deployments, route parity, and
   Snapshot identity.

Rollback is a normal revert of the publication commit. The reverted legacy manifest
authoritatively selects its restored raw database, so no application-code rollback
is required. Do not manually decompress and commit a raw replacement.

## Backlog: move Snapshot blobs out of Git

Before publication frequency or Snapshot size materially increases, evaluate an
immutable object-store or release-asset design with:

- content-addressed object names;
- an authenticated write path and public read-only delivery;
- immutable retention plus rollback pointers;
- SHA-256 verification before use;
- atomic manifest/pointer promotion;
- equivalent CI, provenance, and post-deploy checks;
- a migration plan that keeps the current legacy and gzip readers during rollout.

That project is deliberately not coupled to this hotfix. Deterministic gzip removes
the immediate raw-binary push failure without weakening repository security or
provisioning new infrastructure.
