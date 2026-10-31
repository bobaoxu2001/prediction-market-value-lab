"""Skeptic checks for reversal-1. Imports analyze.py functions; writes only to scratch."""
import sys, pickle
import numpy as np, pandas as pd
REPO = "/Users/xuao/Projects/prediction-market-value-lab/artifacts/research/reversal-1"
OUT = "/private/tmp/claude-501/-Users-xuao-Documents-2025-----AI-Projects-prediction-market-value-lab/6306567b-fa12-4743-9bb1-c4f3881d7852/scratchpad/rev1-final"
sys.path.insert(0, REPO)
import analyze as A

cal, fees, recs = A.load()
rows = []
for tk, x in recs.items():
    c = cal.get(tk)
    if c is None:
        continue
    ser = A.minute_series(x)
    if ser is None or not ser[5].any():
        continue
    grid, bid, ask, mid, spread, valid, xmid, xvalid = ser
    meta = fees.get(x["series"], {})
    mult = float(meta.get("fee_multiplier", 1))
    cat = meta.get("category") or "unknown"
    settle = float(c["settlement_value_dollars"])
    close = x["close"]; group = c["event_ticker"]
    L = len(bid) - 1
    with np.errstate(invalid="ignore"):
        ok = valid & (spread <= A.SPREAD_MAX + A.EPS)
    # market-level typical spread (median over tight-filter minutes) for selection check
    mkt_med_spread = float(np.nanmedian(spread[ok])) if ok.any() else np.nan

    def rec(kind, sign, D, W, m, ref, refwide=None):
        e = m + 1
        if e > L or not valid[e]:
            return
        o = A.trade(m, sign, bid, ask, mid, valid, xmid, xvalid, settle, mult)
        entry = ask[e] if sign < 0 else 1 - bid[e]
        f_in = A.fee(entry, mult); f_in100 = A.fee100(entry, mult)
        # momentum trade (opposite side of the plan's trade): after a rise buy YES, after a drop buy NO
        o2 = A.trade(m, -sign, bid, ask, mid, valid, xmid, xvalid, settle, mult)
        for h in ("15", "60", "360", "settle"):
            net, net100, dmid, held, xk = o[h]
            net2, net2_100, dmid2, _, _ = o2[h]
            if h != "settle" and not held:
                x_ = e + int(h)
                s_x = (ask[x_] - bid[x_]) if xk == "two-sided" else ((ask[x_] - 0) if (xk == "boundary" and bid[x_] == 0) else ((1 - bid[x_]) if xk == "boundary" else np.nan))
                exit_ = (bid[x_] if bid[x_] > 0 else 0.0) if sign < 0 else (1 - ask[x_] if ask[x_] < 1 else 0.0)
                f_out = A.fee(exit_, mult); f_out100 = A.fee100(exit_, mult)
            else:
                s_x = 0.0; f_out = 0.0; f_out100 = 0.0
            rows.append((kind, D, W, h, group, tk, cat, ">6h" if close - grid[m] > 6*3600 else "<=6h",
                         spread[m], spread[e], s_x, f_in, f_out, f_in100, f_out100, mid[e], net, net100, dmid, held,
                         net2, net2_100, dmid2, refwide, mkt_med_spread, mult))

    for kind, sign, tight in A.KINDS:
        for D in A.DS:
            for W in A.WS:
                evs = A.find_events(mid, spread, valid, D, W, sign, tight)
                for m, ref in evs:
                    # was the reference minute (argmax/argmin of valid mids in window) wide?
                    lo = max(0, m - W)
                    w_mid = mid[lo:m]; w_ok = ok[lo:m]
                    use = (w_ok if tight else valid[lo:m])
                    cand = np.where(use, w_mid, np.nan)
                    # all minutes achieving ref
                    hits = np.flatnonzero(np.abs(cand - ref) < 1e-9)
                    refwide = bool(np.all(~w_ok[hits])) if len(hits) else None  # every minute at the ref is wide
                    rec(kind, sign, D, W, m, ref, refwide)
    okm = np.flatnonzero(ok)
    for m in okm[::A.BASE_EVERY]:
        rec("base_yes", -1, None, None, int(m), float(mid[m]))
        rec("base_no", 1, None, None, int(m), float(mid[m]))

cols = ["kind","D","W","H","group","ticker","category","ttc","s_m","s_e","s_x","f_in","f_out","f_in100","f_out100",
        "mid_e","net","net100","dmid","held","net2","net2_100","dmid2","refwide","mkt_med_spread","mult"]
df = pd.DataFrame(rows, columns=cols)
df.to_pickle(OUT + "/rows.pkl")
print(len(df))
