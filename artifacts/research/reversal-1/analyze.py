"""reversal-1 analysis, as PLAN.md fixes it. EXPLORATORY - not evidence.

Reads candles.jsonl.gz (fetch.py) and the calibration sample; writes results.csv and REPORT.md.
No network. Every cell, horizon, direction and split is reported; none is chosen afterwards.
The changes made after the first partial run (PLAN.md, "Changes after the audit") are marked
"audit" below.
"""
import csv
import gzip
import json
import sys
import zlib
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
FEW_GROUPS = 10  # audit: an interval from fewer event groups is not shown as a lead


def fee(p, mult):
    """Kalshi taker fee for one contract at price p, rounded up to the cent."""
    return np.ceil(np.round(0.07 * mult * p * (1 - p) * 100, 9)) / 100


def fee100(p, mult):
    """audit: the same fee per contract when 100 contracts are traded at once (rounded on the total)."""
    return np.ceil(np.round(0.07 * mult * 100 * p * (1 - p) * 100, 9)) / 100 / 100


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
    except (EOFError, OSError, zlib.error):  # audit: a member still being written
        pass
    # final check: a broad catch must not hide a truncated read once the fetch is complete
    if (HERE / "fetched.txt").exists() and "--partial" not in sys.argv:
        done = set((HERE / "fetched.txt").read_text().split())
        if not done <= set(recs):
            sys.exit(f"candles.jsonl.gz holds {len(recs)} records but fetched.txt lists {len(done)}: "
                     "the read stopped early (pass --partial while a fetch is running)")
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
        # audit: the exit mid also accepts a one-sided book at the boundary (no bid, or no ask),
        # since leaving those out drops exactly the exits where the price ran to 0 or 1
        xvalid = (bid >= 0) & (ask > bid) & (ask <= 1) & ~((bid == 0) & (ask == 1))
    mid = np.where(valid, (bid + ask) / 2, np.nan)
    xmid = np.where(xvalid, (bid + ask) / 2, np.nan)
    spread = np.where(valid, ask - bid, np.nan)
    return grid, bid, ask, mid, spread, valid, xmid, xvalid


def find_events(mid, spread, valid, D, W, sign, tight_ref=False):
    """sign -1: drop (mid - max of prior W <= -D); +1: rise (mid - min of prior W >= D).
    tight_ref (audit, sensitivity only): the reference minutes must pass the spread filter too."""
    n = len(mid)
    fill = -np.inf if sign < 0 else np.inf
    with np.errstate(invalid="ignore"):
        ok = valid & (spread <= SPREAD_MAX + EPS)
    use = ok if tight_ref else valid
    src = np.concatenate([np.full(W, fill), np.where(use, mid, fill)])
    win = sliding_window_view(src, W)[:n]  # window m = minutes m-W .. m-1
    ref = win.max(1) if sign < 0 else win.min(1)
    with np.errstate(invalid="ignore"):
        hit = ok & np.isfinite(ref) & ((mid - ref <= -D + EPS) if sign < 0 else (mid - ref >= D - EPS))
    out, last = [], -10**9
    for m in np.flatnonzero(hit):
        if m < last + COOLDOWN:
            continue
        out.append((int(m), float(ref[m])))
        last = m
    return out


def trade(m, sign, bid, ask, mid, valid, xmid, xvalid, settle, mult):
    """Outcomes for horizons 15/60/360/settle: (net 1-lot, net 100-lot, mid change, held, exit kind);
    None when minute m+1 has no valid quote. sign -1 buys YES (after a drop), +1 buys NO (after a rise)."""
    L = len(bid) - 1
    e = m + 1
    if e > L or not valid[e]:
        return None
    entry = ask[e] if sign < 0 else 1 - bid[e]
    f_in, f_in100 = fee(entry, mult), fee100(entry, mult)
    payout = settle if sign < 0 else 1 - settle
    flip = 1 if sign < 0 else -1
    held = (payout - entry - f_in, payout - entry - f_in100, (settle - mid[e]) * flip, True, "settled")
    out = {}
    for H in HS:
        x = e + H
        if x > L:
            out[str(H)] = held
            continue
        if sign < 0:
            exit_ = bid[x] if bid[x] > 0 else 0.0
        else:
            exit_ = 1 - ask[x] if ask[x] < 1 else 0.0
        net = exit_ - entry - f_in - fee(exit_, mult)
        net100 = exit_ - entry - f_in100 - fee100(exit_, mult)
        if valid[x]:
            dmid, kind = (mid[x] - mid[e]) * flip, "two-sided"
        elif xvalid[x]:
            dmid, kind = (xmid[x] - mid[e]) * flip, "boundary"
        else:
            dmid, kind = np.nan, "no mid"
        out[str(H)] = (net, net100, dmid, False, kind)
    out["settle"] = held
    return out


