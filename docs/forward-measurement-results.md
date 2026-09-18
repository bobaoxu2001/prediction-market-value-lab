# Forward measurement: results

**VERDICT: `CURRENT_MARKET_MICROSTRUCTURE_DOES_NOT_SUPPORT_PROFITABLE_EXECUTION`**

One recording, 2026-09-18, 11:33-12:23 UTC. Eight events selected by volume from a
census of 336,325, 300 capture cycles, **2,400 synchronized snapshots, zero
rejections**, 23,700 legs captured of which 15,681 were two-sided, 2,076 prints, both
hash chains intact. Leg skew bound: **283 ms median, 420 ms p95, 1,671 ms worst** -
against 10,762 seconds in the phase this replaces.

Recording under `STRATEGY_VERSION` 1.0.0, analysed under `ANALYSIS_VERSION` 1.1.0.

## 1. No dislocation, on either structure, at any threshold

| structure | observations | ≥1c | ≥3c | ≥5c | near-miss | p50 | best ever |
|---|---|---|---|---|---|---|---|
| implication pairs | 16,722 | **0** | **0** | **0** | 0 | −28c | **−6c** |
| ...adjacent rungs only | 3,171 | 0 | 0 | 0 | 0 | −9c | −6c |
| complete set (control) | 15,681 | **0** | **0** | **0** | 0 | −2c | **−1c** |

Not one observation in 32,403 cleared even one cent, and not one came within a cent of
clearing. There are no episodes to measure the duration, depth or capacity of, because
there were no episodes.

247,200 candidate implication pairs were refused by the proof and 28,278 provable ones
could not be priced because a leg was unquoted on the side it would have to be bought.
Both counts are in the report; a denominator of only the priceable pairs would make the
market look more tradeable than it is.

**The complete-set control behaved as predicted, which is how you know the instrument
works.** Its best observation was exactly one tick from parity and never crossed,
because on Kalshi a complete set under a dollar *is* a crossed book and the matching
engine removes it on arrival. A zero there is close to a tautology - and it means that
where simultaneity is free, there is still nothing.

**Why the implication pair is so far from money.** At mid, the pair costs
`p_low + (1 − p_high)` = `1 + (p_low − p_high)`. It starts above a dollar by the whole
probability gap between the rungs, before any spread is paid. Distant rungs are not
slightly unprofitable, they are structurally unprofitable, which is the −28c median.
Adjacent rungs are the only sub-population that can be near money, and there the
median pair costs **$1.09** and the best pair ever seen cost **$1.06**. The strategy
needs it under $0.99, and under about $0.96 to survive one adverse fill.

## 2. The maker route is negative on every policy and every admissible estimator

2,309 quotes per policy, 42 markets, 8 event-days - **all within one session**, which
is one date, not eight independent draws in time.

| policy | fills | fill rate | completion | markout 60s \| fill | hedge cost | **hedged pair PnL** | E[PnL] / quote | 95% CI |
|---|---|---|---|---|---|---|---|---|
| join-yes | 33 | 1.43% | 45% | −2.88c | 60.2c | **−8.30c** | −1.56c | [−5.4c, +0.25c] |
| improve-yes | 56 | 2.43% | 52% | −0.91c | 58.1c | **−6.30c** | −2.24c | [−6.5c, +0.24c] |
| join-no | 44 | 1.91% | 66% | −0.76c | 59.3c | **−4.28c** | −0.92c | [−4.6c, +2.37c] |
| improve-no | 96 | 4.16% | 61% | −0.42c | 51.0c | **−3.11c** | −0.67c | [−4.1c, +2.04c] |

Queue-model estimator. All four policies were declared in the source and all four are
reported; the best of four is not a finding.

Two different statements, with different strengths:

