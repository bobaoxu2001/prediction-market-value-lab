"""Did the book keep selling a settled outcome? Rule fixed before the data exists.

KXBTCD settles on the average of sixty one-second BRTI prints over [00:59:00, 01:00:00),
and trading closes at 01:00:00 - the same instant. So the outcome is decided a second
at a time *while the book is still open*, and anyone watching a constituent exchange
can see how much of the average is already fixed.

At every book sweep, using only Coinbase prints the recorder had actually *received* by
then (seen_at, never the exchange's own timestamp - that is what we knew, not what was
true), we ask how far the unknown remainder of the average would have to move to flip
each rung:

    before the window   the whole average is unknown; flip needs spot to cross K
    inside the window   e seconds are fixed at mean m; the remaining 60-e seconds must
                        average K' = (60K - e*m) / (60-e); flip needs spot to reach K'

    required move = |K' - spot| / spot

A rung is LOCKED at a sweep if the required move is at least 1% and at most ten minutes
remain. Bitcoin moving 1% and holding it within ten minutes is a many-sigma event, and
Coinbase-vs-BRTI basis is hundredths of a percent, so a lock is not a certainty but it
is the thing a latency edge would be buying.

An OPPORTUNITY is a sweep where a rung is locked and its winning side is offered at a
price that clears Kalshi's taker fee (ceil to the cent of 0.07 * P * (1-P)) by at least
one cent. It is TAKEABLE only if the same offer was still there on the next sweep,
because a price seen once may be gone before an order arrives.

Judged only after settlement, and only on rungs whose final Coinbase average settled at
least 0.12% from the strike ($100 on Bitcoin at $84k): nearer than that, the proxy and
the index could disagree about the outcome itself and the rung is reported as unjudged,
not as a win.

The same rule judges Ether and Solana, whose ladders settle the same way at the same
instant; the margin is relative so that one number serves all three. Fee: the series
are quadratic with multiplier 1, which is Kalshi's standard 0.07 * P * (1-P).

    analyze.py [RECORDING_DIR] [--pretend-settle ISO]   (default: this directory, 01:00Z)
"""
import argparse, json, math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("recording", nargs="?", default=str(Path(__file__).parent))
ap.add_argument("--pretend-settle", help="dry run of the code path only; never a result")
args = ap.parse_args()
D = Path(args.recording)
meta = json.loads((D / "markets.json").read_text())
# trading closes at the instant the averaging window ends; the first recording predates
# the recorder writing close_time down, and settled at 01:00Z
SETTLE = datetime.fromisoformat(meta.get("close_time", "2026-09-24T01:00:00Z").replace("Z", "+00:00"))
if args.pretend_settle:
    SETTLE = datetime.fromisoformat(args.pretend_settle)
    print(f"DRY RUN - pretending settlement at {SETTLE:%H:%M:%S}; nothing below is a finding\n")
WINDOW_START = SETTLE - timedelta(seconds=60)
LOCK_MOVE = 0.01                       # required move to flip, as a fraction of spot
LOCK_HORIZON = timedelta(minutes=10)   # only this close to settlement
CLEAR_FRACTION = 0.0012               # judged only if settled this far from the strike
MIN_NET = Decimal("0.01")


def taker_fee(p: Decimal) -> Decimal:
    return Decimal(math.ceil(Decimal("0.07") * p * (1 - p) * 100)) / 100


def parse_exchange_time(s: str) -> datetime:
    whole, _, frac = s.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{whole}.{(frac + '000000')[:6]}+00:00")


def body(line): return json.loads(line)["body"]
books = [body(l) for l in open(D / "books.jsonl")]
prices = [p for p in (body(l) for l in open(D / "prices.jsonl")) if p.get("price")]
for p in prices:
    p["seen"] = datetime.fromisoformat(p["seen_at"])
    p["ex"] = parse_exchange_time(p["exchange_time"])
    p["px"] = float(p["price"])
prices.sort(key=lambda p: p["seen"])
seen_times = [p["seen"] for p in prices]

# The settlement proxy: one price per exchange second inside the window, by exchange time.
by_second = {}
for p in prices:
    if WINDOW_START <= p["ex"] < SETTLE:
        by_second[p["ex"].replace(microsecond=0)] = p["px"]
if len(by_second) < 30:
    raise SystemExit(f"only {len(by_second)} window seconds covered - not enough to stand in for the index")
