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

## The void asymmetry: checked, and it is worse than the fee gap

The two venues publish incompatible rules for a game that does not finish on schedule.

> **Kalshi** (`BASEBALLTOTALS` contract terms): "If `<game>` is suspended or postponed
> and not resumed within 48 hours of the original scheduled start time, all Contracts
> settle to the last fair market price prior to suspension unless the outcome has been
> unequivocally determined"

> **Polymarket** (market description): "If the game is postponed, this market will
> remain open until the game has been completed. If the game is canceled entirely, with
> no make-up game, this market will resolve 50-50."

| event | Kalshi | Polymarket |
|---|---|---|
| postponed, resumed within 48h | actual result | actual result |
| **postponed beyond 48h** | **settles at the last quote** | **stays open until played** |
| cancelled, no make-up | → last quote | **50-50** |

One leg cashes out at whatever the book happened to say; the other stays open for as
long as it takes. The holder is left with a directional position on a pair bought
precisely so they would not have one - and the capital behind the open leg stays locked
the whole time.

Neither behaviour existed in the payoff model, because neither is a refund, a coin flip
or a zero. Both are now first-class: `VoidPayout.MARKET_PRICE` for a payout that is a
real number nobody holds at trade time, and `VoidPayout.REMAINS_OPEN` for a leg that
does not settle on its partner's timetable. Both score at zero for a floor and both flag
the report, because a structure whose payoff depends on a future quote does not have a
floor.

Run against the pair that looked +1c:

| scenario | min payout | cost | worst case |
|---|---|---|---|
| if no void were possible | $100.00 | $102.69 | −$2.69 |
| **under the real published rules** | **$0.00** | $102.69 | **−$102.69** |
| if both venues merely refunded | $42.00 | $102.69 | −$60.69 |

So this was never an arbitrage that missed by a cent. Its worst case is the **entire
stake**, and the fee gap was the less important of the two reasons not to trade it.

## Still unproven

1. **The team map is hand-built.** `sox -> CWS` would wrongly match the Red Sox. A
   matcher that is wrong about which game it is has already produced one fake +29c edge
   in this session.
2. **Polymarket fees and settlement timing** are not in the numbers above.
3. **How often MLB games are postponed past 48 hours** is not measured here. The
   structure is unfloored whatever the frequency; the frequency would only tell you how
   often the floor is tested.

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
