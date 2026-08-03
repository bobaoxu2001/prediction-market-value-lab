# PMVL Pilot — HISTORICAL SAMPLE, Snapshot 2026-07-31

> ⚠️ **Historical sample — generated from the validated Snapshot dated 2026-07-31. Not current market research.**
>
> Do not act on any price, probability or edge in this document. Every figure was true at the Snapshot's cutoff and is now historical. No current research is being issued here.

**No recommendations were published this week — 410 scored markets settled and were graded anyway**

Window: 2026-07-24 to 2026-07-31 (UTC).

## Recommendation scorecard

- Recommendations published in the window: **0**
- Resolved in the window: **0**

## Forecast accuracy on everything that settled

410 scored markets resolved inside the window. These are graded whether or not they were ever recommended, which is what keeps this section meaningful in a week with no recommendations.

Brier score: mean squared error of the probability forecast. **Lower is better**, 0 is perfect, 0.25 is a coin flip.

| Set | n | Model Brier | Market Brier | Model advantage |
| --- | ---: | ---: | ---: | ---: |
| Estimates with an independent prior | 216 | 0.03587 | 0.04349 | +0.00763 |
| Estimates derived from the market price | 194 | 0.04572 | 0.04572 | +0.00000 |

- *Estimates with an independent prior*: The only set where beating the market means anything: these estimates never saw the target market's own price.
- *Estimates derived from the market price*: Scored for completeness only. An estimate built from the market price cannot meaningfully beat that price, and a good score here is not evidence of skill.

## What this week's numbers cannot tell you

- No recommendations were published in this window, so there is no recommendation scorecard to report. That is the expected outcome on a week when nothing cleared the admission gate — it is not a data problem.
- Forecast accuracy is not profitability, and profitability is not skill. A well-calibrated forecast can still lose money after costs, and a lucky week can flatter a poor model.

## Snapshot provenance

- Snapshot: `a3487a6fa577-2026-07-31T08:56:07.827120`
- Pipeline commit: `a3487a6fa577`
- Model version: `ensemble-v1.0.0`
- Data cutoff: 2026-07-31 08:56 UTC
- Freshest observed quote: 2026-07-31 08:56 UTC
- Report generated for: 2026-07-31 08:56 UTC

| Input | State | Age |
| --- | --- | --- |
| top of book | fresh | 0.0 h |
| full orderbook | fresh | 0.0 h |
| model prediction | fresh | 0.0 h |

---

### Terms of this report

- Research and information only. This is not investment, legal, tax or financial advice, not a solicitation, and not a recommendation to trade.
- Nothing here is personalised. It is the same report sent to every pilot member, written without knowledge of your circumstances, capital or risk tolerance.
- No return is promised or implied. Prediction-market contracts can settle worthless and the entire amount paid for one can be lost.
- Every figure comes from a frozen Snapshot and does not update. Prices move; a quote shown here may no longer exist. Verify on the venue before acting.
- PMVL places no orders, holds no funds and has no execution access to any venue. Any position you take is your own decision.
