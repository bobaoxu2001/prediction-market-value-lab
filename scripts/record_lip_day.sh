#!/bin/bash
# One day of the LIP maker shadow study (docs/lip-preregistration.md, section 8).
#
# Runs from cron at 12:25 UTC. Takes no arguments and makes no choices: the window,
# the lanes and the admission rule are all fixed in the pre-registration and the
# recorder. It refuses to run inside the maker study's window (02:00-12:25 UTC), waits
# for the maker study's lock and process to clear, and gives up at 13:30 UTC.
set -euo pipefail

REPO="/Users/xuao/Projects/prediction-market-value-lab"
cd "$REPO"
mkdir -p artifacts/forward/lip-daily
DAY="$(date -u +%Y-%m-%d)"

# The recorder takes its own lock (/tmp/pmvl-lip-day.lock.d) and releases it on exit;
# a stale lock after a crash is reported and the day is skipped, never shared.
if ! .venv/bin/python -m pmvl_worker.lip_cli record-day >> "artifacts/forward/lip-daily/$DAY.log" 2>&1; then
    echo "$(date -u +%FT%TZ) $DAY recorder exited non-zero (see $DAY.log)" \
        >> artifacts/forward/lip-daily/skipped.log
fi