**The hedged pair is decisively negative and needs no statistics.** A maker who refuses
inventory buys the quote and immediately buys the other side, which settles for exactly
$1. That is `1 − price − hedge cost`, an accounting identity per fill, and it is
**−3.1c to −8.3c** across every policy. The spread crossed to get out is wider than the
spread earned resting. There is no sample-size question here: it is negative on every
fill, not on average.

**The inventory-carrying branch is under-powered and centred negative.** Expectancy per
quote is negative for all four policies, but every interval straddles zero, and one
session is not a sample. That branch is `INSUFFICIENT_EVIDENCE` on its own and is
*not* what this verdict rests on - carrying inventory is taking a view, not executing.

**The inadmissible estimator is the only one that ever turns positive.** Under
`TOUCHED`, join-no reports **+1.04c** per quote against −0.92c under the queue model.
Counting a print at your price as your fill flips the sign of the headline. That is the
entire reason the estimator exists in the code, marked inadmissible, rather than being
left out.

A note on small samples, from this project's own data: at the five-minute mark,
improve-no showed a **+5.25c** markout on 3 fills. At 96 fills it is **−0.42c**.

## 3. No atomic route

[Separately audited](kalshi-atomic-execution-audit.md): single-leg orders only, a batch
endpoint whose per-order `error` field makes partial failure a designed outcome, FIX
that is `NewOrderSingle`, order groups that are a rate limiter, and a multivariate
collection that mints parlays but cannot absorb or emit leg positions. Netting and
collateral return reduce capital, not the risk in the gap between two fills.

So even had a dislocation appeared, it would face the sequencing loss measured in the
previous phase: +$33.45 atomic, −$52.80 sequential, −$387.82 on a failed second leg.

## 4. What this verdict does and does not say

It says: on this venue, in this session, across 32,403 synchronized observations of
119 deterministic structures and 9,236 maker quotes, **there was no priced
inefficiency large enough to pay the cost of taking it**, and the one route that needs
no view - quote, fill, hedge - loses three to eight cents a contract by construction.

It does not say the market is efficient in general, that no strategy can work, or that
a different venue behaves this way. It covers one date, one exchange, eight events and
a two-leg structure family. A strategy that carries inventory, holds to settlement,
trades a different asset class, or operates where the spread is not the dominant cost
is untested here and is not refuted by this.

## 5. What would change it

Nothing about better data collection. The recorder now measures skew in hundreds of
milliseconds and the answer did not move; the barrier is not measurement.

The three things that would:

1. **A spread narrower than the fee.** Every number above is dominated by the cost of
   crossing. On these books the median quoted spread is 2c and the temperature ladders
   run 7-20c. A maker route needs markets where the spread earned exceeds the spread
   paid to get out, and this session had none.
2. **More sessions.** The inventory-carrying maker branch is centred negative with
   intervals straddling zero on one date. Twenty independent dates would settle its
   sign. That is a measurement this system can now run unattended, and it is the only
   open question of the three.
3. **An atomic multi-leg primitive.** Not available on this venue, and its absence is
   documented rather than assumed.

## Reproduction

```bash
pmvl record forward --events-from artifacts/forward/selection-2026-09-18.json \
  --interval 10 --duration 5400 --watch 100 --out artifacts/forward/run4
pmvl replay dislocation artifacts/forward/run4 --structure implication
pmvl replay dislocation artifacts/forward/run4 --structure complete-set
pmvl replay maker-sweep artifacts/forward/run4
```

The raw L2 and tape are untracked by design (`.gitignore`), as the research archive is.
Every derived file is committed, including `observation_ledger_*.csv.gz` (one row per
observation, 32,403 of them, all negative) and `maker_quote_ledger_<policy>.csv` (one
row per quote, 2,309 per policy, of which 97% never filled). The losers are kept
alongside the winners and the unfilled quotes alongside the fills, because the fill
rate has to be recoverable from the artifact - it is the number a reader most needs to
check, and a ledger of only the fills would destroy its denominator. The observation
ledgers are gzipped; nothing is summarised away.
