"""Forward-record NWS observations and Kalshi daily-high books together.

The question is not whether an already-locked rung is priced at a dollar - it is -
but what happens at the moment a rung *becomes* locked: does the book reprice before
the public NWS feed publishes the crossing observation, or after? Both streams are
written to disk with the time each was actually *seen*, so the lag can be measured
offline and re-measured later.
"""
import asyncio, json, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pmvl_markets.providers.kalshi import KalshiProvider
from pmvl_markets.providers.nws_observations import NwsObservationsProvider
from pmvl_markets.recording import AppendOnlyLog

OUT = Path(__file__).parent
CITIES = {"KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA",
          "KXHIGHAUS": "KAUS", "KXHIGHDEN": "KDEN", "KXHIGHLAX": "KLAX",
          "KXHIGHPHIL": "KPHL"}
DAY = sys.argv[1] if len(sys.argv) > 1 else "26SEP23"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 5 * 3600
BOOK_EVERY, OBS_EVERY = 15.0, 60.0

async def main():
    k, n = KalshiProvider(), NwsObservationsProvider()
    books_log = AppendOnlyLog(OUT / "books.jsonl")
    obs_log = AppendOnlyLog(OUT / "observations.jsonl")
    seen_obs: set[tuple[str, str]] = set()
    try:
        tickers, rules = [], {}
        for series in CITIES:
            d = await k._client.get_json("/markets", params={"status": "open", "event_ticker": f"{series}-{DAY}",
                                                             "limit": 50}, use_cache=False, allow_404=True)
            for m in (d or {}).get("markets") or []:
                tickers.append(m["ticker"])
                rules[m["ticker"]] = {k2: m.get(k2) for k2 in ("strike_type", "floor_strike", "cap_strike",
                                                                "event_ticker", "rules_primary")}
        (OUT / "markets.json").write_text(json.dumps({"day": DAY, "cities": CITIES, "markets": rules}, indent=2))
        print(f"{len(tickers)} rungs across {len(CITIES)} cities; recording {DURATION/3600:.1f}h", flush=True)
        end = time.time() + DURATION
        last_obs = 0.0
        while time.time() < end:
            t0 = time.time()
            cap = await k.get_orderbooks_synchronized(tickers)
            if cap.ok:
                books_log.append({"seen_at": cap.received_at.isoformat(),
                                  "skew_ms": round(cap.roundtrip_ms, 1),
                                  "books": {t: {"yes_ask": str(b.best_ask("yes")) if b.best_ask("yes") is not None else None,
                                                "no_ask": str(b.best_ask("no")) if b.best_ask("no") is not None else None,
                                                "yes_ask_size": str(b.asks("yes")[0].size) if b.asks("yes") else None,
                                                "no_ask_size": str(b.asks("no")[0].size) if b.asks("no") else None}
                                            for t, b in cap.books.items()}})
            if time.time() - last_obs >= OBS_EVERY:
                last_obs = time.time()
                for series, station in CITIES.items():
                    f = await n.fetch_recent(station, limit=12)
                    seen = datetime.now(timezone.utc).isoformat()
                    for r in f.records:
                        key = (station, r.observation_time.isoformat())
                        if key in seen_obs or r.temperature_f is None:
                            continue
                        seen_obs.add(key)
                        #: seen_at is when *we* first had it - the only honest
                        #: lower bound on when anyone could act on the public feed
                        obs_log.append({"station": station, "series": series,
                                        "observation_time": r.observation_time.isoformat(),
                                        "seen_at": seen, "temperature_f": str(r.temperature_f),
                                        "published_at": r.published_at.isoformat() if r.published_at else None})
            await asyncio.sleep(max(0.0, BOOK_EVERY - (time.time() - t0)))
    finally:
        await k.aclose(); await n.aclose()

asyncio.run(main())
