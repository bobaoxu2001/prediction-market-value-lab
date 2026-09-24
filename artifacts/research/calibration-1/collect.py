"""Collect the calibration study's inputs, exactly as docs/calibration-preregistration.md fixes them.

    collect.py rows      list settled markets day by day; keep the 10% event sample
    collect.py fees      the fee terms of every series in the sample
    collect.py quotes    the hourly candles at t_obs for H in {1, 6, 24} h

Each stage resumes where it stopped. Read-only, at 2.5 requests a second: the maker
study's recorder and the crypto scheduler share Kalshi's read limit with this.
"""
import asyncio, hashlib, json, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pmvl_markets.providers.http import HttpClient
from pmvl_shared.config import get_settings

HERE = Path(__file__).parent
ROWS, FEES, QUOTES = HERE / "rows.jsonl", HERE / "fees.json", HERE / "quotes.jsonl"
PROGRESS = HERE / "progress.json"
WINDOW = (datetime(2026, 6, 30, tzinfo=timezone.utc), datetime(2026, 9, 16, tzinfo=timezone.utc))
# closes are listed from a day before the window (t_obs can be up to 24 h before the
# expected expiration) to yesterday (a market can close after its expected expiration)
LIST_FROM = WINDOW[0] - timedelta(days=1)
LIST_TO = datetime(2026, 9, 23, tzinfo=timezone.utc)
HORIZONS_H = (1, 6, 24)
MIN_VOLUME = 100          # implied by the OI >= 100 filter at t_obs: volume >= OI always


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sampled(event_ticker):
    return int(hashlib.sha256(event_ticker.encode()).hexdigest(), 16) % 10 == 0


def progress():
    return json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}


def save_progress(p):
    PROGRESS.write_text(json.dumps(p, indent=1))


async def stage_rows(c):
    p = progress()
    done = set(p.get("days_listed", []))
    day = LIST_FROM
    while day < LIST_TO:
        key = day.date().isoformat()
        if key in done:
            day += timedelta(days=1)
            continue
        cursor, pages, seen, kept = None, 0, 0, []
        while True:
            params = {"status": "settled", "limit": 1000, "mve_filter": "exclude",
                      "min_close_ts": int(day.timestamp()), "max_close_ts": int((day + timedelta(days=1)).timestamp()) - 1}
            if cursor:
                params["cursor"] = cursor
            d = await c.get_json("/markets", params=params, use_cache=False)
            ms = d.get("markets") or []
            pages += 1
            seen += len(ms)
            for m in ms:
                if not sampled(m["event_ticker"]) or float(m.get("volume_fp") or 0) < MIN_VOLUME:
                    continue
                ee = m.get("expected_expiration_time")
                if not ee or not (WINDOW[0] <= ts(ee) < WINDOW[1]):
                    continue
                kept.append({k: m.get(k) for k in (
                    "ticker", "event_ticker", "status", "result", "settlement_value_dollars",
                    "open_time", "close_time", "expected_expiration_time", "volume_fp", "market_type")})
            cursor = d.get("cursor")
            if not cursor or not ms:
                break
        with open(ROWS, "a") as f:
            for r in kept:
                f.write(json.dumps(r) + "\n")
        p.setdefault("days_listed", []).append(key)
        p.setdefault("listing", {})[key] = {"pages": pages, "markets": seen, "kept": len(kept)}
        save_progress(p)
        print(f"{key}: {seen} settled markets over {pages} pages, kept {len(kept)}", flush=True)
        day += timedelta(days=1)


def load_rows():
    rows = {}
    for line in open(ROWS):
        r = json.loads(line)
        rows[r["ticker"]] = r        # a market listed twice (day boundary) is kept once
    return rows


async def stage_fees(c):
    fees = json.loads(FEES.read_text()) if FEES.exists() else {}
    series = sorted({r["event_ticker"].split("-")[0] for r in load_rows().values()} - set(fees))
    for s in series:
        d = await c.get_json(f"/series/{s}", allow_404=True, use_cache=False)
        se = (d or {}).get("series") or {}
        fees[s] = {"fee_type": se.get("fee_type"), "fee_multiplier": se.get("fee_multiplier"),
                   "category": se.get("category")}
        if len(fees) % 50 == 0:
            FEES.write_text(json.dumps(fees, indent=1))
    FEES.write_text(json.dumps(fees, indent=1))
    print(f"fees for {len(fees)} series", flush=True)


async def stage_quotes(c):
    rows = load_rows()
    have = set()
    if QUOTES.exists():
        for line in open(QUOTES):
            have.add(json.loads(line)["ticker"])
    groups = defaultdict(list)
    for r in rows.values():
        if r["ticker"] in have:
            continue
        f = ts(r["expected_expiration_time"]).replace(minute=0, second=0, microsecond=0)
        groups[f].append(r["ticker"])
    total = sum(len(v) for v in groups.values())
    print(f"{len(rows)} rows, {len(have)} already quoted, {total} to fetch in {len(groups)} hour groups", flush=True)
    n = 0
    for f in sorted(groups):
        tickers = groups[f]
        start = int((f - timedelta(hours=48)).timestamp())
        end = int((f - timedelta(hours=1)).timestamp())
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            d = await c.get_json("/markets/candlesticks", params={
                "market_tickers": ",".join(chunk), "start_ts": start, "end_ts": end, "period_interval": 60},
                use_cache=False)
            got = {m["market_ticker"]: m.get("candlesticks") or [] for m in (d or {}).get("markets") or []}
            with open(QUOTES, "a") as out:
                for tk in chunk:
                    cs = sorted(got.get(tk, []), key=lambda x: x["end_period_ts"])
                    obs = {}
                    for h in HORIZONS_H:
                        t_obs = int((f - timedelta(hours=h)).timestamp())
                        last = [x for x in cs if x["end_period_ts"] <= t_obs]
                        if not last:
                            obs[str(h)] = None
                            continue
                        x = last[-1]
                        obs[str(h)] = {"t_obs": t_obs, "candle_end": x["end_period_ts"],
                                       "yes_ask": (x.get("yes_ask") or {}).get("close_dollars"),
                                       "yes_bid": (x.get("yes_bid") or {}).get("close_dollars"),
                                       "oi": x.get("open_interest_fp")}
                    out.write(json.dumps({"ticker": tk, "floor_exp": int(f.timestamp()), "obs": obs}) + "\n")
            n += len(chunk)
        if n and n % 2000 < 100:
            print(f"  quotes: {n}/{total}", flush=True)
    print(f"quotes done: {n} markets", flush=True)


async def main():
    c = HttpClient(get_settings().kalshi_api_base, name="kalshi", rate_per_second=2.5)
    try:
        for stage in sys.argv[1:]:
            await {"rows": stage_rows, "fees": stage_fees, "quotes": stage_quotes}[stage](c)
    finally:
        await c.aclose()

asyncio.run(main())
