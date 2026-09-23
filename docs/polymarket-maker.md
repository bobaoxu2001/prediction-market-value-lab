# Polymarket maker: the cost is gone, and so is the edge

One session, 2026-09-23 15:25-16:10 UTC: 40 binary markets, 179 synchronized book
snapshots, 7,339 prints, 798 quotes per policy. **Hypothesis-generating, not a test** - there
is no Polymarket pre-registration, and one session is one draw.

## Why this was the last route worth measuring

The Kalshi maker route died on the hedged pair - buy the fill, close it, pay the spread and
a taker fee - at **−2.7c to −4.1c** a contract on every policy. Polymarket charges **zero**
maker and zero taker fee on 999 of 1,000 markets and ticks at **0.001** instead of 0.01. The
dominant term in that loss does not exist there. That made it the one place a maker edge
could plausibly survive.

## The cost did go away

| | Kalshi | **Polymarket** |
|---|---|---|
| hedged pair, per contract | −2.7c to −4.1c | **+0.05c (join) / −0.13c (improve)** |

The prediction was right: remove the fee and tighten the tick, and the immediate-hedge loss
collapses to roughly nothing. It collapses to nothing, not to something positive.

## Holding for five minutes looked positive, and is not

| | @30 s | @60 s | @300 s |
|---|---|---|---|
| E[PnL]/quote, join (mid-mark) | −$0.008 | −$0.047 | +$0.011 |
| **E[PnL]/quote, join (realizable exit)** | | −$0.045 | **+$0.063** |

A positive 300-second number is the first positive maker figure in this project, which is
the reason it got the hardest look. Three things are wrong with it:

1. **Horizon.** 300 s was examined after 30 s and 60 s had come out negative. Nothing
   pre-registered it.
2. **It is one market.** The top 3 fills are **113%** of the total ($56.61 of $49.95); the
   other 37 fills lose **−$6.66**; the **median fill is exactly +0.0000**. Every large
   contributor is on one Bitcoin price ladder that moved sharply, and the largest *loss*
   (−$25.55) is on the same ladder. That is directional exposure to one underlying.
3. **Clustered properly, it straddles zero.** Resampling whole markets: join
   **[−0.0004, +0.194]**, improve **[−0.0041, +0.166]**. Remove the one market and the rest is
   **+$0.005** and **+$0.001** per quote.

The realizable exit exceeding the mid-mark was the first warning. Closing through the book
pays half a spread and should normally come out *below* a mid-mark. It came out five times
above it because the mark needs both sides of a token's book and scores zero when a large
move leaves the book one-sided - which dropped exactly the fills the exit counted.

## Four measurement faults this session found, all pulling toward zero or toward confidence

| fault | effect | fix |
|---|---|---|
| capture held in memory | "zero fills on 538 quotes" could not be replayed | append-only logs, same as Kalshi |
| trade feed indexes in **batches** | a window's prints unpublished when recording stops | measured horizon; drain command |
| **most fills are mints** (a YES buyer against a NO bidder) | tape shows only the complement buy, so bids "never fill" | infer 1 − p fills at replay |
| intervals clustered by **token** | YES and NO of one market counted as two draws | cluster by market |

The mint fault is the large one. On the markets checked, 116 of 159 and 146 of 184
transactions touched both tokens. Without the inference, this session's bid-fillable prints
fall from 197 to 76.

## Where this leaves the maker question

| venue | immediate hedge | unconditional expectancy |
|---|---|---|
| Kalshi, 2 days | **−2.7c to −4.1c**, every fill | negative on all 4 policies, both days |
| Polymarket, 1 session | **≈ 0** | −$0.047 @60 s (join interval excludes zero, improve straddles); positive @300 s only through one market |

Polymarket removes the structural cost and leaves no structural edge behind it. A properly
pre-registered multi-session Polymarket study would settle the sign; the prior from this
session is that it would find zero.
