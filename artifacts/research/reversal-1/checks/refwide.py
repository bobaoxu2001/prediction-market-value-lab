import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/xuao/Projects/prediction-market-value-lab/artifacts/research/reversal-1")
import analyze as A
df = pd.read_pickle("rows.pkl")
pd.set_option("display.width", 220); pd.set_option("display.max_rows", 300)
out = []
for kind in ("drop", "rise"):
    for H in ("15", "60", "360", "settle"):
        for D in A.DS:
            for W in A.WS:
                ev = df[(df.kind == kind) & (df.H == H) & (df.D == D) & (df.W == W)]
                ev = ev[ev.dmid.notna()]
                allm = ev.dmid.mean() * 100
                r = {"kind": kind, "D": round(D*100), "W": W, "H": H, "n": len(ev), "share_wide": ev.refwide.mean(), "all": allm}
                for lab, s in (("wide", ev[ev.refwide == True]), ("tight", ev[ev.refwide == False])):
                    m, lo, hi = A.boot(s.dmid.to_numpy(float), s.group.to_numpy())
                    r[lab] = m*100; r[lab+"_lo"] = lo*100; r[lab+"_hi"] = hi*100
                # share of the total pooled sum contributed by wide-ref events
                r["wide_share_of_sum"] = ev[ev.refwide == True].dmid.sum() / ev.dmid.sum()
                out.append(r)
o = pd.DataFrame(out)
print(o.round(3).to_string())
o.to_csv("refwide.csv", index=False)
