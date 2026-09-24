"""Record Kalshi's Bitcoin ladder and the live Coinbase price through one settlement.

KXBTCD settles on the simple average of CF Benchmarks' BRTI over the sixty seconds
before the hour. The BRTI is built from exchange prices; Coinbase is a constituent and
is free and sub-second. So near settlement the outcome of every rung is close to
determined by a price anyone can see. The question is whether the book is.
"""
import asyncio, json, sys, time, ssl, certifi
from datetime import datetime, timezone
from pathlib import Path
import httpx
from pmvl_markets.providers.kalshi import KalshiProvider
from pmvl_markets.recording import AppendOnlyLog

EVENT = sys.argv[1]
UNTIL = datetime.fromisoformat(sys.argv[2])
PRODUCT = sys.argv[3] if len(sys.argv) > 3 else "BTC-USD"       # the Coinbase constituent
OUT = Path(sys.argv[4]) if len(sys.argv) > 4 else Path(__file__).parent
OUT.mkdir(parents=True, exist_ok=True)
TICKER_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT}/ticker"

async def main():
    ctx = ssl.create_default_context(cafile=certifi.where())
    k = KalshiProvider()
    books_log = AppendOnlyLog(OUT / "books.jsonl")
    px_log = AppendOnlyLog(OUT / "prices.jsonl")
    async with httpx.AsyncClient(timeout=10, verify=ctx) as cb:
        spot = float((await cb.get(TICKER_URL)).json()["price"])
        d = await k._client.get_json("/markets", params={"status": "open", "event_ticker": EVENT, "limit": 1000},
                                     use_cache=False)
        ms = [m for m in d["markets"] if m.get("floor_strike") is not None]
        ms.sort(key=lambda m: abs(float(m["floor_strike"]) - spot))
        ms = ms[:100]
        (OUT / "markets.json").write_text(json.dumps({"event": EVENT, "product": PRODUCT, "spot_at_start": spot,
            "markets": {m["ticker"]: {"floor_strike": m["floor_strike"], "strike_type": m["strike_type"]}
                        for m in ms}}, indent=2))
        tickers = [m["ticker"] for m in ms]
        print(f"{EVENT}: {PRODUCT} spot {spot:.4f}, {len(tickers)} rungs nearest spot, "
              f"strikes {min(float(m['floor_strike']) for m in ms):.0f}-{max(float(m['floor_strike']) for m in ms):.0f}",
              flush=True)

        async def price_loop():
            while datetime.now(timezone.utc) < UNTIL:
                t0 = time.time()
                try:
                    r = await cb.get(TICKER_URL)
                    j = r.json()
                    px_log.append({"seen_at": datetime.now(timezone.utc).isoformat(),
                                   "price": j.get("price"), "bid": j.get("bid"), "ask": j.get("ask"),
                                   "exchange_time": j.get("time")})
                except Exception as e:
                    print("price err", e, flush=True)
                await asyncio.sleep(max(0.0, 1.0 - (time.time() - t0)))

        async def book_loop():
            while datetime.now(timezone.utc) < UNTIL:
                cap = await k.get_orderbooks_synchronized(tickers)
                if cap.ok:
                    books_log.append({"seen_at": cap.received_at.isoformat(), "rtt_ms": round(cap.roundtrip_ms, 1),
                        "books": {t: {"yes_ask": str(b.best_ask("yes")) if b.best_ask("yes") is not None else None,
                                      "no_ask": str(b.best_ask("no")) if b.best_ask("no") is not None else None,
                                      "yes_size": str(b.asks("yes")[0].size) if b.asks("yes") else None,
                                      "no_size": str(b.asks("no")[0].size) if b.asks("no") else None}
                                  for t, b in cap.books.items()}})
        await asyncio.gather(price_loop(), book_loop())
    await k.aclose()

asyncio.run(main())
