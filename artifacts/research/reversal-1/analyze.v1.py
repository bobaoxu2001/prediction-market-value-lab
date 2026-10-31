"""reversal-1 analysis, exactly as PLAN.md fixes it. EXPLORATORY - not evidence.

Reads candles.jsonl.gz (fetch.py) and the calibration sample; writes results.csv and REPORT.md.
No network. Every cell, horizon, direction and split is reported; none is chosen afterwards.
"""
import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HERE = Path(__file__).parent
CAL = HERE.parent / "calibration-1"
DS = (0.05, 0.10, 0.15)
WS = (5, 15, 60)
HS = (15, 60, 360)
HORIZONS = ("15", "60", "360", "settle")
SPREAD_MAX = 0.05
COOLDOWN = 60
BASE_EVERY = 60
B = 2000
SEED = 20261005
EPS = 1e-9


def fee(p, mult):
    """Kalshi taker fee for one contract at price p, rounded up to the cent."""
    return np.ceil(np.round(0.07 * mult * p * (1 - p) * 100, 9)) / 100


def load():
    cal = {}
    for line in open(CAL / "rows.jsonl"):
        r = json.loads(line)
        cal[r["ticker"]] = r
    fees = json.load(open(CAL / "fees.json"))
    recs = {}
    try:
        with gzip.open(HERE / "candles.jsonl.gz", "rt") as f:
            for line in f:
                try:
                    x = json.loads(line)
                except json.JSONDecodeError:  # a line cut off by a stopped fetch
                    break
                recs[x["ticker"]] = x
    except EOFError:
        pass
    return cal, fees, recs


