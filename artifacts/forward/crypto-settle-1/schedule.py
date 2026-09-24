"""Repeat the settlement recording every hour, for every asset, then judge each one.

Each hourly ladder (KXBTCD, KXETHD, KXSOLD, KXXRPD, KXDOGED) opens an hour before it
closes, and closes at the instant its sixty-second averaging window ends. For each of
the next N hours this starts one recorder per asset eleven minutes before the close,
stops it three minutes after, and runs the pre-registered analysis on each recording.
The rule is analyze.py's as committed; this only supplies more settlements to it.

    schedule.py HOURS [ROOT]

Read-only: the recorders read public order books and Coinbase's public ticker.
"""
import json, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
REPO = HERE.parents[2]
PY = REPO / ".venv" / "bin" / "python"
SERIES = {"KXBTCD": "BTC-USD", "KXETHD": "ETH-USD", "KXSOLD": "SOL-USD",
          "KXXRPD": "XRP-USD", "KXDOGED": "DOGE-USD"}
LEAD = timedelta(minutes=11)
TAIL = timedelta(minutes=3)
NY = ZoneInfo("America/New_York")

hours = int(sys.argv[1])
root = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / "artifacts" / "forward" / "crypto-settle-runs"
root.mkdir(parents=True, exist_ok=True)


def log(msg):
    line = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}"
    print(line, flush=True)
    with open(root / "schedule.log", "a") as f:
        f.write(line + "\n")


def next_close(now):
    top = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return top if top - LEAD > now else top + timedelta(hours=1)


done = 0
while done < hours:
    close = next_close(datetime.now(timezone.utc))
    start = close - LEAD
    wait = (start - datetime.now(timezone.utc)).total_seconds()
    log(f"next settlement {close:%H:%MZ}; starting recorders in {wait / 60:.1f} min")
    if wait > 0:
        time.sleep(wait)
    suffix = close.astimezone(NY).strftime("%y%b%d%H").upper()
    run = root / f"{close:%Y%m%dT%H%M}Z"
    procs = []
    for series, product in SERIES.items():
        event = f"{series}-{suffix}"
        out = run / series
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "runner.log", "w") as fh:
            procs.append((event, out, subprocess.Popen(
                [str(PY), str(HERE / "recorder.py"), event, (close + TAIL).isoformat(), product, str(out), "1.0"],
                stdout=fh, stderr=subprocess.STDOUT, cwd=REPO)))
    maker = subprocess.run(["pgrep", "-f", "pmvl record sessions"], capture_output=True).returncode == 0
    log(f"recording {', '.join(e for e, _, _ in procs)} until {close + TAIL:%H:%M:%SZ} at 1 sweep/s"
        f"{' (maker study recorder also running)' if maker else ''}")
    for _, _, p in procs:
        p.wait()
    with open(run / "analysis.txt", "w") as fh:
        for event, out, p in procs:
            fh.write(f"===== {event} (recorder exit {p.returncode}) =====\n")
            if p.returncode != 0 or not (out / "books.jsonl").exists():
                fh.write((out / "runner.log").read_text()[-2000:] + "\n")
                continue
            r = subprocess.run([str(PY), str(HERE / "analyze.py"), str(out)],
                               capture_output=True, text=True, cwd=REPO)
            fh.write(r.stdout + r.stderr + "\n")
    summary = {"settlement": close.isoformat(), "events": [e for e, _, _ in procs], "maker_running": maker,
               "recorder_exit": {e: p.returncode for e, _, p in procs}}
    with open(root / "runs.jsonl", "a") as fh:
        fh.write(json.dumps(summary) + "\n")
    log(f"settlement {close:%H:%MZ} judged -> {run / 'analysis.txt'}")
    done += 1
