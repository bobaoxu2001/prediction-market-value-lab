"""For each rung that locked during the recording: who knew first, the market or the feed?

A rung locks when an observation proves the outcome - the daily max can only rise, so
once a reading clears a threshold the answer is fixed. Three moments matter:

  observed   when the temperature actually happened (observation_time)
  published  when the public NWS feed first showed it to us (seen_at)
  priced     when the book stopped offering the locked side below $0.99

If `priced` comes before `published`, the market was faster than the public feed and
there is nothing to trade on it. Only if `priced` comes after is there a window, and
the edge in it is 1 - ask - fee at the moment of publication.
"""
import json, sys
from bisect import bisect_left
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from pmvl_markets.pricing.fees import KALSHI_TAKER_RATE, taker_fee
from pmvl_shared.enums import Platform

D = Path(__file__).parent
#: Half a degree Celsius. Five-minute ASOS readings arrive in whole degrees C, so
#: 96.8F is really "36C", which only proves the true temperature was in
#: [95.9, 97.7)F. A 0.3F margin claimed locks on readings that could not prove
#: them; half a degree C is the widest bin any reading here can occupy.
MARGIN = Decimal("0.9")
KNEW = Decimal("0.99")

def body(line): return json.loads(line)["body"]
markets = json.loads((D / "markets.json").read_text())["markets"]
books = [body(l) for l in open(D / "books.jsonl")]
book_t = [datetime.fromisoformat(b["seen_at"]) for b in books]
obs = sorted((body(l) for l in open(D / "observations.jsonl")),
             key=lambda r: r["observation_time"])
series_of = {t: t.split("-")[0] for t in markets}

def lock_side_and_test(rule):
    st = rule["strike_type"]
    if st == "greater":
        x = Decimal(str(rule["floor_strike"])); return "yes", lambda T: T >= x + 1 + MARGIN
    if st == "less":
        x = Decimal(str(rule["cap_strike"])); return "no", lambda T: T >= x + MARGIN
    if st == "between":
        c = Decimal(str(rule["cap_strike"])); return "no", lambda T: T >= c + 1 + MARGIN
    return None, None

def ask_at(ticker, side, when):
    i = bisect_left(book_t, when)
    if i >= len(books): return "after_end", None
    v = books[i]["books"].get(ticker, {}).get(f"{side}_ask")
    return books[i]["seen_at"], (Decimal(v) if v is not None else None)

def cheapest_offer(ticker, side, lo, hi):
    """Lowest offer on `side` seen in [lo, hi], or None if it was never offered.

    This is the question the thesis actually turns on. "When did the market price
    it" is misleading when the side was already priced before anything happened -
    that returned the first book and read as a 20-minute lead. What matters is
    whether a trader acting on the feed could ever have bought the certain side
    below a dollar.
    """
    best = None
    for i in range(bisect_left(book_t, lo), len(books)):
        if book_t[i] > hi:
            break
        v = books[i]["books"].get(ticker, {}).get(f"{side}_ask")
        if v is not None:
            v = Decimal(v)
            best = v if best is None or v < best else best
    return best

rows = []
start = book_t[0] if book_t else None
#: Everything returned by the very first poll was already published before we were
#: watching, so its seen_at is our start time, not a publication time. Only an
#: observation that first appears on a *later* poll has a seen_at that bounds its
#: publication to within one polling interval.
from datetime import timedelta as _td
_first = min((datetime.fromisoformat(r["seen_at"]) for r in obs), default=None)
#: the first poll is a round across every station, fetched seconds apart, so it is a
#: window rather than one timestamp
first_round_end = _first + _td(seconds=30) if _first else None
for ticker, rule in sorted(markets.items()):
    side, test = lock_side_and_test(rule)
    if side is None: continue
    station_obs = [r for r in obs if r["series"] == series_of[ticker]]
    lock = next((r for r in station_obs if test(Decimal(r["temperature_f"]))), None)
    if lock is None: continue
    if first_round_end and datetime.fromisoformat(lock["seen_at"]) <= first_round_end:
        continue   # already published before we started watching: no lock moment seen
    t_obs = datetime.fromisoformat(lock["observation_time"])
    t_pub = datetime.fromisoformat(lock["seen_at"])
    #: the window a trader on the public feed could act in: from publication onward
    window_end = t_pub + __import__("datetime").timedelta(minutes=10)
    before = cheapest_offer(ticker, side, book_t[0], t_obs) if book_t else None
    after = cheapest_offer(ticker, side, t_pub, window_end)
    edge = None
    if after is not None and after < KNEW:
        fee = taker_fee(Platform.KALSHI, Decimal(1), after, rate=KALSHI_TAKER_RATE)
        edge = Decimal(1) - after - fee
    rows.append((ticker, side, lock["temperature_f"], t_obs, t_pub, before, after, edge))

print(f"{len(books)} book sweeps, {len(obs)} observations; "
      f"{len(rows)} rungs locked while we were watching\n")
for (tk, side, T, to, tp, before, after, edge) in rows:
    print(f"{tk:32s} {side:3s} {T:>7s}F at {to:%H:%M}, public at {tp:%H:%M} "
          f"(feed lag {(tp - to).total_seconds() / 60:.0f}m)")
    print(f"{'':32s}     cheapest {side} offer before the crossing: {before}")
    print(f"{'':32s}     cheapest {side} offer once public (+10m): {after}"
          + (f"  -> EDGE {edge:+.3f}/ct" if edge is not None else "  -> nothing to buy"))