final_avg = sum(by_second.values()) / len(by_second)
CLEAR = CLEAR_FRACTION * final_avg
print(f"{meta['event']} ({meta.get('product', 'BTC-USD')})")
print(f"Coinbase proxy for the settlement average: {final_avg:,.2f} "
      f"({len(by_second)}/60 seconds covered, range {min(by_second.values()):,.2f}-{max(by_second.values()):,.2f})")
lag = sorted((p["seen"] - p["ex"]).total_seconds() for p in prices)
print(f"our view of Coinbase lagged the exchange by {lag[len(lag)//2]:.2f}s median, {lag[-1]:.2f}s max\n")


def known_at(t):
    """Spot, and the part of the average fixed, from prints received by t."""
    i = bisect_right(seen_times, t)
    if i == 0:
        return None, 0, 0.0
    fixed = {}
    for p in prices[:i]:
        if WINDOW_START <= p["ex"] < SETTLE:
            fixed[p["ex"].replace(microsecond=0)] = p["px"]
    spot = prices[i - 1]["px"]
    if not fixed:
        return spot, 0, 0.0
    return spot, len(fixed), sum(fixed.values()) / len(fixed)


def required_move(K, spot, e, m):
    """(fraction spot must move to flip the rung, the side currently winning)."""
    if e >= 60:
        return math.inf, ("yes" if m > K else "no")
    k_flip = (60 * K - e * m) / (60 - e) if e else K
    return abs(k_flip - spot) / spot, ("yes" if spot > k_flip else "no")


judged, unjudged = {}, []
for tk, m in meta["markets"].items():
    K = float(m["floor_strike"])
    if abs(final_avg - K) < CLEAR:
        unjudged.append((tk, K))
    else:
        judged[tk] = (K, "yes" if final_avg > K else "no")

sweeps = [b for b in books if SETTLE - LOCK_HORIZON <= datetime.fromisoformat(b["seen_at"]) < SETTLE]
print(f"{len(sweeps)} book sweeps in the last {LOCK_HORIZON.seconds // 60} minutes; "
      f"{len(judged)} rungs judged, {len(unjudged)} too close to call on a proxy "
      f"({', '.join(f'{K:,.2f}' for _, K in sorted(unjudged, key=lambda x: x[1]))})\n")

opps = defaultdict(list)        # ticker -> [(t, ask, size, net, move, takeable)]
wrong_side_locks = []           # a lock that the final average contradicted - the lock rule failed
for i, b in enumerate(sweeps):
    t = datetime.fromisoformat(b["seen_at"])
    spot, e, m = known_at(t)
    if spot is None:
        continue
    nxt = sweeps[i + 1]["books"] if i + 1 < len(sweeps) else {}
    for tk, (K, won) in judged.items():
        move, locked_side = required_move(K, spot, e, m)
        if move < LOCK_MOVE:
            continue
        if locked_side != won:
            wrong_side_locks.append((t, tk))
            continue
        book = b["books"].get(tk) or {}
        ask = book.get(f"{won}_ask")
        if ask is None:
            continue
        ask = Decimal(ask)
        net = 1 - ask - taker_fee(ask)
        if net < MIN_NET:
            continue
        still = (nxt.get(tk) or {}).get(f"{won}_ask")
        takeable = still is not None and Decimal(still) <= ask
        opps[tk].append((t, ask, book.get(f"{won}_size"), net, move, takeable, e))

if wrong_side_locks:
    print(f"!! {len(wrong_side_locks)} locks pointed at the side that LOST - the lock rule is unsafe")
n_opp = sum(len(v) for v in opps.values())
n_take = sum(1 for v in opps.values() for o in v if o[5])
print(f"locked rungs offering the winning side at >= {MIN_NET} net of fee: "
      f"{n_opp} sweep-observations on {len(opps)} rungs, {n_take} still there on the next sweep\n")
cap_total = Decimal(0)
for tk in sorted(opps, key=lambda x: judged[x][0]):
    K, won = judged[tk]
    v = opps[tk]
    best = max(v, key=lambda o: o[3])
    take = [o for o in v if o[5]]
    cap = max((o[3] * Decimal(o[2] or 0) for o in take), default=Decimal(0))
    cap_total += cap
    print(f"  {tk:30s} K={K:>10,.2f} won {won:3s} {len(v):4d} sweeps ({len(take):4d} takeable)  "
          f"first {v[0][0]:%H:%M:%S} last {v[-1][0]:%H:%M:%S}  best ask {best[1]} "
          f"(net {best[3]}, size {best[2]}, flip needed {best[4]*100:.2f}%, {best[6]}s of average fixed)  "
          f"top-of-book $ {cap:.2f}")
print(f"\nlargest single takeable top-of-book profit per rung, summed: ${cap_total:.2f}")
