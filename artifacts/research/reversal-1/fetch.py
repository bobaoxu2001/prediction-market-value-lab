"""Fetch 1-minute candles for the reversal-1 sample (PLAN.md). Read-only public data.

Refuses to run, and stops, whenever either study is recording or about to start: the maker
study's lock or process, the LIP recorder's lock or process, or the clock inside 12:10-13:35
UTC (LIP starts 12:25) or 01:55-02:40 UTC (the maker study starts 02:10). Resumes where it
stopped; a market whose request fails is retried on the next run.
"""
import asyncio
import gzip
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pmvl_markets.providers.http import HttpClient
from pmvl_shared.config import get_settings

HERE = Path(__file__).parent
CAL = HERE.parent / "calibration-1"
OUT = HERE / "candles.jsonl.gz"
DONE = HERE / "fetched.txt"
STOP_UTC = (12, 10)
RATE = 2.5


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sample():
    rows = [json.loads(line) for line in open(CAL / "rows.jsonl")]
    rows = [r for r in rows if float(r["volume_fp"]) >= 1000]
    by_series = defaultdict(list)
    for r in rows:
        by_series[r["event_ticker"].split("-")[0]].append(r)
    keep = []
    for series, rs in by_series.items():
        rs.sort(key=lambda r: hashlib.sha256(r["ticker"].encode()).hexdigest())
        keep += rs[:20]
    return keep


def studies_busy():
    for lock in ("/tmp/pmvl-maker-day.lock.d", "/tmp/pmvl-lip-day.lock.d"):
        if Path(lock).exists():
            return f"lock {lock}"
    for pat in ("pmvl record sessions", "lip_cli record-day"):
        if subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0:
            return f"process {pat}"
    # never be running when a study starts: LIP fires 12:25 UTC (its wrapper waits until
    # 13:30), the maker study 02:10 UTC. Outside these, a running study shows its lock/process.
    now = datetime.now(timezone.utc)
    hm = (now.hour, now.minute)
    if STOP_UTC <= hm < (13, 35):
        return "12:10-13:35 UTC - the LIP day starts at 12:25"
    if (1, 55) <= hm < (2, 40):
        return "01:55-02:40 UTC - the maker day starts at 02:10"
    return ""


async def main():
    keep = sample()
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    todo = [r for r in keep if r["ticker"] not in done]
    print(f"sample {len(keep)} markets in {len({r['event_ticker'].split('-')[0] for r in keep})} series; "
          f"{len(done)} done, {len(todo)} to fetch", flush=True)
    c = HttpClient(get_settings().kalshi_api_base, name="kalshi", rate_per_second=RATE)
    n = errors = 0
    try:
        for r in todo:
            why = studies_busy()
            if why:
                print(f"stopping: {why}", flush=True)
                return
            close = ts(r["close_time"])
            start = max(ts(r["open_time"]), close - timedelta(days=7))
            series = r["event_ticker"].split("-")[0]
            try:
                # the endpoint returns at most 5,000 candles per request: ask in pieces of 4,900 minutes
                # markets settled before Kalshi's historical cutoff (2026-08-06) are served only by
                # /historical/markets/..., with plain field names ("close", "volume")
                for src, path in (("live", f"/series/{series}/markets/{r['ticker']}/candlesticks"),
                                  ("historical", f"/historical/markets/{r['ticker']}/candlesticks")):
                    cs, t0, t1 = {}, int(start.timestamp()), int(close.timestamp())
                    while t0 < t1:
                        t_end = min(t1, t0 + 4900 * 60)
                        d = await c.get_json(path, params={"start_ts": t0, "end_ts": t_end, "period_interval": 1},
                                             use_cache=False, allow_404=True)
                        for x in (d or {}).get("candlesticks") or []:
                            cs[x["end_period_ts"]] = x
                        t0 = t_end
                    if cs:
                        break
                cs = [cs[k] for k in sorted(cs)]
                errors = 0
            except Exception as e:  # left out of fetched.txt, so the next run retries it
                errors += 1
                print(f"  error {r['ticker']}: {type(e).__name__}: {str(e)[:120]}", flush=True)
                if errors >= 20:
                    print("stopping: 20 errors in a row", flush=True)
                    return
                await asyncio.sleep(min(60, 2 ** errors))
                continue
            def px(side):
                return side.get("close_dollars", side.get("close"))
            keep_fields = [{"t": x["end_period_ts"],
                            "b": px(x.get("yes_bid") or {}), "a": px(x.get("yes_ask") or {}),
                            "v": x.get("volume_fp", x.get("volume"))} for x in cs]
            with gzip.open(OUT, "at") as out:
                out.write(json.dumps({"ticker": r["ticker"], "series": series, "n": len(cs), "src": src if cs else None,
                                      "start": int(start.timestamp()), "close": int(close.timestamp()),
                                      "candles": keep_fields}) + "\n")
            with open(DONE, "a") as fh:
                fh.write(r["ticker"] + "\n")
            n += 1
            if n % 250 == 0:
                print(f"  {n}/{len(todo)} fetched", flush=True)
    finally:
        await c.aclose()
    print(f"done: {n} fetched this run", flush=True)


if __name__ == "__main__":
    why = studies_busy()
    if why:
        print(f"refused: {why}")
        sys.exit(3)
    asyncio.run(main())
