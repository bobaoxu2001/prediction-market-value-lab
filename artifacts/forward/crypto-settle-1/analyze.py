"""Did the book lag the live price into settlement?

Model-free. After settlement each rung's outcome is known, so for each rung we ask two
things:

  price-certain  the first moment from which the live price stayed on the winning side
                 of the strike, by a margin, through to settlement
  book-certain   the first moment from which the book stopped offering the winning side
                 below 0.97

If the book is consistently later, it lagged a price anyone could see, and the winning
side was on offer in between. Coinbase is a proxy for the BRTI, not the index, so only
rungs that settled well clear of the strike are judged: close to the strike the proxy
and the index can disagree about the outcome itself.
"""
import json
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

D = Path(__file__).parent
CLEAR = 150.0        # settled at least this far from the strike, in dollars
MARGIN = 60.0        # "price-certain" = spot stayed this far on the winning side
CERTAIN = Decimal("0.97")

def body(l): return json.loads(l)["body"]
meta = json.loads((D / "markets.json").read_text())
books = [body(l) for l in open(D / "books.jsonl")]
prices = [body(l) for l in open(D / "prices.jsonl") if body(l).get("price")]
bt = [datetime.fromisoformat(b["seen_at"]) for b in books]
pt = [datetime.fromisoformat(p["seen_at"]) for p in prices]
px = [float(p["price"]) for p in prices]

settle = datetime(2026, 9, 24, 1, 0, 0, tzinfo=timezone.utc)
window = [px[i] for i, t in enumerate(pt) if settle - timedelta(seconds=60) <= t < settle]
if not window:
    raise SystemExit("no prices in the settlement window yet")
avg = sum(window) / len(window)
print(f"Coinbase 60s average into settlement: {avg:,.2f} over {len(window)} prints "
      f"(range {min(window):,.2f}-{max(window):,.2f})\n")

def spot_at(t):
    i = bisect_right(pt, t) - 1
    return px[i] if i >= 0 else None

rows = []
for ticker, m in meta["markets"].items():
    K = float(m["floor_strike"])
    if abs(avg - K) < CLEAR:
        continue
    won = "yes" if avg > K else "no"
    sign = 1 if won == "yes" else -1
    # price-certain: last time before settlement that spot was NOT clearly on the winning side
    last_unclear = None
    for t, p in zip(pt, px):
        if t >= settle: break
        if sign * (p - K) < MARGIN:
            last_unclear = t
    price_certain = (last_unclear + timedelta(seconds=1)) if last_unclear else pt[0]
    # book-certain: last time before settlement the winning side was offered below CERTAIN
    last_cheap, cheapest = None, None
    for t, b in zip(bt, books):
        if t >= settle: break
        a = b["books"].get(ticker, {}).get(f"{won}_ask")
        if a is not None and Decimal(a) < CERTAIN:
            last_cheap = t
            cheapest = Decimal(a) if cheapest is None or Decimal(a) < cheapest else cheapest
    if last_cheap is None or last_cheap < price_certain:
        continue      # never cheap after the price made it clear: no window
    # the cheapest price the winning side was offered at AFTER the price made it clear
    after = [Decimal(b["books"][ticker][f"{won}_ask"]) for t, b in zip(bt, books)
             if price_certain <= t < settle and b["books"].get(ticker, {}).get(f"{won}_ask") is not None
             and Decimal(b["books"][ticker][f"{won}_ask"]) < CERTAIN]
    rows.append((ticker, K, won, price_certain, last_cheap, min(after) if after else None))

judged = sum(1 for m in meta["markets"].values() if abs(avg - float(m["floor_strike"])) >= CLEAR)
print(f"{judged} rungs settled at least ${CLEAR:.0f} from the strike; "
      f"{len(rows)} still offered the winning side below {CERTAIN} after the price had made it clear\n")
for tk, K, won, pc, lc, cheapest in sorted(rows, key=lambda r: r[1]):
    print(f"  {tk:28s} K={K:>9,.0f} won {won:3s}  price-certain {pc:%H:%M:%S}  "
          f"still cheap until {lc:%H:%M:%S} ({(lc-pc).total_seconds():5.0f}s)  cheapest after: {cheapest}")