def level(p):
    p = round(p, 6)  # audit: 0.2 must not fall below 0.20 by float error
    return "<20c" if p < 0.20 else ("20-80c" if p <= 0.80 else ">80c")


KINDS = (("drop", -1, False), ("rise", 1, False), ("drop_tight", -1, True), ("rise_tight", 1, True))


def collect(cal, fees, recs):
    rows = []  # one per (event or baseline minute, horizon)
    stats = defaultdict(int)
    for tk, x in recs.items():
        c = cal.get(tk)
        if c is None:
            continue
        ser = minute_series(x)
        stats["markets_fetched"] += 1
        if ser is None or not ser[5].any():  # audit: count markets that never have a valid quote
            stats["markets_without_valid_quotes"] += 1
            continue
        stats["markets_with_valid_quotes"] += 1
        grid, bid, ask, mid, spread, valid, xmid, xvalid = ser
        series = x["series"]
        meta = fees.get(series, {})
        mult = float(meta.get("fee_multiplier", 1))
        cat = meta.get("category") or "unknown"
        settle = float(c["settlement_value_dollars"])
        close = x["close"]
        group = c["event_ticker"]

        def emit(kind, sign, D, W, m, ref):
            o = trade(m, sign, bid, ask, mid, valid, xmid, xvalid, settle, mult)
            if o is None:
                stats[f"untradeable {kind}"] += 1
                return
            ttc = ">6h" if close - grid[m] > 6 * 3600 else "<=6h"
            for h, (net, net100, dmid, held, xk) in o.items():
                stats[f"exit {xk}"] += 1
                rows.append((kind, D, W, h, group, tk, cat, ttc, level(ref), net, net100, dmid, held))

        for kind, sign, tight in KINDS:
            for D in DS:
                for W in WS:
                    for m, ref in find_events(mid, spread, valid, D, W, sign, tight):
                        emit(kind, sign, D, W, m, ref)
        with np.errstate(invalid="ignore"):
            ok = np.flatnonzero(valid & (spread <= SPREAD_MAX + EPS))
        for m in ok[::BASE_EVERY]:
            emit("base_yes", -1, None, None, int(m), float(mid[m]))
            emit("base_no", 1, None, None, int(m), float(mid[m]))
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
                                     "level", "net", "net100", "dmid", "held"])
    df["D"] = df["D"].fillna(-1)
    df["W"] = df["W"].fillna(-1)
    out = []
    splits = [("all", None), ("category", "category"), ("ttc", "ttc"), ("level", "level")]
    for (kind, D, W, H), g in df.groupby(["kind", "D", "W", "H"], sort=True):
        for split, col in (splits[:1] if kind.endswith("_tight") else splits):
            parts = [("all", g)] if col is None else list(g.groupby(col))
            for val, s in parts:
                grp = s["group"].to_numpy()
                net = boot(s["net"].to_numpy(float), grp)
                n100 = boot(s["net100"].to_numpy(float), grp)
                sm = s[s["dmid"].notna()]
                dm = boot(sm["dmid"].to_numpy(float), sm["group"].to_numpy())
                out.append({
                    "kind": kind, "D_c": "" if D < 0 else round(D * 100), "W_min": "" if W < 0 else int(W),
                    "H": H, "split": split, "value": val, "events": len(s),
                    "markets": s["ticker"].nunique(), "groups": s["group"].nunique(),
                    "held_to_settle": int(s["held"].sum()),
                    "net_mean": net[0], "net_lo": net[1], "net_hi": net[2],
                    "net100_mean": n100[0], "net100_lo": n100[1], "net100_hi": n100[2],
                    "mid_n": len(sm), "mid_mean": dm[0], "mid_lo": dm[1], "mid_hi": dm[2]})
    return out


def c(v):
    return "" if v != v else f"{v * 100:+.2f}"


def ci(r, k):
    return f"{c(r[k + '_mean'])} [{c(r[k + '_lo'])}, {c(r[k + '_hi'])}]"


NAMES = {"drop": "drop -> buy YES", "rise": "rise -> buy NO (control)",
         "base_yes": "baseline buy YES", "base_no": "baseline buy NO",
         "drop_tight": "drop, tight reference", "rise_tight": "rise, tight reference"}


def table(rows, H):
    lines = ["| trade | D | W | events | groups | net 1-lot [95%] | net 100-lot [95%] | mid change [95%] |",
             "|---|---:|---:|---:|---:|---|---|---|"]
    for r in rows:
        if r["H"] == H and r["split"] == "all":
            lines.append(f"| {NAMES[r['kind']]} | {r['D_c']} | {r['W_min']} | {r['events']} | {r['groups']} | "
                         f"{ci(r, 'net')} | {ci(r, 'net100')} | {ci(r, 'mid')} |")
    return lines


