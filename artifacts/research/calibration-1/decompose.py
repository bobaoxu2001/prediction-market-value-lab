"""DESCRIPTIVE, after the test: where does a taker's loss go - to a biased price, or to the spread?

Not part of the pre-registered test (which has already been run and failed). For every
observation with both sides quoted, splits the taker's net return into
    (payout - mid)          is the market's mid-price calibrated?
  - (ask - mid)             the half-spread paid
  - fee
by horizon and by mid-price decile, pooled over both periods.
"""
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import importlib.util

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("a", HERE / "analyze.py")
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)

rows, fees, quotes = a.load()
agg = defaultdict(lambda: defaultdict(list))
for tk, r in rows.items():
    f = fees.get(r["event_ticker"].split("-")[0]) or {}
    q = quotes.get(tk)
    if q is None or f.get("fee_type") not in a.FEE_TYPES or r.get("settlement_value_dollars") in (None, ""):
        continue
    sv = float(r["settlement_value_dollars"])
    opened, closed = a.ts(r["open_time"]), a.ts(r["close_time"])
    for h in a.HORIZONS_H:
        o = q["obs"].get(str(h))
        if not o or not o.get("yes_ask") or not o.get("yes_bid") or float(o.get("oi") or 0) < a.MIN_OI:
            continue
        t_obs = datetime.fromtimestamp(o["t_obs"], tz=timezone.utc)
        if not (opened <= t_obs < closed):
            continue
        ya, yb = float(o["yes_ask"]), float(o["yes_bid"])
        if not (0 < yb < ya < 1):
            continue
        mid = (ya + yb) / 2
        d = min(int(mid * 10), 9)
        for key in ((h, "all"), (h, d)):
            g = agg[key]
            g["calib"].append(sv - mid)
            g["half"].append((ya - yb) / 2)
            g["fee"].append(a.taker_fee(ya, float(f.get("fee_multiplier") or 1)))
            g["events"].append(r["event_ticker"])

print(f"{'H':>3s} {'mid':>9s} {'n':>6s} {'events':>6s} {'payout-mid':>11s} {'half-spread':>11s} {'fee':>6s}  (cents; a YES taker nets payout-mid - half-spread - fee)")
for h in a.HORIZONS_H:
    for d in ["all"] + list(range(10)):
        g = agg.get((h, d))
        if not g or len(g["calib"]) < 50:
            continue
        lab = "all" if d == "all" else f"{d / 10:.1f}-{(d + 1) / 10:.1f}"
        print(f"{h:3d} {lab:>9s} {len(g['calib']):6d} {len(set(g['events'])):6d} {np.mean(g['calib']) * 100:+10.2f}c "
              f"{np.mean(g['half']) * 100:10.2f}c {np.mean(g['fee']) * 100:5.2f}c")
    print()
