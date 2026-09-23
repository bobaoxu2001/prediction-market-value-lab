#!/bin/bash
# One day of the maker study: 20 sessions of 30 minutes, into a dated directory.
#
# Run from cron. Everything it needs is fixed in the pre-registration
# (docs/maker-multiday-preregistration.md) - the frozen universe, the session count,
# the cadence - so this script takes no arguments and makes no choices. A day that
# needs a decision is a day that is not a repetition of the others.
set -euo pipefail

REPO="/Users/xuao/Projects/prediction-market-value-lab"
cd "$REPO"

DAY="$(date -u +%Y-%m-%d)"
OUT="artifacts/forward/maker-daily/$DAY"
LOCK="/tmp/pmvl-maker-day.lock.d"

# A day that overruns must not be joined by the next one: two recorders would halve
# each other's rate budget and both would record a market that neither was alone in.
#
# mkdir is the lock, because macOS has no flock(1). It is atomic, it fails when the
# directory exists, and a stale one after a crash is visible rather than silent - the
# next run says exactly which directory to remove.
mkdir -p artifacts/forward/maker-daily

# A recorder started by hand holds no lock file, so check for the process too.
# Without this the first cron firing would have run straight into a day that was
# already recording, and the two would have halved each other's rate budget.
if pgrep -f "pmvl record sessions" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) $DAY skipped: a recorder is already running" \
        >> artifacts/forward/maker-daily/skipped.log
    exit 0
fi

if ! mkdir "$LOCK" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) $DAY skipped: $LOCK exists, so a previous day is still running (or crashed - remove it by hand)" \
        >> artifacts/forward/maker-daily/skipped.log
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

mkdir -p "$OUT"
exec >> "$OUT/runner.log" 2>&1
echo "=== $DAY starting $(date -u +%FT%TZ) ==="

# --count is honoured against the manifest, so a day interrupted midway resumes
# where it stopped rather than starting over or stopping short.
.venv/bin/pmvl record sessions \
    --universe artifacts/forward/universe-study-frozen.json \
    --count 20 --duration 1800 --interval 10 --watch 100 --top 8 \
    --out "$OUT"

echo "=== $DAY recorded $(date -u +%FT%TZ) ==="

# Scan the day the moment it is recorded, and write the answer down either way.
# A watchdog that only runs when someone remembers to run it is not a watchdog,
# and one that only speaks on a positive cannot be told apart from a broken one.
.venv/bin/pmvl replay daily-scan "$OUT" || \
    echo "$(date -u +%FT%TZ) $DAY scan failed" >> artifacts/forward/maker-daily/skipped.log

# Polymarket complete sets are the one family where two genuinely independent books
# price the same dollar, so they can dislocate where Kalshi's cannot. One request per
# 200 markets, so it is cheap enough to check every day.
# A minute at ~2 Hz, to probe the resolution the 10-second cadence cannot reach.
.venv/bin/pmvl record burst --seconds 60 || \
    echo "$(date -u +%FT%TZ) $DAY burst failed" >> artifacts/forward/maker-daily/skipped.log

.venv/bin/pmvl record polymarket-sets || \
    echo "$(date -u +%FT%TZ) $DAY polymarket scan failed" >> artifacts/forward/maker-daily/skipped.log

echo "=== $DAY finished $(date -u +%FT%TZ) ==="
