"""Do forward-filled candle quotes match the order books we actually recorded?

Run before the calibration pre-registration, to decide whether candles may stand in
for the book. Compares, for every recorded snapshot taken within 4 s after a period
boundary, the recorded best asks with the last candle ending at that boundary.

    validate_candles.py            1-minute candles, minute boundaries
    validate_candles.py --hourly   hourly candles, hour boundaries (what the study uses)
"""
import asyncio, glob, json, os, sys
from bisect import bisect_right
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pmvl_markets.providers.http import HttpClient
from pmvl_markets.providers.kalshi import KalshiProvider
from pmvl_shared.config import get_settings

dirs = sorted(set(glob.glob("artifacts/forward/maker-study*/session-*")))
HOURLY = "--hourly" in sys.argv
PERIOD_MIN = 60 if HOURLY else 1
# the batch endpoint caps a request at 10,000 candles; three days of hours fits 100 tickers
LOOKBACK = 3 * 86400 if HOURLY else 3600
async def main():
    k = KalshiProvider(client=HttpClient(get_settings().kalshi_api_base, name="kalshi", rate_per_second=3.0))
    agree = Counter(); examples = []; err = Counter()
    for d in dirs:
        snaps = []
        with open(os.path.join(d, "snapshots.jsonl")) as f:
            for line in f:
                b = json.loads(line)["body"]
                if "books" in b: snaps.append(b)
        if not snaps: continue
        t0 = int(datetime.fromisoformat(snaps[0]["response_received_at"]).timestamp())
        t1 = int(datetime.fromisoformat(snaps[-1]["response_received_at"]).timestamp())
        tickers = sorted({t for s in snaps for t in s["books"]})
        candles = {}
        for i in range(0, len(tickers), 100):
            r = await k._client.get_json("/markets/candlesticks", params={"market_tickers": ",".join(tickers[i:i+100]),
                    "start_ts": t0 - LOOKBACK, "end_ts": t1, "period_interval": PERIOD_MIN}, use_cache=False)
            for m in (r or {}).get("markets") or []:
                cs = sorted(((c["end_period_ts"], c) for c in m.get("candlesticks") or []), key=lambda x: x[0])
                candles[m["market_ticker"]] = cs
        for s in snaps:
            t = datetime.fromisoformat(s["response_received_at"]).timestamp()
            sec = t % (60 * PERIOD_MIN)
            if sec > 4: continue          # only snapshots within 4 s after a minute boundary
            boundary = int(t - sec)
            for tk, book in s["books"].items():
                cs = candles.get(tk) or []
                i = bisect_right([e for e, _ in cs], boundary) - 1
                if i < 0: agree["no candle yet"] += 1; continue
                c = cs[i][1]
                ca = (c.get("yes_ask") or {}).get("close_dollars"); cb = (c.get("yes_bid") or {}).get("close_dollars")
                ya = min((Decimal(p) for p, q in book.get("yes_asks") or []), default=None)
                na = min((Decimal(p) for p, q in book.get("no_asks") or []), default=None)
                yb = (1 - na) if na is not None else None
                ca = Decimal(ca) if ca else None; cb = Decimal(cb) if cb else None
                if ca is not None and ca >= 1: ca = None      # candle writes an empty side as 1.00 / 0.00
                if cb is not None and cb <= 0: cb = None
                cna = (1 - cb) if cb is not None else None
                for side, c_ask, r_ask in (("yes", ca, ya), ("no", cna, na)):
                    if c_ask is None or r_ask is None:
                        err[(side, "one side empty" if (c_ask is None) != (r_ask is None) else "both empty")] += 1
                    else:
                        err[(side, int((c_ask - r_ask) * 100))] += 1
                gap = boundary - cs[i][0]
                key = "exact" if (ca == ya and cb == yb) else ("ask only" if ca == ya else ("bid only" if cb == yb else "both differ"))
                agree[(key, "same minute" if gap == 0 else "carried forward")] += 1
                if key == "both differ" and len(examples) < 8:
                    examples.append((tk, boundary, gap, str(ca), str(ya), str(cb), str(yb)))
    await k.aclose()
    tot = sum(v for kk, v in agree.items() if isinstance(kk, tuple))
    for kk, v in sorted(agree.items(), key=lambda x: -x[1]):
        print(f"  {str(kk):45s} {v:6d}  {v / max(tot,1):.1%}")
    for side in ("yes", "no"):
        rows = {kk[1]: v for kk, v in err.items() if kk[0] == side}
        num = {kk: v for kk, v in rows.items() if isinstance(kk, int)}
        tot = sum(num.values())
        print(f"{side} ask, candle minus recorded, in cents ({tot} comparable):")
        for c in sorted(num):
            if num[c] / tot >= 0.002: print(f"   {c:+4d}c  {num[c]:6d}  {num[c]/tot:6.1%}")
        mean = sum(c * v for c, v in num.items()) / tot
        print(f"   mean {mean:+.3f}c; |err|<=1c {sum(v for c, v in num.items() if abs(c) <= 1)/tot:.1%}; "
              f"candle cheaper {sum(v for c, v in num.items() if c < 0)/tot:.1%}, dearer {sum(v for c, v in num.items() if c > 0)/tot:.1%}; "
              f"one side empty {rows.get('one side empty', 0)}")
asyncio.run(main())
