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
MARGIN = Decimal("0.3")
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

def priced_at(ticker, side, since):
    """First book at/after `since` where the locked side is gone or >= 0.99."""
    for i in range(bisect_left(book_t, since), len(books)):
        v = books[i]["books"].get(ticker, {}).get(f"{side}_ask")
        if v is None or Decimal(v) >= KNEW:
            return book_t[i]
    return None

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
    _, ask_obs = ask_at(ticker, side, t_obs)
    _, ask_pub = ask_at(ticker, side, t_pub)
    # the market is judged from the observation onward, so its lead over the feed is visible
    t_prc = priced_at(ticker, side, min(t_obs, t_pub) if start is None else max(t_obs, start))
    lead = (t_pub - t_prc).total_seconds() if t_prc else None
    edge = None
    if ask_pub is not None and ask_pub < KNEW:
        fee = taker_fee(Platform.KALSHI, Decimal(1), ask_pub, rate=KALSHI_TAKER_RATE)
        edge = Decimal(1) - ask_pub - fee
    rows.append((ticker, side, lock["temperature_f"], t_obs, t_pub, t_prc, ask_obs, ask_pub, lead, edge))

print(f"{len(books)} book sweeps, {len(obs)} observations; "
      f"{len(rows)} rungs locked while we were watching\n")
for (tk, side, T, to, tp, tr, ao, ap, lead, edge) in rows:
    who = ("market first by " + f"{lead/60:.1f} min") if lead is not None and lead > 0 else \
          ("FEED FIRST" if lead is not None else "never priced in window")
    print(f"{tk:32s} {side:3s} obs {T:>6s}F @ {to:%H:%M}  published {tp:%H:%M} "
          f"(lag {(tp-to).total_seconds()/60:4.0f}m)  priced {tr:%H:%M}" if tr else
          f"{tk:32s} {side:3s} obs {T:>6s}F @ {to:%H:%M}  published {tp:%H:%M}  priced -")
    print(f"{'':32s}     ask at obs {ao}  ask at publication {ap}  -> {who}"
          + (f"  EDGE {edge:+.3f}/ct" if edge is not None else ""))
