# Crypto settlement lag: does the book keep selling an outcome the price has decided?

The weather test found a real latency edge that free data cannot reach: the market
reacts to a five-minute station reading within about a minute, and no free feed shows
that reading for 17-46 minutes (`weather-source-lag.md`). The lesson was about the data,
not the edge. Crypto is the case where the data is free and fast.

## Why crypto is the right test

Kalshi's hourly above/below ladders (KXBTCD, KXETHD, KXSOLD, KXXRPD, KXDOGED) settle on
the simple average of sixty one-second prints of a CF Benchmarks real-time index over the
minute before the hour. The indices are built from exchange prices, Coinbase among them,
and Coinbase's public ticker is free and sub-second. Two facts from the market data make
the test sharp:

- **Trading closes at the same instant the averaging window ends** (close 01:00:00Z, window
  00:59:00-01:00:00). The outcome is fixed a second at a time *while the book is open*.
- **Fees are Kalshi's standard quadratic schedule** on all five series (multiplier 1):
  0.07 x P x (1-P), rounded up to the cent. At 0.98 that is a cent; at 0.99 it takes the
  whole margin.

## The rule, committed before the data

`artifacts/forward/crypto-settle-1/analyze.py`, committed at 00:37 UTC for a 01:00
settlement and not changed in substance since:

- At every book sweep, using only Coinbase prints *received* by then (never the exchange
  timestamp - what was knowable, not what was true), compute how far the unknown part of
  the average must move to flip each rung. Before the window that is spot crossing the
  strike; inside it, with e seconds fixed at mean m, the rest must average
  K' = (60K - e*m)/(60-e).
- **Locked**: the required move is at least 1% with at most ten minutes to go.
- **Opportunity**: a locked rung offering its winning side at a price that clears the fee
  by at least a cent. **Takeable**: the same offer is still there on the next sweep.
- Judged only on rungs whose final Coinbase average settled at least 0.12% from the strike
  ($100 on Bitcoin); nearer than that the proxy and the index could disagree about the
  outcome itself. Those rungs are reported as unjudged, never as wins.
- A lock that pointed at the side that lost is reported as a failure of the rule.

## Result: 2026-09-24 01:00 UTC, five ladders

| ladder | sweeps, last 10 min | judged / unjudged rungs | opportunities | takeable |
|---|---|---|---|---|
| KXBTCD | 1,279 | 98 / 2 | **0** | 0 |
| KXETHD | 1,288 | 98 / 2 | **0** | 0 |
| KXXRPD | 1,330 | 74 / 1 | **0** | 0 |
| KXSOLD | - | not judged: 24 of 60 window seconds covered | - | - |
| KXDOGED | - | not judged: 4 of 60 window seconds covered | - | - |

**The committed test found nothing.** No locked rung offered its winning side at a price
that clears the fee by a cent, at any sweep, on any ladder it could judge. Solana and
Dogecoin were declined by the rule's own coverage guard: Coinbase's ticker only moves its
timestamp on a trade, and those two traded in fewer than half the window's seconds. That is
the guard working, and they are reported as unjudged, not as zeros.

### Why it is empty (exploratory, `explore.py`)

The exploratory pass maps the whole curve - for every judged rung at every sweep, the move
that would flip it against what its winning side cost - with a forward-filled proxy that
covers all five assets. It is not the pre-registered test.

- **Inside the averaging window the winning side is not offered at all**, on any judged rung
  of any of the five ladders: 12,000-13,000 rung-sweeps each, zero asks. Nobody bids even a
  cent for the losing side once the average starts being fixed. There is no stale offer to
  take, at any price.
- **Before the window, price tracks the move needed**, as a volatility model would. Bitcoin:
  0.05-0.10% from flipping sells at a median 0.86; 0.10-0.20% at 0.98; 0.20-0.30% only at
  0.99; beyond 0.30% the winning side is gone. Ether is offered at 0.95-0.97 up to a 0.5%
  move, which for Ether over ten minutes is roughly a 1.4-sigma event - fair, not stale.
- The frontier where the winning side disappears sits at about 0.3% for Bitcoin and Solana
  and 0.5% for Ether - far inside the 1% the committed rule calls locked. The market treats
  as settled what the rule would, and earlier.

### The latency this machine brings to the race

| leg | measured |
|---|---|
| Coinbase exchange time to our receipt (REST poll, through this machine's proxy) | 1.1 s median, up to 10.6 s |
| round trip to Kalshi's API | 410 ms median, 520 ms p90 |

A participant hosted near both venues sees each in tens of milliseconds. Any strategy that
depends on reaching a stale quote first is lost from here by well over a second before it
starts. That is structural, not a sampling result, and it would not be changed by more
settlements.

## Verdict for this route

**CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION** - one settlement,
three judged ladders, zero opportunities, and the reason is visible: the book withdraws the
winning side before the averaging window opens and prices the approach consistently with
the move still needed. Unlike weather, faster *free* data is not what is missing: the data
here was free and fast, and the market was faster.

Replication runs hourly under `schedule.py` with the rule unchanged; its verdicts accumulate
in `artifacts/forward/crypto-settle-runs/`.