def minute_series(x):
    cs = [c for c in x["candles"] if c["b"] is not None and c["a"] is not None]
    t0 = -(-x["start"] // 60) * 60
    t_last = x["close"] // 60 * 60
    if not cs or t_last <= t0:
        return None
    grid = np.arange(t0, t_last + 60, 60)
    ct = np.array([c["t"] for c in cs])
    order = np.argsort(ct, kind="stable")
    ct = ct[order]
    cb = np.array([float(cs[i]["b"]) for i in order])
    ca = np.array([float(cs[i]["a"]) for i in order])
    j = np.searchsorted(ct, grid, side="right") - 1
    jj = np.clip(j, 0, None)
    bid = np.where(j >= 0, cb[jj], np.nan)
    ask = np.where(j >= 0, ca[jj], np.nan)
    with np.errstate(invalid="ignore"):
        valid = (bid > 0) & (ask > bid) & (ask < 1)
    mid = np.where(valid, (bid + ask) / 2, np.nan)
    spread = np.where(valid, ask - bid, np.nan)
    return grid, bid, ask, mid, spread, valid


def find_events(mid, spread, valid, D, W, sign):
    """sign -1: drop (mid - max of prior W <= -D); +1: rise (mid - min of prior W >= D)."""
    n = len(mid)
    fill = -np.inf if sign < 0 else np.inf
    src = np.concatenate([np.full(W, fill), np.where(valid, mid, fill)])
    win = sliding_window_view(src, W)[:n]  # window m = minutes m-W .. m-1
    ref = win.max(1) if sign < 0 else win.min(1)
    with np.errstate(invalid="ignore"):
        ok = valid & (spread <= SPREAD_MAX + EPS) & np.isfinite(ref)
        hit = ok & ((mid - ref <= -D + EPS) if sign < 0 else (mid - ref >= D - EPS))
    out, last = [], -10**9
    for m in np.flatnonzero(hit):
        if m < last + COOLDOWN:
            continue
        out.append((int(m), float(ref[m])))
        last = m
    return out


def trade(m, sign, bid, ask, mid, valid, settle, mult):
    """Outcomes for horizons 15/60/360/settle; None when minute m+1 has no valid quote.
    sign -1 buys YES (after a drop), +1 buys NO (after a rise)."""
    L = len(bid) - 1
    e = m + 1
    if e > L or not valid[e]:
        return None
    entry = ask[e] if sign < 0 else 1 - bid[e]
    f_in = fee(entry, mult)
    payout = settle if sign < 0 else 1 - settle
    flip = 1 if sign < 0 else -1
    out = {}
    for H in HS:
        x = e + H
        if x > L:
            out[str(H)] = (payout - entry - f_in, (settle - mid[e]) * flip, True)
            continue
        if sign < 0:
            exit_ = bid[x] if bid[x] > 0 else 0.0
        else:
            exit_ = 1 - ask[x] if ask[x] < 1 else 0.0
        net = exit_ - entry - f_in - fee(exit_, mult)
        dmid = (mid[x] - mid[e]) * flip if valid[x] else np.nan
        out[str(H)] = (net, dmid, False)
    out["settle"] = (payout - entry - f_in, (settle - mid[e]) * flip, True)
    return out



def level(p):
    return "<20c" if p < 0.20 else ("20-80c" if p <= 0.80 else ">80c")


def collect(cal, fees, recs):
    rows = []  # one per (event or baseline minute, horizon)
    stats = defaultdict(int)
    for tk, x in recs.items():
        c = cal.get(tk)
        if c is None:
            continue
        ser = minute_series(x)
        stats["markets_fetched"] += 1
        if ser is None:
            stats["markets_without_quotes"] += 1
            continue
        stats["markets_with_quotes"] += 1
        grid, bid, ask, mid, spread, valid = ser
        series = x["series"]
        meta = fees.get(series, {})
        mult = float(meta.get("fee_multiplier", 1))
        cat = meta.get("category") or "unknown"
        settle = float(c["settlement_value_dollars"])
        close = x["close"]
        group = c["event_ticker"]

        def emit(kind, D, W, m, ref):
            sign = -1 if kind in ("drop", "base_yes") else 1
            o = trade(m, sign, bid, ask, mid, valid, settle, mult)
            if o is None:
                stats[f"untradeable_{kind}"] += 1
                return
            ttc = ">6h" if close - grid[m] > 6 * 3600 else "<=6h"
            for h, (net, dmid, held) in o.items():
                rows.append((kind, D, W, h, group, tk, cat, ttc, level(ref), net, dmid, held))

        for sign, kind in ((-1, "drop"), (1, "rise")):
            for D in DS:
                for W in WS:
                    for m, ref in find_events(mid, spread, valid, D, W, sign):
                        emit(kind, D, W, m, ref)
        with np.errstate(invalid="ignore"):
            ok = np.flatnonzero(valid & (spread <= SPREAD_MAX + EPS))
        for m in ok[::BASE_EVERY]:
            emit("base_yes", None, None, int(m), float(mid[m]))
            emit("base_no", None, None, int(m), float(mid[m]))
    return rows, stats


def boot(vals, groups):
    """Pooled mean and the 95% cluster-bootstrap interval over groups."""
    if len(vals) == 0:
        return (np.nan, np.nan, np.nan)
    _, inv = np.unique(groups, return_inverse=True)
    k = inv.max() + 1
    S = np.bincount(inv, weights=vals, minlength=k)
    C = np.bincount(inv, minlength=k).astype(float)
    mean = S.sum() / C.sum()
    if k < 2:
        return (mean, np.nan, np.nan)
    rng = np.random.default_rng(SEED)
    draws = np.empty(B)
    step = max(1, 4_000_000 // k)
    for s in range(0, B, step):
        idx = rng.integers(0, k, size=(min(step, B - s), k), dtype=np.int32)
        draws[s:s + len(idx)] = S[idx].sum(1) / C[idx].sum(1)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return (mean, lo, hi)


def summarise(rows):
    import pandas as pd
    df = pd.DataFrame(rows, columns=["kind", "D", "W", "H", "group", "ticker", "category", "ttc",
                                     "level", "net", "dmid", "held"])
    df["D"] = df["D"].fillna(-1)
    df["W"] = df["W"].fillna(-1)
    out = []
    splits = [("all", None), ("category", "category"), ("ttc", "ttc"), ("level", "level")]
    for (kind, D, W, H), g in df.groupby(["kind", "D", "W", "H"], sort=True):
        for split, col in splits:
            parts = [("all", g)] if col is None else list(g.groupby(col))
            for val, s in parts:
                net = boot(s["net"].to_numpy(float), s["group"].to_numpy())
                sm = s[s["dmid"].notna()]
                dm = boot(sm["dmid"].to_numpy(float), sm["group"].to_numpy())
                out.append({
                    "kind": kind, "D_c": "" if D < 0 else round(D * 100), "W_min": "" if W < 0 else int(W),
                    "H": H, "split": split, "value": val, "events": len(s),
                    "markets": s["ticker"].nunique(), "groups": s["group"].nunique(),
                    "held_to_settle": int(s["held"].sum()),
                    "net_mean": net[0], "net_lo": net[1], "net_hi": net[2],
                    "mid_n": len(sm), "mid_mean": dm[0], "mid_lo": dm[1], "mid_hi": dm[2]})
    return out


def c(v):
    return "" if v != v else f"{v * 100:+.2f}"


def report(out, stats, n_sample):
    lines = ["# reversal-1 - does a sharp drop on Kalshi rebound?", "",
             "**EXPLORATORY. Not evidence.** Every number below is a lead for a pre-registered",
             "forward shadow test, never a result on its own (PLAN.md). Historical public candles;",
             "a one-lot taker; no order was placed.", "",
             "Units: cents per contract. `net` = taker round trip after fees; `mid` = change of the mid",
             "from the entry minute (the rebound itself, before any cost). Intervals: 95%, cluster",
             "bootstrap over event groups.", "",
             f"Sample: {n_sample} markets planned; {stats['markets_fetched']} fetched; "
             f"{stats['markets_with_quotes']} with quotes.",
             "Untradeable events (no valid quote the next minute): "
             + ", ".join(f"{k[12:]} {v}" for k, v in sorted(stats.items()) if k.startswith("untradeable_")), ""]
    for H in HORIZONS:
        lines += [f"## Horizon {H}" + (" minutes" if H != "settle" else " (hold to settlement)"), "",
                  "| trade | D | W | events | groups | net [95%] | mid change [95%] |",
                  "|---|---:|---:|---:|---:|---|---|"]
        for r in out:
            if r["H"] != H or r["split"] != "all":
                continue
            name = {"drop": "drop -> buy YES", "rise": "rise -> buy NO (control)",
                    "base_yes": "baseline buy YES", "base_no": "baseline buy NO"}[r["kind"]]
            lines.append(f"| {name} | {r['D_c']} | {r['W_min']} | {r['events']} | {r['groups']} | "
                         f"{c(r['net_mean'])} [{c(r['net_lo'])}, {c(r['net_hi'])}] | "
                         f"{c(r['mid_mean'])} [{c(r['mid_lo'])}, {c(r['mid_hi'])}] |")
        lines.append("")
    tested = [r for r in out if r["kind"] in ("drop", "rise")]
    pos = [r for r in tested if r["net_lo"] == r["net_lo"] and r["net_lo"] > 0]
    lines += ["## Every row whose net interval is above zero", "",
              f"{len(pos)} of {len(tested)} event rows (all cells, horizons, directions and splits). By",
              f"chance alone about {0.025 * len(tested):.0f} would be (2.5% each, ignoring that rows overlap).", ""]
    if pos:
        lines += ["| trade | D | W | H | split | value | events | groups | net [95%] | mid change [95%] |",
                  "|---|---:|---:|---|---|---|---:|---:|---|---|"]
        for r in sorted(pos, key=lambda r: -r["net_lo"]):
            lines.append(f"| {r['kind']} | {r['D_c']} | {r['W_min']} | {r['H']} | {r['split']} | {r['value']} | "
                         f"{r['events']} | {r['groups']} | {c(r['net_mean'])} [{c(r['net_lo'])}, {c(r['net_hi'])}] | "
                         f"{c(r['mid_mean'])} [{c(r['mid_lo'])}, {c(r['mid_hi'])}] |")
    mpos = [r for r in tested if r["mid_lo"] == r["mid_lo"] and r["mid_lo"] > 0 and r["split"] == "all"]
    lines += ["", "## Overall rows whose mid change (before costs) is above zero", "",
              f"{len(mpos)} of {sum(r['split'] == 'all' for r in tested)} overall event rows."]
    if mpos:
        lines += ["", "| trade | D | W | H | events | groups | mid change [95%] | net [95%] |",
                  "|---|---:|---:|---|---:|---:|---|---|"]
        for r in mpos:
            lines.append(f"| {r['kind']} | {r['D_c']} | {r['W_min']} | {r['H']} | {r['events']} | {r['groups']} | "
                         f"{c(r['mid_mean'])} [{c(r['mid_lo'])}, {c(r['mid_hi'])}] | "
                         f"{c(r['net_mean'])} [{c(r['net_lo'])}, {c(r['net_hi'])}] |")
    lines += ["", "Splits for every row are in `results.csv`."]
    return "\n".join(lines) + "\n"


def main():
    cal, fees, recs = load()
    sys.path.insert(0, str(HERE))
    from fetch import sample
    n_sample = len(sample())
    rows, stats = collect(cal, fees, recs)
    out = summarise(rows)
    with open(HERE / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    (HERE / "REPORT.md").write_text(report(out, stats, n_sample))
    print(dict(stats), len(rows), "outcome rows,", len(out), "summary rows")


if __name__ == "__main__":
    main()
