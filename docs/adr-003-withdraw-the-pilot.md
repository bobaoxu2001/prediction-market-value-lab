# ADR 003 — Withdraw the paid pilot; make cost the product and data the paid layer

**Status:** accepted.
**Date:** 7 August 2026.
**Supersedes:** the commercial half of [ADR 002](adr-002-saas-foundation.md). The
accounts, entitlement and Stripe decisions in ADR 002 stand unchanged; only what is
being sold changes here.
**Related:** `docs/founding-pilot-fulfilment.md`, `docs/legal-placeholders.md`.

## Context

ADR 002 closed with a sentence that turned out to be the whole problem:

> The pricing page currently sells early access to unfinished work. That is honest
> but it is not a business.

The Founding Research Pilot was the attempt to make it one: USD 49, one time, 30
days of a hand-reviewed daily digest, capped at five members. Zero were sold — the
member ledger in `private/` is still the empty template.

Three things are now known that were not known when the pilot was designed.

**1. The model does not beat the market.** `pmvl retrodict` replays the models
against already-settled markets and scores them against the venue's own price at
the same instant. On 92 crypto forecasts across four lead times:

| | Brier |
|---|---|
| Independent estimate | 0.07415 |
| The market's own price | 0.07167 |
| **Improvement** | **−0.0025** |

Close to a tie, slightly worse. The sample is narrow — two days of settlements, one
category, mostly short-dated — and it is the only real evidence there is. The
pilot's entire premise is that the digest contains information the price does not.
That premise is currently unsupported by the only measurement that bears on it.

**2. There is no track record and will not be for weeks.** `pmvl readiness` reports
0 settled live recommendations against the 60 needed to quote a Brier-versus-market
figure and the 150 needed to fit a calibration map. Everything downstream of the
snapshot has only ever run on synthetic history.

**3. The category with the audience is the category with no opinion.** The sports
and politics boards carry the retail volume on both venues. The sports base-rate
model now covers head-to-head games, is off by default pending measurement, and
prices none of the golf, motorsport, futures and prop contracts that make up most
of that board.

## Decision 1 — Withdraw the pilot rather than reprice it

The pilot is closed. `/founding-pilot` stays reachable and says so; the payment link
is disabled in code rather than merely unset.

Reasons:

- The most likely subscriber experience was thirty emails reporting zero actionable
  candidates and a cost table. That is the product behaving correctly and it is a
  refund conversation, not a business.
- A one-time 30-day purchase produces no renewal signal, so five sales would have
  measured curiosity, not retained value — and $245 is not worth the thirty days of
  manual editorial review the runbook correctly demands.
- Withdrawing costs nothing. Nobody has paid, so there is no member to migrate, no
  refund to process and no commitment to unwind.

**Rejected:** repricing to a monthly subscription. It fixes the renewal-signal
problem and leaves the real one untouched — it would still be selling a forecast
advantage that the only available measurement says is not there.

## Decision 2 — The execution-cost engine is the product, and it is free

`/cost` becomes the entry point rather than the fallback the reader lands on when
the ranked list is empty.

The argument for it is that it is the only surface here that does not depend on a
forecast being right:

- A binary contract pays exactly $1, so an execution-cost estimate *is* a
  break-even probability. It is arithmetic over observed depth and published fee
  schedules, not a prediction.
- The effect is large and specific. Kalshi ceils its fee to the whole cent on the
  whole order, so a 1¢ contract bought one at a time costs 2¢ — a 100% premium that
  falls to 7% at a hundred contracts. Neither venue displays this.
- It has an answer for every market with a quote, including every category the
  models decline. The independence rule cannot empty it.

It stays free permanently, and that is a product decision rather than a pricing
oversight: a tool whose value is "here is what this actually costs you" cannot be
the thing behind the paywall without undermining the reason to trust it.

## Decision 3 — The paid layer is data, not opinion

If a paid tier is ever opened it sells **normalised cross-venue market data with
cost-adjusted execution prices**, over an API. Not signals, not a digest, not a
ranked list.

Reasons:

- It is the part of this system that is verifiable by the buyer on the day they buy
  it. Every figure can be checked against the venues' own endpoints; nothing about
  it requires trusting a forecast that has not been validated.
- The work is already done and tested — normalisation, the executable ask ladder,
  the fee and rounding rules, the Decimal money core — and none of it depends on
  the independence gate producing a recommendation.
- The buyers are people who want inputs rather than conclusions: quantitative
  hobbyists, small trading teams, researchers, journalists. That audience does not
  need the model to beat the market, which is the one thing that cannot currently
  be claimed.

The realistic ceiling is small. Prediction-market data is a narrow niche and this
would be a modest business, not a large one. Writing that down here is deliberate,
so that a later decision to invest heavily has to argue against it.

## Decision 4 — Nothing is sold until the measurement supports it

Two conditions before any paid tier opens, both machine-checkable:

1. `pmvl readiness` reports the `brier_vs_market` milestone met — at least 60
   settled live recommendations from a pipeline that is not stalled.
2. The Brier-versus-market figure on that live sample is published on
   `/track-record`, whatever its sign.

`BILLING_MODE` remains `disabled` | `test` with no `live` value, exactly as ADR 002
set it. This ADR does not relax that gate; it adds an evidential one in front of it.

If the figure comes out negative, the honest response is the one the pricing page
already commits to: say so, narrow the model's scope, and sell the data layer, which
never depended on the forecast being good.

## Consequences

- The site has no purchasable product. That is the accurate state of the business
  and the page now says it plainly rather than implying a queue to join.
- `docs/founding-pilot-fulfilment.md` describes a suspended process. It is kept
  rather than deleted: the manual editorial discipline in it is the right shape for
  any future paid delivery, and rewriting it from memory later would lose that.
- The unresolved placeholders in `docs/legal-placeholders.md` — legal entity,
  jurisdiction, refund policy, retention — stop being urgent, because nothing is
  being sold. They become a precondition of Decision 4 rather than an open risk.
- The retrodiction result is now load-bearing for a commercial decision, so its
  caveats matter: it rests on defences rather than on structure, its market prices
  come from candles rather than executable quotes, and its sample is narrow. It is
  evidence for *not selling yet*, which is the direction where a weak measurement is
  safe to act on.
