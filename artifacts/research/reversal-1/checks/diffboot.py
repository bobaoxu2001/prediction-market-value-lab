import sys, numpy as np, pandas as pd
sys.path.insert(0, "/Users/xuao/Projects/prediction-market-value-lab/artifacts/research/reversal-1")
import analyze as A
df = pd.read_pickle("rows.pkl")
df["D"] = df["D"].fillna(-1); df["W"] = df["W"].fillna(-1)

def diffboot(ev, bs, col):
    ev = ev[ev[col].notna()]; bs = bs[bs[col].notna()]
    groups = pd.Index(pd.unique(np.concatenate([ev.group.values, bs.group.values])))
    ie = groups.get_indexer(ev.group.values); ib = groups.get_indexer(bs.group.values)
    k = len(groups)
    Se = np.bincount(ie, weights=ev[col].values, minlength=k); Ce = np.bincount(ie, minlength=k).astype(float)
    Sb = np.bincount(ib, weights=bs[col].values, minlength=k); Cb = np.bincount(ib, minlength=k).astype(float)
    point = Se.sum()/Ce.sum() - Sb.sum()/Cb.sum()
    rng = np.random.default_rng(A.SEED)
    draws = np.empty(A.B); step = max(1, 4_000_000 // k)
    for s in range(0, A.B, step):
        idx = rng.integers(0, k, size=(min(step, A.B - s), k), dtype=np.int32)
        draws[s:s+len(idx)] = Se[idx].sum(1)/Ce[idx].sum(1) - Sb[idx].sum(1)/Cb[idx].sum(1)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return point*100, lo*100, hi*100

out = []
for kind, base in (("drop", "base_yes"), ("rise", "base_no"), ("drop_tight", "base_yes"), ("rise_tight", "base_no")):
    for H in ("15", "60", "360", "settle"):
        bs = df[(df.kind == base) & (df.H == H)]
        for D in A.DS:
            for W in A.WS:
                ev = df[(df.kind == kind) & (df.H == H) & (df.D == D) & (df.W == W)]
                p, lo, hi = diffboot(ev, bs, "dmid")
                out.append((kind, round(D*100), W, H, p, lo, hi))
o = pd.DataFrame(out, columns=["kind", "D", "W", "H", "excess", "lo", "hi"])
o.to_csv("excess_mid.csv", index=False)
pd.set_option("display.width", 200); pd.set_option("display.max_rows", 300)
print(o.round(3).to_string())
for (k, H), g in o.groupby(["kind", "H"]):
    print(k, H, "excess %.2f..%.2f" % (g.excess.min(), g.excess.max()), "mean %.2f" % g.excess.mean(),
          "n lo>0:", int((g.lo > 0).sum()), "n hi<0:", int((g.hi < 0).sum()), "of", len(g))
