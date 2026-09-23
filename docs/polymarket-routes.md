# Polymarket: the market is arbitraged to its own tick boundary

Two routes existed on Polymarket that could not exist on Kalshi, and neither had ever
been measured. Both are now closed, and they closed in a more interesting way than the
Kalshi ones did.

## Why these two were worth measuring at all

On Kalshi a complete set cannot be mispriced. The asks are derived from the bids inside
one matching engine, so `yes_ask + no_ask < $1` *is* a crossed book and the engine
removes it on arrival. A zero there is close to a tautology, which is why it was only
ever used as a control.

Polymarket is different in kind. YES and NO are separate ERC-1155 tokens with separate
order books, an N-outcome group is N separate books, and nothing cross-checks any of
them. A set under a dollar is structurally *possible*. And - decisively - **999 of 1,000
Polymarket markets charge zero maker and zero taker fee**, so the fee barrier that closed
every Kalshi route does not exist here.

That combination is the best case this project ever had: no fee barrier, independent
books, and enormous depth.

## Route 1: two-way complete sets

1,679 sets priced across 2,000 active markets, in ten batch requests so the two sides of
a set are always read together.

| | |
|---|---|
| priced under $1.00 | **0** |
| median edge | −1c |
| p95 / max | **−0.001 / −0.001** |
| exactly one tick from parity | **13.5%** |
| depth behind the best | 6,473 sets |

It never crosses. An eighth of the book sits one tenth of a cent away and stops there.

## Route 2: N-way mutually exclusive groups

Polymarket's `negRisk` flag marks groups where the venue enforces that exactly one
outcome resolves YES - which is the venue-enforced partition this repo's basket proof
requires and could not previously find.

104 negRisk events scanned:

| | |
|---|---|
| refused as incomplete | **90** |
| complete, every outcome quoted | 14 |
| **priced under $1.00** | **2** |

The 90 refusals matter. Three of them are the largest events on the platform - the 2028
presidential markets, 128 outcomes each - and summing only their *quoted* outcomes gives
$0.886 to $0.921, which reads as an 8-to-11 cent arbitrage. It is not one. 75 to 86 of
the 128 outcomes have no quote at all, and their cost is not zero. This is the same
failure as the 5-of-168-rungs basket priced at $0.82 that this project refused earlier,
and the same rule refused it again.

## The two that did price under a dollar

| | Fed rate cuts 2026 | Fed Decision in December |
|---|---|---|
| legs | 13 | 5 |
| sum of best asks | $0.999 | $0.996 |
| edge per set | $0.001 | $0.004 |
| min depth | 90.09 sets | 11.64 sets |
| gross | **$0.09** | **$0.047** |
| capital | $90.00 | $11.59 |
| venue fees | **zero** | **zero** |

Both are genuinely exhaustive - 0 through 12+ cuts, and 50+ down through 50+ up - and
both are positive after everything the venue charges. They are also not tradeable, and
the reason is the tick:

| | Fed rate cuts 2026 | Fed Decision in December |
|---|---|---|
| **edge measured in ticks** | **1.0** | **0.4** |
| one leg moving one tick costs | 1.0× the whole edge | **2.5× the whole edge** |
| fills required, all or nothing | 13 | 5 |

The second one's edge is **smaller than the minimum price increment on its own worst
leg**. It is inside the quantisation of the market it trades in. The first is exactly one
tick, on a structure that needs thirteen sequential fills in thirteen independent books,
any one of which moving by the smallest amount the venue permits erases all of it.

Against this project's own standard - established when a Kalshi structure showed an 11:1
adverse-to-favourable ratio and needed a 92% fill rate merely to break even on attempts -
a one-tick edge across thirteen legs does not survive contact.

## What this closes, and what it says

Every structurally-plausible route has now been measured:

| route | closed by |
|---|---|
| Kalshi implication pairs | 787k+ observations, none clearing 1c |
| Kalshi complete sets | structurally impossible (one matching engine) |
| **Polymarket complete sets** | **1,679 sets, best −0.1c, never crosses** |
| **Polymarket N-way groups** | **2 of 104 cross, by 0.4 and 1.0 ticks** |
| Cross-venue equivalents | 1c short of fees, and floor $0.00 under real void rules |
| Maker, four policies | negative on both measured days |
| Atomic multi-leg | no mechanism exists on either venue |

The Polymarket result is the most informative of them. Given zero fees, independent
books and deep liquidity, the market still stops **at its own tick boundary** - not a
cent past it, not a cent short. That is what an efficiently arbitraged market looks like
from the inside, and it is a much stronger statement than "the fees were too high",
because here there were no fees to blame.

Both routes are now scanned daily into `edge_watch_polymarket.csv`, so the day one of
them moves further than a tick, the record will show it.
