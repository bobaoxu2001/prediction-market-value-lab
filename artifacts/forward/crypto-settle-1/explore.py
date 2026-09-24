"""EXPLORATORY - how does the book price near-certainty? Not the pre-registered test.

analyze.py asks one committed question (was a locked winner on sale?). This one maps
the whole curve: for every judged rung at every sweep in the last ten minutes, the
move the unknown part of the average would need to flip it, and what the winning side
cost there. It exists to show *why* the committed test comes back empty, and to shape
any rule for later hours, which would then have to be committed before those hours.

It differs from analyze.py in one deliberate way: the settlement proxy forward-fills
the last Coinbase trade into every second of the window (the index samples every
second whether or not anyone traded), so thinly traded assets are not left unjudged.

    explore.py RECORDING_DIR
"""
import json, math, sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
D = Path(sys.argv[1])
meta = json.loads((D / "markets.json").read_text())
SETTLE = datetime.fromisoformat(meta.get("close_time", "2026-09-24T01:00:00Z").replace("Z", "+00:00"))
W0 = SETTLE - timedelta(seconds=60)
def body(l): return json.loads(l)["body"]
def pex(s):
    w, _, f = s.rstrip("Z").partition("."); return datetime.fromisoformat(f"{w}.{(f+'000000')[:6]}+00:00")
books = [body(l) for l in open(D / "books.jsonl")]
prices = [p for p in (body(l) for l in open(D / "prices.jsonl")) if p.get("price")]
for p in prices:
    p["seen"] = datetime.fromisoformat(p["seen_at"]); p["ex"] = pex(p["exchange_time"]); p["px"] = float(p["price"])
prices.sort(key=lambda p: p["seen"]); st = [p["seen"] for p in prices]
# forward-filled per-second proxy
def fill_avg():
    vals = []
    for s in range(60):
        t = W0 + timedelta(seconds=s + 1)
        c = [p for p in prices if p["ex"] < t]
        if c: vals.append(c[-1]["px"])
    return sum(vals) / len(vals), len(vals)
avg, n = fill_avg()
print(f"{meta['event']}: forward-filled proxy avg {avg:.6g} ({n}/60 s)")
def known(t):
    i = bisect_right(st, t)
    if i == 0: return None, 0, 0
    got = [p for p in prices[:i]]
    spot = got[-1]["px"]
    lastex = max(p["ex"] for p in got)
    fixed = []
    for s in range(60):
        e = W0 + timedelta(seconds=s + 1)
        if e > lastex: break
        c = [p for p in got if p["ex"] < e]
        if c: fixed.append(c[-1]["px"])
    return spot, len(fixed), (sum(fixed) / len(fixed) if fixed else 0)
BINS = [0, 0.0005, 0.001, 0.002, 0.003, 0.005, 0.01, 1]
agg = defaultdict(lambda: {"n": 0, "ask": [], "none": 0, "le98": 0, "le97": 0})
lastmin = defaultdict(lambda: {"n": 0, "ask": [], "none": 0, "le98": 0, "le97": 0})
clear = 0.0012 * avg
for b in books:
    t = datetime.fromisoformat(b["seen_at"])
    if not (SETTLE - timedelta(minutes=10) <= t < SETTLE): continue
    spot, e, m = known(t)
    if spot is None: continue
    for tk, mm in meta["markets"].items():
        K = float(mm["floor_strike"])
        if abs(avg - K) < clear: continue
        won = "yes" if avg > K else "no"
        kf = (60 * K - e * m) / (60 - e) if e else K
        side = "yes" if spot > kf else "no"
        mv = abs(kf - spot) / spot * (1 if side == won else -1)
        if mv < 0: continue
        bi = max(i for i, x in enumerate(BINS) if mv >= x)
        for tgt in ([agg] + ([lastmin] if t >= W0 else [])):
            a = tgt[bi]; a["n"] += 1
            ask = (b["books"].get(tk) or {}).get(f"{won}_ask")
            if ask is None: a["none"] += 1; continue
            a["ask"].append(Decimal(ask))
            if Decimal(ask) <= Decimal("0.98"): a["le98"] += 1
            if Decimal(ask) <= Decimal("0.97"): a["le97"] += 1
for name, tgt in (("last 10 min", agg), ("inside the averaging window", lastmin)):
    print(f"  {name}: winning-side ask by move needed to flip")
    for bi in sorted(tgt):
        a = tgt[bi]; asks = sorted(a["ask"])
        med = asks[len(asks)//2] if asks else None
        print(f"    move {BINS[bi]*100:5.2f}-{(BINS+[math.inf])[bi+1]*100:6.2f}%  obs {a['n']:6d}  no ask {a['none']:6d}  "
              f"min {asks[0] if asks else None}  median {med}  <=0.98 {a['le98']:5d}  <=0.97 {a['le97']:5d}")
