"""The calibration test, exactly as docs/calibration-preregistration.md fixes it.

    analyze.py exploration          all 60 rules on the exploration period; names the candidate
    analyze.py holdout BIN H        the one candidate, on the holdout, once

Exploration writes exploration.json; the holdout run refuses a rule other than the
candidate recorded there, so the holdout cannot be used to pick among rules.
"""
import json, math, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
SPLIT = datetime(2026, 8, 16, tzinfo=timezone.utc)
HORIZONS_H = (1, 6, 24)
BINS = 20
MIN_OI = 100.0
ORDER = 100                     # fee computed on a 100-contract order
TAKER_RATE = 0.07
FEE_TYPES = {"quadratic", "quadratic_with_maker_fees"}
ITER = 10_000
SEED = 20260924
FAMILY = BINS * len(HORIZONS_H)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def taker_fee(p, multiplier):
    """Per contract, on a 100-contract order, rounded up to the cent on the order."""
    return math.ceil(TAKER_RATE * multiplier * ORDER * p * (1 - p) * 100 - 1e-9) / 100 / ORDER


def load():
    rows = {}
    for line in open(HERE / "rows.jsonl"):
        r = json.loads(line)
        rows[r["ticker"]] = r
    fees = json.loads((HERE / "fees.json").read_text())
    quotes = {}
    for line in open(HERE / "quotes.jsonl"):
        q = json.loads(line)
        quotes[q["ticker"]] = q
    return rows, fees, quotes


def observations():
    rows, fees, quotes = load()
    obs, dropped = [], Counter()
    for tk, r in rows.items():
        series = r["event_ticker"].split("-")[0]
        f = fees.get(series) or {}
        if f.get("fee_type") not in FEE_TYPES:
            dropped[f"fee type {f.get('fee_type')}"] += 1
            continue
        if r.get("settlement_value_dollars") in (None, ""):
            dropped["no settlement value"] += 1
            continue
        q = quotes.get(tk)
        if q is None:
            dropped["no quotes fetched"] += 1
            continue
        sv = float(r["settlement_value_dollars"])
        mult = float(f.get("fee_multiplier") or 1)
        exp = ts(r["expected_expiration_time"])
        split = "exploration" if exp < SPLIT else "holdout"
        opened, closed = ts(r["open_time"]), ts(r["close_time"])
        for h in HORIZONS_H:
            o = q["obs"].get(str(h))
            if o is None:
                dropped["no candle at or before t_obs"] += 1
                continue
            t_obs = datetime.fromtimestamp(o["t_obs"], tz=timezone.utc)
            if not (opened <= t_obs < closed):
                dropped["not open at t_obs"] += 1
                continue
            if float(o.get("oi") or 0) < MIN_OI:
                dropped["open interest < 100 at t_obs"] += 1
                continue
            ya = float(o["yes_ask"]) if o.get("yes_ask") else None
            yb = float(o["yes_bid"]) if o.get("yes_bid") else None
            for side, price, payout in (("yes", ya, sv), ("no", (1 - yb) if yb is not None else None, 1 - sv)):
                if price is None or not (0 < price < 1):
                    continue
                net = payout - price - taker_fee(price, mult)
                obs.append({"split": split, "h": h, "bin": min(int(price * BINS), BINS - 1), "price": price,
                            "net": net, "event": r["event_ticker"], "series": series, "side": side,
                            "category": f.get("category"), "day": exp.date().isoformat()})
    return obs, dropped


def cluster_bootstrap(values, clusters, rng):
    """Means of event-resampled draws: every event is a cluster."""
    sums, counts = defaultdict(float), defaultdict(int)
    for v, c in zip(values, clusters):
        sums[c] += v
        counts[c] += 1
    keys = list(sums)
    s = np.array([sums[k] for k in keys])
    n = np.array([counts[k] for k in keys], dtype=float)
    idx = rng.integers(0, len(keys), size=(ITER, len(keys)))
    return s[idx].sum(axis=1) / n[idx].sum(axis=1), len(keys)


def bin_label(b):
    return f"[{b / BINS:.2f}, {(b + 1) / BINS:.2f})"


