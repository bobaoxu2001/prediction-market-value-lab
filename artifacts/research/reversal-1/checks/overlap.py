import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/xuao/Projects/prediction-market-value-lab/artifacts/research/reversal-1")
import analyze as A
cal, fees, recs = A.load()
rows = []
for tk, x in recs.items():
    c = cal.get(tk)
    if c is None: continue
    ser = A.minute_series(x)
    if ser is None or not ser[5].any(): continue
    grid, bid, ask, mid, spread, valid, xmid, xvalid = ser
    mult = float(fees.get(x["series"], {}).get("fee_multiplier", 1))
    settle = float(c["settlement_value_dollars"]); group = c["event_ticker"]
    for D in A.DS:
        for W in A.WS:
            plan = {m for m, _ in A.find_events(mid, spread, valid, D, W, -1, False)}
            for m, ref in A.find_events(mid, spread, valid, D, W, -1, True):
                o = A.trade(m, -1, bid, ask, mid, valid, xmid, xvalid, settle, mult)
                if o is None: continue
                for h in ("15", "60", "360", "settle"):
                    rows.append((D, W, h, group, m in plan, o[h][2]))
df = pd.DataFrame(rows, columns=["D", "W", "H", "group", "in_plan", "dmid"])
out = []
for (D, W, H), g in df.groupby(["D", "W", "H"]):
    g = g[g.dmid.notna()]
    r = dict(D=round(D*100), W=W, H=H, n=len(g), share_in_plan=g.in_plan.mean(), all=g.dmid.mean()*100)
    for lab, s in (("in_plan", g[g.in_plan]), ("not_in_plan", g[~g.in_plan])):
        m, lo, hi = A.boot(s.dmid.to_numpy(float), s.group.to_numpy())
        r[lab] = m*100; r[lab+"_lo"] = lo*100; r[lab+"_hi"] = hi*100
    out.append(r)
o = pd.DataFrame(out)
pd.set_option("display.width", 220); pd.set_option("display.max_rows", 100)
print(o.round(3).to_string())