def report(out, stats, n_sample):
    lines = ["# reversal-1 - does a sharp drop on Kalshi rebound?", "",
             "**EXPLORATORY. Not evidence.** Every number below is a lead for a pre-registered",
             "forward shadow test, never a result on its own (PLAN.md). Historical public candles;",
             "a taker; no order was placed.", "",
             "Units: cents per contract. `net 1-lot` = taker round trip after fees, each fee rounded up",
             "to the cent on one contract (the plan's measure); `net 100-lot` = the same with the fee",
             "rounded on 100 contracts (audit sensitivity); `mid change` = change of the mid from the",
             "entry minute, before any cost - the rebound itself. Intervals: 95%, cluster bootstrap over",
             "event groups.", "",
             f"Sample: {n_sample} markets planned; {stats['markets_fetched']} fetched; "
             f"{stats['markets_with_valid_quotes']} with a valid quote at some minute.",
             "Untradeable events (no valid quote the next minute): "
             + ", ".join(f"{k[12:]} {v}" for k, v in sorted(stats.items()) if k.startswith("untradeable ")) + ".",
             "Exits (all rows x horizons): "
             + ", ".join(f"{k[5:]} {v}" for k, v in sorted(stats.items()) if k.startswith("exit ")) + ".", ""]
    plan = [r for r in out if not r["kind"].endswith("_tight")]
    for H in HORIZONS:
        lines += [f"## Horizon {H}" + (" minutes" if H != "settle" else " (hold to settlement)"), ""]
        lines += table(plan, H) + [""]
    tested = [r for r in plan if r["kind"] in ("drop", "rise")]
    pos = [r for r in tested if r["net_lo"] == r["net_lo"] and r["net_lo"] > 0]
    few = [r for r in pos if r["groups"] < FEW_GROUPS]
    pos = [r for r in pos if r["groups"] >= FEW_GROUPS]
    n_ok = sum(r["groups"] >= FEW_GROUPS for r in tested)
    lines += ["## Rows whose net interval (1-lot) is above zero", "",
              f"{len(pos)} of the {n_ok} event rows with at least {FEW_GROUPS} event groups (all cells, horizons,",
              f"directions and splits; {len(tested)} rows in all). {len(few)} of the {len(tested) - n_ok} rows with fewer",
              "groups are above zero too, where the bootstrap interval means nothing. The rows overlap",
              "heavily (the same events in many cells, horizons and splits), so this count is a pointer,",
              "not a test.", ""]
    if pos:
        lines += ["| trade | D | W | H | split | value | events | groups | net 1-lot [95%] | mid change [95%] |",
                  "|---|---:|---:|---|---|---|---:|---:|---|---|"]
        for r in sorted(pos, key=lambda r: -r["net_lo"]):
            lines.append(f"| {r['kind']} | {r['D_c']} | {r['W_min']} | {r['H']} | {r['split']} | {r['value']} | "
                         f"{r['events']} | {r['groups']} | {ci(r, 'net')} | {ci(r, 'mid')} |")
    mpos = [r for r in tested if r["mid_lo"] == r["mid_lo"] and r["mid_lo"] > 0 and r["split"] == "all"]
    mneg = [r for r in tested if r["mid_hi"] == r["mid_hi"] and r["mid_hi"] < 0 and r["split"] == "all"]
    lines += ["", "## Overall rows whose mid change (before costs) is away from zero", "",
              f"{len(mpos)} of {sum(r['split'] == 'all' for r in tested)} overall event rows above zero "
              f"(a reversal), {len(mneg)} below zero (the move continued)."]
    if mpos or mneg:
        lines += ["", "| trade | D | W | H | events | groups | mid change [95%] | net 1-lot [95%] |",
                  "|---|---:|---:|---|---:|---:|---|---|"]
        for r in mpos + mneg:
            lines.append(f"| {r['kind']} | {r['D_c']} | {r['W_min']} | {r['H']} | {r['events']} | {r['groups']} | "
                         f"{ci(r, 'mid')} | {ci(r, 'net')} |")
    tight = [r for r in out if r["kind"].endswith("_tight")]
    lines += ["", "## Sensitivity (audit, decided after the first partial run): tight reference", "",
              "The plan's drop compares the mid with the highest mid of the previous W minutes, whatever",
              "the spread was then - so a wide quote collapsing counts as a drop. Here the reference",
              "minutes must also have a spread of 5c or less: only real price moves between tight quotes.", ""]
    for H in HORIZONS:
        lines += [f"### Horizon {H}", ""] + table(tight, H) + [""]
    lines += ["Splits for every plan row are in `results.csv`."]
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
