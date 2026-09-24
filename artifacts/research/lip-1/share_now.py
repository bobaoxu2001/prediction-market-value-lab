"""EXPLORATORY - what share of a live Liquidity Incentive pool would a new bid earn right now?

Reads the live programs and their books (read-only) and applies the filed scoring rule
(KalshiEX, August 2025 program notice, as clarified by the current help-center article):

  - bids qualify level by level from the best bid, *whole levels at a time*, until the
    cumulative size reaches the Target Size; a side that never reaches it has no
    qualifying bids, and (help center) a snapshot without both sides counts for no one
  - the reference price is the level where cumulative size reaches a fifth of the target;
    bids at or above it get full credit, deeper ones Discount Factor ** ticks
  - a user's share of a side is their credit over all qualifying credit on that side

A new bid of size S is scored at two places a maker could actually rest it:
  join     at the current best bid (it joins that level, so it qualifies whenever the
           best level is part of the qualifying set, which it always is)
  improve  one tick above the best bid, where it becomes the new best bid

Nothing here places an order. It says what the rule would pay, not what the fills cost.

    share_now.py [SAMPLE_SIZE]
"""
import asyncio, random, statistics, sys
from datetime import datetime, timezone
from decimal import Decimal

from pmvl_markets.providers.kalshi import KalshiProvider
from pmvl_shared.enums import Side

TICK = Decimal("0.01")
SIZES = (100, 300, 1000)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def side_scores(levels, target, disc):
    """{price: credit per contract}, and whether the side reaches target. Levels best first."""
    cum, ref, qualifying = Decimal(0), None, []
    for price, size in levels:
        qualifying.append(price)
        cum += size
        if ref is None and cum >= target / 5:
            ref = price
        if cum >= target:
            break
    if cum < target:
        return {}, False
    return {p: disc ** max(0, int((ref - p) / TICK)) for p in qualifying}, True


def share(levels, target, disc, price, size):
    """Our share of one side if we add `size` at `price`; None if the side cannot qualify."""
    merged = {}
    for p, q in levels:
        merged[p] = merged.get(p, Decimal(0)) + q
    ours_before = merged.get(price, Decimal(0))
    merged[price] = ours_before + size
    ordered = sorted(merged.items(), key=lambda x: -x[0])
    credit, ok = side_scores(ordered, target, disc)
    if not ok or price not in credit:
        return None if not ok else Decimal(0)
    total = sum(q * credit[p] for p, q in ordered if p in credit)
    return size * credit[price] / total


async def main(n):
    k = KalshiProvider()
    now = datetime.now(timezone.utc)
    progs, cursor = [], None
    while True:
        params = {"status": "active", "type": "liquidity", "limit": 10000}
        if cursor:
            params["cursor"] = cursor
        d = await k._client.get_json("/incentive_programs", params=params, use_cache=False)
        rows = d.get("incentive_programs") or []
        progs += [p for p in rows if ts(p["start_date"]) <= now < ts(p["end_date"])]
        cursor = d.get("next_cursor") or d.get("cursor")
        if not cursor or not rows:
            break
    random.seed(20260924)
    sample = random.sample(progs, min(n, len(progs)))
    books = {}
    tickers = [p["market_ticker"] for p in sample]
    for i in range(0, len(tickers), 100):
        cap = await k.get_orderbooks_synchronized(tickers[i:i + 100])
        books.update(cap.books)
    await k.aclose()

    rows = []
    for p in sample:
        b = books.get(p["market_ticker"])
        if b is None:
            continue
        target = Decimal(p["target_size_fp"])
        disc = Decimal(p["discount_factor_bps"]) / 10000
        rate = p["period_reward"] / 10000 / ((ts(p["end_date"]) - ts(p["start_date"])).total_seconds() / 3600)
        lv = {s: sorted(((l.price, l.size) for l in b.bids(s)), key=lambda x: -x[0]) for s in (Side.YES, Side.NO)}
        base_ok = {s: side_scores(lv[s], target, disc)[1] for s in lv}
        row = {"ticker": p["market_ticker"], "rate": rate, "both_at_target": all(base_ok.values())}
        for place in ("join", "improve"):
            for size in SIZES:
                shares = []
                for s in (Side.YES, Side.NO):
                    if not lv[s]:
                        shares.append(None)
                        continue
                    best = lv[s][0][0]
                    price = best if place == "join" else best + TICK
                    if price >= 1:
                        shares.append(None)
                        continue
                    shares.append(share(lv[s], target, disc, price, Decimal(size)))
                # a snapshot counts only with both sides at target; quoting both sides ourselves
                row[(place, size)] = None if None in shares else rate * float(sum(shares)) / 2
        rows.append(row)

    both = sum(r["both_at_target"] for r in rows)
    print(f"{len(rows)} live programs sampled at {now:%Y-%m-%d %H:%MZ} of {len(progs)} active; "
          f"pool median ${statistics.median(r['rate'] for r in rows):.2f}/h; both sides already at target: {both}")
    print("a new bid of S contracts on BOTH sides, earning share x pool (one pool per program):")
    for place in ("join", "improve"):
        for size in SIZES:
            v = sorted(r[(place, size)] for r in rows if r[(place, size)] is not None)
            if not v:
                continue
            print(f"  {place:7s} S={size:5d}: scoreable in {len(v):4d}/{len(rows)}; $/hour median {statistics.median(v):.3f}, "
                  f"p90 {v[9 * len(v) // 10]:.3f}, max {v[-1]:.2f}; summed over the sample ${sum(v):,.0f}/h")


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 400))
