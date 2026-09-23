# Cross-venue: the pairs do exist, and they are still inside the fee barrier

## What this corrects

An earlier phase of this project concluded that Kalshi and Polymarket share **no
verifiably equivalent markets**: 264k open markets narrowed to 10,662 matchable, 1,860
candidates, 14 not-incompatible, and **0 verified equivalent**. I reported that the
99.2% rejection rate was correct, because the venues list different strikes, dates,
sources and bases.

The rejection rate was correct. The conclusion drawn from it was too strong. A
**sport-specific key** - team pair, game date, and line - finds **14 genuinely
equivalent pairs** in a single sample where the generic matcher found none:

| | |
|---|---|
| Kalshi | `KXMLBTOTAL-26SEP222140LAAATH-10`, strike 9.5, "Over 9.5 runs scored" |
| Polymarket | market 4801380, "Los Angeles Angels vs. Athletics: O/U 9.5" |
| Kalshi rule | "If ... collectively score more 9.5 runs in the game originally scheduled for Sep 22, 2026 at 9:40 PM EDT" |
| Polymarket rule | "resolve to Over if ... combine to score 10 or more runs" |

`> 9.5` and `>= 10` are the same claim about the same game. The generic matcher missed
these because it compared titles; "Over 9.5 runs scored" and "Los Angeles Angels vs.
Athletics: O/U 9.5" share almost no words.

## What it does not change

Measured on genuinely synchronized books - one Kalshi batch request and one Polymarket
batch request issued together, inside a **1,314 ms** window - across 14 matched pairs
and both directions:

- most pairs sum to **$1.00 - $1.04**, i.e. a gross edge of −4c to 0c
- the best two showed **+1c gross** at displayed prices
- priced against **actual book depth** both turned out to be **−1c**, and the Kalshi
  taker fee alone adds another 1.16-1.69c per set

So the two best candidates net **−$21.62** and **−$58.35** at their executable size,
before Polymarket's fees are counted at all.

## Why it still matters

Cross-venue sits far closer to parity than anything measured inside one venue:

| structure | median gross edge | best observed |
|---|---|---|
| same-venue implication pairs | −28c | −6c |
| same-venue complete sets | −2c | −1c |
| **cross-venue equivalent pairs** | **−1c to −2c** | **+1c (gone at depth)** |

This is the only family in the project that has ever reached parity. It is still inside
the fee barrier, and it now carries *two* venues' sequencing risk rather than one, which
is worse than the single-venue case already shown to be fatal. But it is the first place
where the gap is measured in single cents rather than tens of them.

## Before any of this could be traded, three things are unproven

1. **Void asymmetry.** Kalshi settles the game "originally scheduled" for a date;
   Polymarket's wording differs. A postponement may void one leg and not the other,
   which turns a $1 floor into an open position. `payoff.py` models exactly this and it
   has not been run against these pairs.
2. **The team map is hand-built.** `sox -> CWS` would wrongly match the Red Sox. A
   matcher that is wrong about which game it is has already produced one fake +29c edge
   in this session.
3. **Polymarket fees and settlement timing** are not in the numbers above.

## The bug this session produced, and what caught it

The first run of this matcher reported **+29c, +26c and +8c** edges. All three were the
same fault: the key was (teams, line) with **no date**, and these teams play a series -
consecutive days, same line. It paired Kalshi's September 23 game against Polymarket's
September 22 one.

A fourth row was the giveaway that made me look: a full-game Kalshi market matched
against a Polymarket **1st-5-innings** market, because the `O/U` pattern was unanchored.
It showed −43c, which is not an arbitrage in either direction and does not happen
between two markets on the same event.

This is the same failure as the rain-in-Dallas pairing from the implication scan, in a
different costume: a key that does not identify the event. The date is now in the key and
the `O/U` pattern is anchored to the end of the title.
