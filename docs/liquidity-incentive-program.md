# Kalshi's Liquidity Incentive Program: the one income a maker has that fills do not decide

Status: **a lead, not a result.** Nothing here is pre-registered and nothing here nets
the reward against fills on a forward sample. Scripts in `artifacts/research/lip-1/`.

## Why it matters now

The calibration study found the mid calibrated and a taker's loss to be the spread plus
the fee (`calibration-results.md`); the maker study finds a resting quote's fills roughly
give that spread back to the traders who choose to hit it (`maker-study-results.md`, and
the settlement-holding interim). A maker whose fills break even needs a second source of
income. Kalshi pays one, publicly.

## What the program is

Public at `GET /incentive_programs` (read-only, no account). On 2026-09-24:

- **6,640 liquidity programs** active or upcoming across **517 series**, about **$716,000**
  in pools scheduled 2026-09-05 to 2026-10-14. `period_reward` is in centi-cents.
- Almost all use a **Target Size of 1,000** contracts and a **Discount Factor of 0.5**; no
  per-account cap is set.
- Pools range from a few dollars a day (weekly mention and count markets) to **$100 an
  hour** (Miami hourly temperature) and **$20 per 15 minutes** (FX and crypto 15-minute
  markets, in-play table tennis).

## The scoring rule (KalshiEX notice to the CFTC, August 2025; help center, current)

- The book is sampled once a second at a random instant.
- On each side, bids qualify **level by level from the best bid, whole levels at a time**,
  until cumulative size reaches the Target Size. A side that never reaches it has no
  qualifying bids; the current help-center version also drops any snapshot without both
  sides at target, for everyone.
- Bids at or above the reference price get full credit, deeper ones Discount Factor to the
  power of the ticks between. (The filing sets the reference at the best bid; the current
  help center at the level where cumulative size reaches a fifth of the target.)
- A user's share of a side is their credit over all credit; the snapshot score sums both
  sides; the period payout is the user's share of all snapshot scores times the pool,
  paid if at least $1.

**Eligibility.** The filing excludes Kalshi affiliates, members with a Market Maker
Agreement, and customers of introducing brokers and FCMs. **The current help center also
excludes international, non-U.S. users.** Kalshi's Chief Regulatory Officer may revoke
participation found "abusive or in any way inconsistent with the purpose of the Program."

## What a new bid would earn, against the books as they stood (`share_now.py`)

400 live programs sampled at 03:04 UTC; 340 already had both sides at target:

| new bid, both sides | median $/h per program | p90 | max |
|---|---|---|---|
| join the best bid, 100 contracts | 0.05 | 0.18 | 17.67 |
| join the best bid, 1,000 | 0.38 | 1.11 | 59.14 |
| one tick better, 1,000 | 0.60 | 4.17 | 80.00 |

A 1,000-contract bid on each side commits roughly $1,000 of capital per program.

## What the recorded quotes would have earned (`explore_recorded.py`)

The maker study's recordings, whose universe was frozen before these programs were known,
cover 64 markets while a program was live (54 rain, 10 Miami temperature), 701
session-market pairs. For the study's own 50-contract, 60-second quotes the reward is
**about $0.0001 a quote**, against fill losses of **$0.05-0.13 a quote** at the 60-second
exit. Two reasons, both instructive:

- On rain, both sides were at target in only 48% of snapshots, and with 4,000-6,000
  contracts resting a side, a 50-lot joining the best level is a sliver of it.
- On Miami temperature the YES side was empty in every recorded snapshot, so no snapshot
  counted and that $100-an-hour pool went to no one.

## Reading it

- **The pools are largest where quoting is most dangerous.** Kalshi pays most where makers
  are scarce - hourly weather, 15-minute FX and crypto, in-play sports - and makers are
  scarce there because informed flow picks off resting quotes. The weather study measured
  that flow directly: the market reprices within a minute of a five-minute station reading
  that free feeds show 17-46 minutes later (`weather-source-lag.md`).
- **The quiet pools are shared.** Weekly count and gas-price markets pay $2-4 an hour to a
  1,000-lot, with deep books already competing for it.
- **Completing an empty side is not a strategy here.** The formula would pay an order that
  merely lets a starved side reach target; whether that is liquidity or gaming is for
  Kalshi to decide under the clause above, and a plan whose profit depends on that answer
  is not one to build.

The live question is per market: does the reward rate exceed taker flow times the adverse
selection per filled contract? The maker study's improve-by-a-tick quotes lose about 1.5-2c
per filled contract at the 60-second exit, so a market with a few hundred contracts an hour
of taker flow loses and a quiet one might not. Only a forward shadow study - queue-modeled
fills against the tape, the reward scored second by second, both on the same quotes -
answers it, and it would have to be pre-registered before its data exists.
