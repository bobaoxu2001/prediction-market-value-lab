"""EXPLORATORY - what would the maker study's own quotes have earned from Kalshi's
Liquidity Incentive Program, next to what their fills cost?

Not a pre-registered test. The maker study froze its universe before anyone here knew
the programs existed, so which recorded markets happened to carry one is not tilted
toward a result - but the question is new, and this only decides whether a forward,
pre-registered study is worth running.

For every quote the four pre-registered maker policies place on a market while an
incentive program on it is live:

  reward   Kalshi's published scoring, applied to the recorded books: a side counts
           only when its resting size reaches the target (our quote included); orders
           at or better than the reference price (where cumulative size from the best
           bid reaches a fifth of the target) get full credit, deeper ones the discount
           factor per tick; our share of a side is our credit over everyone's. Accrued
           from when the order would rest until it fills or expires.
  fills    the same quote's QUEUE_MODEL fill, valued held to settlement, and at the
           60 s exit through the book.

Two approximations, both conservative for the reward: the recorder's ~10 s snapshots
stand in for Kalshi's 1 s sampling, and a one-sided quote is credited half its side
share (a snapshot scores at most 2.0 across both sides, and the pool pays out once).

    explore_recorded.py HISTORY_JSON SETTLEMENTS_JSONL DAY_DIR [DAY_DIR ...]
"""
import json, sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pmvl_markets.arbitrage.maker_experiment import FillEstimator, QuotePolicy, run_experiment, settlement_pnl_per_contract
from pmvl_markets.recording import load_recording, read_log
from pmvl_shared.enums import Side

TICK = Decimal("0.01")
EST = FillEstimator.QUEUE_MODEL


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


history = json.load(open(sys.argv[1]))
programs = defaultdict(list)
for p in history:
    programs[p["market_ticker"]].append(p)
latest = {}
for e in read_log(Path(sys.argv[2])):
    latest[e.body["ticker"]] = e.body
payout = {t: Decimal(str(r["settlement_value_dollars"])) for t, r in latest.items()
          if r.get("status") in ("finalized", "settled") and r.get("settlement_value_dollars") not in (None, "")}


def live_program(ticker, when):
    for p in programs.get(ticker, []):
        if ts(p["start_date"]) <= when < ts(p["end_date"]):
            return p
    return None


def side_credit(levels, target, disc, extra_price=None, extra_size=Decimal(0)):
    """(everyone else's credit, our credit, side reaches target?) for one side of one book."""
    lv = sorted(((l.price, l.size) for l in levels), key=lambda x: -x[0])
    merged = list(lv)
    if extra_price is not None and extra_size > 0:
        merged.append((extra_price, extra_size))
        merged.sort(key=lambda x: -x[0])
    cum, ref = Decimal(0), None
    for price, size in merged:
        cum += size
        if cum >= target / 5:
            ref = price
            break
    if ref is None:
        return Decimal(0), Decimal(0), False
    reaches = sum(s for _, s in merged) >= target
    others, ours, cum = Decimal(0), Decimal(0), Decimal(0)
    counted_ours = False
    for price, size in lv:
        if cum >= target:
            break
        others += size * (disc ** max(0, int((ref - price) / TICK)))
        cum += size
    if extra_price is not None and extra_size > 0:
        # our order joins its level at the back of the queue; it counts if the size ahead
        # of it (better prices and its own level) leaves room under the target
        ahead = sum(s for p, s in lv if p >= extra_price)
        if ahead < target:
            ours = min(extra_size, target - ahead) * (disc ** max(0, int((ref - extra_price) / TICK)))
            counted_ours = True
    return others, ours, reaches and (counted_ours or extra_size == 0)


policies = [QuotePolicy(side=Side(s), improve_ticks=t, name=f"{'improve' if t else 'join'}-{s}")
            for s in ("yes", "no") for t in (0, 1)]
agg = defaultdict(lambda: defaultdict(Decimal))
for day in sys.argv[3:]:
    for session_dir in sorted(Path(day).glob("session-*")):
        if not (session_dir / "snapshots.jsonl").exists():
            continue
        rec = load_recording(session_dir)
        snaps = sorted(rec.snapshots, key=lambda s: s.response_received_at)
        times = [s.response_received_at for s in snaps]
        for policy in policies:
            report = run_experiment(rec.snapshots, rec.trades, policy, strategy_version=rec.single_version or "?")
            for o in report.outcomes:
                q = o.quote
                prog = live_program(q.market, q.placed_at)
                if prog is None:
                    continue
                a = agg[policy.name]
                target = Decimal(prog["target_size_fp"])
                disc = Decimal(prog["discount_factor_bps"]) / 10000
                rate = Decimal(prog["period_reward"]) / 10000 / Decimal(
                    (ts(prog["end_date"]) - ts(prog["start_date"])).total_seconds())
                start = q.placed_at + timedelta(milliseconds=policy.placement_latency_ms)
                end = min(q.expires_at, o.first_fill_at) if o.first_fill_at else q.expires_at
                i = bisect_right(times, start) - 1
                reward = Decimal(0)
                t = start
                while t < end:
                    j = bisect_right(times, t) - 1
                    nxt = times[j + 1] if j + 1 < len(times) else end
                    seg_end = min(nxt, end)
                    book = snaps[j].books.get(q.market) if j >= 0 else None
                    if book is not None:
                        mine_side = Side.YES if q.side == Side.YES else Side.NO
                        other_side = Side.NO if mine_side == Side.YES else Side.YES
                        others, ours, ok_mine = side_credit(book.bids(mine_side), target, disc, q.price, q.size)
                        _, _, ok_other = side_credit(book.bids(other_side), target, disc)
                        if ok_mine and ok_other and ours > 0:
                            reward += rate * Decimal((seg_end - t).total_seconds()) * (ours / (others + ours)) / 2
                    t = seg_end
                a["quotes"] += 1
                a["reward"] += reward
                size = o.filled_size.get(EST, Decimal(0))
                if size > 0:
                    a["fills"] += 1
                    x = o.exit_pnl.get(60)
                    if x is not None:
                        a["exit60"] += x * size
                if q.market in payout:
                    a["settled_quotes"] += 1
                    a["settled_reward"] += reward
                    if size > 0:
                        a["hold"] += settlement_pnl_per_contract(q, payout[q.market], policy) * size

print(f"{'policy':12s} {'quotes':>7s} {'fills':>6s} {'reward/quote':>13s} {'fill@60s exit/quote':>20s} "
      f"{'| settled quotes':>16s} {'reward/quote':>13s} {'held fill/quote':>16s} {'net/quote':>10s}")
for name in sorted(agg):
    a = agg[name]
    n, ns = a["quotes"], a["settled_quotes"]
    print(f"{name:12s} {int(n):7d} {int(a['fills']):6d} {a['reward'] / n:13.4f} {a['exit60'] / n:20.4f} "
          f"{int(ns):16d} {(a['settled_reward'] / ns if ns else 0):13.4f} {(a['hold'] / ns if ns else 0):16.4f} "
          f"{((a['settled_reward'] + a['hold']) / ns if ns else 0):10.4f}")