def exploration():
    obs, dropped = observations()
    ex = [o for o in obs if o["split"] == "exploration"]
    rng = np.random.default_rng(SEED)
    q = 0.05 / FAMILY
    print(f"exploration: {len(ex)} observations from {len({o['event'] for o in ex})} events; "
          f"holdout held back: {sum(o['split'] == 'holdout' for o in obs)} observations")
    print(f"dropped: {dict(dropped)}\n")
    table = []
    by_rule = defaultdict(list)
    for o in ex:
        by_rule[(o["bin"], o["h"])].append(o)
    print(f"{'price bin':14s} {'H':>3s} {'obs':>7s} {'events':>7s} {'mean net':>9s} {'Bonf. LB':>9s}")
    for b in range(BINS):
        for h in HORIZONS_H:
            v = by_rule.get((b, h), [])
            if len(v) < 2:
                table.append({"bin": b, "h": h, "n": len(v), "events": 0, "mean": None, "lb": None})
                continue
            draws, ne = cluster_bootstrap([o["net"] for o in v], [o["event"] for o in v], rng)
            mean = float(np.mean([o["net"] for o in v]))
            lb = float(np.quantile(draws, q))
            table.append({"bin": b, "h": h, "n": len(v), "events": ne, "mean": mean, "lb": lb})
            print(f"{bin_label(b):14s} {h:3d} {len(v):7d} {ne:7d} {mean * 100:+8.2f}c {lb * 100:+8.2f}c")
    ranked = [t for t in table if t["lb"] is not None and t["events"] >= 30]
    cand = max(ranked, key=lambda t: t["lb"])
    passed = cand["lb"] > 0
    print(f"\ncandidate: price {bin_label(cand['bin'])} at H={cand['h']}h - mean {cand['mean'] * 100:+.2f}c, "
          f"Bonferroni lower bound {cand['lb'] * 100:+.2f}c -> criterion 1 {'MET' if passed else 'NOT MET'}")
    # descriptive only: the candidate by side and by category
    v = by_rule[(cand["bin"], cand["h"])]
    for key in ("side", "category"):
        agg = defaultdict(list)
        for o in v:
            agg[o[key]].append(o["net"])
        print(f"  by {key}: " + ", ".join(f"{k} {np.mean(x) * 100:+.2f}c (n={len(x)})"
                                          for k, x in sorted(agg.items(), key=lambda kv: -len(kv[1]))[:8]))
    (HERE / "exploration.json").write_text(json.dumps({
        "candidate": {"bin": cand["bin"], "h": cand["h"]}, "criterion_1_met": passed,
        "table": table, "dropped": dict(dropped), "family": FAMILY, "iterations": ITER, "seed": SEED}, indent=1))


def holdout(b, h):
    ex = json.loads((HERE / "exploration.json").read_text())
    if ex["candidate"] != {"bin": b, "h": h}:
        raise SystemExit(f"refused: the candidate recorded by exploration is {ex['candidate']}, not bin {b} H {h}")
    obs, _ = observations()
    v = [o for o in obs if o["split"] == "holdout" and o["bin"] == b and o["h"] == h]
    rng = np.random.default_rng(SEED + 1)
    draws, ne = cluster_bootstrap([o["net"] for o in v], [o["event"] for o in v], rng)
    mean = float(np.mean([o["net"] for o in v]))
    lb = float(np.quantile(draws, 0.05))
    by_day = defaultdict(list)
    for o in v:
        by_day[o["day"]].append(o["net"])
    print(f"holdout: price {bin_label(b)} at H={h}h - {len(v)} observations, {ne} events, {len(by_day)} days")
    print(f"  mean net {mean * 100:+.2f}c per contract; one-sided 95% event-clustered lower bound {lb * 100:+.2f}c")
    print(f"  criterion 1 (exploration): {'MET' if ex['criterion_1_met'] else 'NOT MET'}; "
          f"criterion 2 (holdout): {'MET' if lb > 0 else 'NOT MET'}")
    print(f"  -> {'CALIBRATION EDGE' if ex['criterion_1_met'] and lb > 0 else 'NO CALIBRATION EDGE'}")
    (HERE / "holdout.json").write_text(json.dumps({"bin": b, "h": h, "n": len(v), "events": ne, "days": len(by_day),
        "mean": mean, "lb95": lb, "criterion_1_met": ex["criterion_1_met"], "criterion_2_met": lb > 0}, indent=1))


if __name__ == "__main__":
    if sys.argv[1] == "exploration":
        exploration()
    elif sys.argv[1] == "holdout":
        holdout(int(sys.argv[2]), int(sys.argv[3]))
