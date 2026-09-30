#!/bin/bash
# One day of the LIP maker shadow study (docs/lip-preregistration.md, section 8).
#
# Runs from cron at 12:25 UTC. Takes no arguments and makes no choices: the window,
# the lanes and the admission rule are all fixed in the pre-registration and the
# recorder. It refuses to run inside the maker study's window (02:00-12:25 UTC), waits
# for the maker study's lock and process to clear, and gives up at 13:30 UTC.
#
# Every run leaves two records in artifacts/forward/lip-daily/runs.jsonl, whatever
# happens: a start record the moment cron fires, and an end record from the EXIT trap
# with the recorder's exact exit code and one status -
#   SUCCESS           exit 0: the day was recorded and closed
#   INTENTIONAL_SKIP  exit 6: the recorder skipped the day by design (study.json says why)
#   RECORDER_FAILURE  any other exit the recorder chose (1 uncaught error, 2 bad arguments,
#                     3 refused: inside the maker window, 4 lock held)
#   SIGNALLED         the recorder, or this script, was stopped by a signal (exit > 128)
#   UNKNOWN_FAILURE   anything else, including this script failing before the recorder ran
# The script exits with the recorder's own code, so an intentional skip (6) is never
# turned into a success. A run cron never started leaves no record here at all; that is
# what `lip_cli run-status` is for (the next run records every missed day).
#
# There is deliberately no `set -e`: a failing command must be recorded, not end the
# script before the record is written. Nothing is restarted automatically - a day that
# failed stays failed (runbook: "When something fails").
set -uo pipefail

REPO="${PMVL_LIP_REPO:-/Users/xuao/Projects/prediction-market-value-lab}"
PY="${PMVL_LIP_PYTHON:-$REPO/.venv/bin/python}"
ROOT="$REPO/artifacts/forward/lip-daily"
# the study day, as the recorder names it: a run before 02:00 UTC belongs to yesterday's window
if [ "$(date -u +%H)" -lt 2 ]; then
    DAY="$(date -u -d '-1 day' +%Y-%m-%d 2>/dev/null || date -u -v-1d +%Y-%m-%d)"
else
    DAY="$(date -u +%Y-%m-%d)"
fi
STARTED="$(date -u +%FT%TZ)"
RUN_ID="$DAY-$(date -u +%H%M%S)-$$"
LOG="$ROOT/$DAY.log"
RUNS="$ROOT/runs.jsonl"
rc=""
signalled=""
child=""

mkdir -p "$ROOT" || exit 70   # nowhere to write a record: the sentinel will see a missed day

record() {  # one JSON line; every value is generated here, none comes from outside
    printf '%s\n' "$1" >> "$RUNS"
}

classify() {
    case "$1" in
        0) echo "SUCCESS recorded" ;;
        6) echo "INTENTIONAL_SKIP skipped_day" ;;
        1) echo "RECORDER_FAILURE uncaught_exception" ;;
        2) echo "RECORDER_FAILURE bad_arguments" ;;
        3) echo "RECORDER_FAILURE refused_inside_maker_window" ;;
        4) echo "RECORDER_FAILURE lock_held" ;;
        *)
            if [ "$1" -gt 128 ] 2>/dev/null; then
                echo "SIGNALLED signal_$(( $1 - 128 ))"
            else
                echo "UNKNOWN_FAILURE exit_$1"
            fi ;;
    esac
}

finish() {
    local code="${rc:-$?}"
    [ -z "$code" ] && code=70
    if [ -n "$signalled" ] && [ "$code" -le 128 ] 2>/dev/null && [ "$code" -ne 0 ]; then
        code=$(( 128 + signalled ))
    fi
    read -r status reason <<< "$(classify "$code")"
    if [ -z "$rc" ] && [ -z "$child" ]; then
        status="UNKNOWN_FAILURE"; reason="wrapper_failed_before_recorder"
    fi
    record "{\"event\":\"end\",\"run_id\":\"$RUN_ID\",\"day\":\"$DAY\",\"started_at\":\"$STARTED\",\"ended_at\":\"$(date -u +%FT%TZ)\",\"exit_code\":$code,\"status\":\"$status\",\"reason\":\"$reason\",\"wrapper_pid\":$$,\"recorder_pid\":\"${child}\"}"
    if [ "$code" -ne 0 ]; then
        echo "$(date -u +%FT%TZ) $DAY $status ($reason, exit $code; see $DAY.log)" >> "$ROOT/skipped.log"
    fi
    # the day's manifest, from its files alone (never the network)
    if [ -f "$ROOT/$DAY/study.json" ]; then
        "$PY" -m pmvl_worker.lip_cli manifest "$ROOT/$DAY" >> "$LOG" 2>&1 || true
    fi
    trap - EXIT
    exit "$code"
}

on_signal() {
    signalled="$1"
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true   # the recorder checkpoints and releases its lock
    fi
}

record "{\"event\":\"start\",\"run_id\":\"$RUN_ID\",\"day\":\"$DAY\",\"started_at\":\"$STARTED\",\"wrapper_pid\":$$}"
trap finish EXIT
trap 'on_signal 15' TERM
trap 'on_signal 2' INT
trap 'on_signal 1' HUP

cd "$REPO" || exit 70

# caffeinate holds the machine awake for as long as this script runs: -i against idle
# sleep, -s against system sleep on mains power. It cannot stop a lid-close sleep on
# battery - the maker study lost 2026-09-25 and -27 outright and three more days in part
# to that - so the laptop has to stay open (or on power with an external display)
# 20:25-10:00 local. A sleep inside the window is recorded as a gap (a wall-clock jump
# in the lanes stream), and a day under 80% of its ticks is not counted.
/usr/bin/caffeinate -i -s -w $$ >/dev/null 2>&1 &

PMVL_LIP_RUN_ID="$RUN_ID" "$PY" -m pmvl_worker.lip_cli record-day >> "$LOG" 2>&1 &
child=$!

# every past day's final status - including a day cron never started - goes to the
# run-status ledger; local files only, and it runs beside the recorder, not before it
"$PY" -m pmvl_worker.lip_cli run-status --record >> "$ROOT/run-status.log" 2>&1 &

while :; do
    wait "$child"
    rc=$?
    # a trapped signal interrupts `wait` while the recorder is still closing down
    if kill -0 "$child" 2>/dev/null; then
        continue
    fi
    break
done
exit "$rc"
