import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/xuao/Projects/prediction-market-value-lab/artifacts/research/reversal-1")
import analyze as A
df = pd.read_pickle("rows.pkl")
pd.set_option("display.width", 220); pd.set_option("display.max_rows", 300)
print("refwide None count (event kinds):", int(df[df.kind.isin(['drop','rise','drop_tight','rise_tight'])].refwide.isna().sum()))
out = []
for kind in ("rise", "rise_tight", "drop", "drop_tight"):
    for H in ("15", "60", "360", "settle"):
        for D in A.DS:
            for W in A.WS:
                ev = df[(df.kind == kind) & (df.H == H) & (df.D == D) & (df.W == W)]
                g = ev.group.to_numpy()
                n1 = A.boot(ev.net2.to_numpy(float), g); n100 = A.boot(ev.net2_100.to_numpy(float), g)
                sm = ev[ev.dmid2.notna()]
                dm = A.boot(sm.dmid2.to_numpy(float), sm.group.to_numpy())
                out.append(dict(kind=kind, trade=("buy YES (momentum)" if kind.startswith("rise") else "buy NO (momentum)"),
                                D=round(D*100), W=W, H=H, net=n1[0]*100, lo=n1[1]*100, hi=n1[2]*100,
                                net100=n100[0]*100, lo100=n100[1]*100, hi100=n100[2]*100, dmid=dm[0]*100, cost=(dm[0]-n1[0])*100))
o = pd.DataFrame(out)
o.to_csv("momentum.csv", index=False)
print(o.round(2).to_string())
print("any momentum row with lo>0:", int((o.lo > 0).sum()), " lo100>0:", int((o.lo100 > 0).sum()), " hi>0:", int((o.hi > 0).sum()), " hi100>0:", int((o.hi100 > 0).sum()))
print(o[o.hi100 > 0].round(2).to_string())
