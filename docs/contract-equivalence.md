# Contract equivalence and the arbitrage claim

How this platform decides that two contracts are the same question, and what it is
willing to call arbitrage once it has.

## The research question

Cross-platform arbitrage between Kalshi and Polymarket is widely claimed and rarely
audited. Most published examples compare two prices without establishing that the two
contracts settle on the same fact. This project asks a narrower, checkable question:

> After settlement semantics are verified, executable depth is walked, and all costs
> are deducted, how often does a genuine risk-free opportunity remain?

The honest answer is usually "none", and the system is built so that answering "none"
is cheap and answering "here is one" is expensive.

## Why title similarity is not enough

Two contracts can share almost every word and settle on different facts. Each of the
following was a real failure mode, and each now has a deterministic test in
`tests/test_settlement_semantics.py`:

| Pairing | Why it settles differently |
|---|---|
| `above 75` vs `75 or above` | On a whole-degree source these differ by the entire 75° outcome |
| `87–88°` bucket vs `above 87` | A band against an unbounded tail |
| `closes above X` vs `reaches X` | Terminal value against an intraday touch — the touch is strictly more likely |
| Chicago vs Denver high temp | Same quantity, different observation station |
| 90-minute result vs advancement | A draw settles NO on one and can settle YES on the other |
| Initial CPI print vs revision | Macro figures are revised; the two settle on different numbers |
| CF Benchmarks vs Binance | Two index methodologies disagree on the same underlying |

### The subtlest one

`>` versus `>=` was being certified as *identical*, from two independent defects that
only combined into a false claim:

1. `rules_for()` passed the venue's structured **threshold** through but not its
   structured **comparator**, so the comparator came from a regex over title and
   subtitle. Kalshi's subtitle convention restates a `>75` market inclusively as
   "76 or above", so text extraction returned `gte` for a market the venue had
   explicitly declared `greater`.
2. `is_continuous_quantity()` classed weather as continuous, making `>` and `>=`
   interchangeable.

Point 2 is the interesting one. Air temperature *is* continuous. The **settlement
source is not**: these markets settle on the NWS climatological report, which
publishes whole degrees. What matters is the granularity of the value the source
publishes, not the physics of the quantity. `settlement_step()` now encodes that, and
the comparators are treated as equivalent only when the step is negligible against
the strike — true for a $0.01 tick on a $70,000 crypto strike, false for a 1-degree
tick on a 75-degree strike.

```
equivalent(">", ">=")  ⟺  settlement_step / threshold  <  1e-4
```

## Verification is monotone

`verify_match()` starts at `IDENTICAL` and only ever demotes. There is no path that
promotes, so no combination of weak positive signals can manufacture a strong claim.
Every demotion appends a human-readable reason, and those reasons are stored, so any
accepted or rejected pair can be audited after the fact.

The final digest check is the backstop: if every individual term passed but the
normalized rule hashes still differ, some term the explicit checks do not cover
differs, and the pair is demoted rather than trusted.

## What earns the word "arbitrage"

`OpportunityClass` reserves it for a terminal payout guaranteed in **every** valid
settlement state:

| Class | Meaning |
|---|---|
| `GUARANTEED_ARBITRAGE` | Guaranteed after all costs, at fillable size. The only class that may be called arbitrage. |
| `EXECUTION_CONSTRAINED_ARBITRAGE` | Price relationship is riskless; depth, venue status or leg synchronisation prevents capture |
| `RELATIVE_VALUE` | Positive EV against an independent estimate, with real downside |
| `MODEL_DISAGREEMENT` | Difference did not survive costs or the conservative bound — research signal only |
| `WATCHLIST` | Monitored; no actionable claim |
| `REJECTED` | Evaluated and declined, with a recorded reason |

Only `RuleCompatibility.IDENTICAL` may back a `GUARANTEED_ARBITRAGE` claim. An
unmapped label defaults to the *weakest* class, and a test asserts every
`ArbitrageLabel` is explicitly mapped, so a newly added label cannot silently inherit
a claim it has not earned.

## Execution is part of the claim

An edge that cannot be filled is not an edge. `depth_profile()` reports contracts and
notional at the best price and cumulatively within one and three cents, because a
single aggregate number hides the distinction that decides whether an opportunity is
real: a 2¢ edge with depth only at the best ask is a handful of contracts; the same
edge three cents deep is a position.

An empty side returns `None`, not zero. "No book" and "$0 available" are different
claims and must not render identically.

## Venue availability

Availability is asserted only for venues read directly. Brokers that resell exchange
event contracts list a subset that changes without notice and is gated by
jurisdiction and account type, and no discovery source for them is wired up here.

`availability_for()` has **no path** from a Kalshi listing to a Moomoo listing. Broker
venues are pinned to `UNVERIFIED` regardless of where the contract was observed. A
venue that *is* read but where the contract is absent returns `CONFIRMED_UNAVAILABLE`
— a real answer rather than ignorance, and the distinction matters because only one of
the two is evidence.

## Limitations

- The hosted demo serves a frozen snapshot, not a live feed.
- Demo history is synthetic and generated by a deliberately imperfect forecaster.
- Sports, macro and politics have no independent probability model; those markets can
  only reach a cross-venue consensus estimate.
- The equity index model uses realised, not implied, volatility.
- Real track record requires calendar time to accumulate; current settled counts are
  too small to separate skill from luck, and the UI says so.
- No hosted Postgres and no continuous ingest.

## Not claimed

No guaranteed profit, no proven alpha, no consistent outperformance, no automated
trading, and no coverage of any broker's contract list. The platform is read-only,
paper-trading only, and holds no funds or keys.
