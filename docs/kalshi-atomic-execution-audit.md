# Does Kalshi offer any way to put on a multi-leg position at once?

**Answer: `NO_ATOMIC_ROUTE_FOUND`.**

This matters more than any parameter in the strategy. The previous phase's three
positive structures all died on the same fact - every one of them was
`THEORETICAL_ONLY`, positive when both legs are priced at the same instant and
negative once the legs are filled in sequence, with KXSOLD going from +$33.45 atomic
to −$52.80 sequential to −$387.82 if the second leg vanishes. An 11:1 adverse ratio
needs about a 92% fill probability just to break even on attempts. If the venue had a
native way to send both legs as one indivisible instruction, that entire loss term
would disappear and the strategy would be alive again. So it was worth checking
properly rather than assuming.

Every claim below is from the live API or the published documentation, dated
2026-09-18. Nothing here required credentials and nothing was written to the venue:
the endpoint probes are GETs, which cannot create an order, and existence was inferred
from `401 token_authentication_failure` (the path is real, auth is missing) versus
`404 not found` (no such path).

## What was checked

| Mechanism | Status | What it actually does |
|---|---|---|
| `POST /portfolio/orders` (create order) | exists | **Single leg only.** TIF is `fill_or_kill`, `immediate_or_cancel`, or `good_till_canceled`, plus `post_only` and `reduce_only`. No multi-leg, spread or contingent order type. |
| `POST /portfolio/orders/batched` | exists (401 on GET) | Submits several orders in one request. The response carries a **per-order `error` field**, so orders succeed and fail independently. A batch is a convenience, not a transaction. |
| Order groups | exists | A `contracts_limit` over a rolling 15-second window; breaching it **cancels every order in the group**. A risk limiter, not a linkage - it can stop a runaway leg, it cannot guarantee the second one. |
| FIX order entry | exists | `NewOrderSingle`, `OrderCancelReplace`, `OrderCancel`, `MassCancel`. Mass cancel emits an individual `ExecutionReport` per order. No multi-leg, no spread instrument, no simultaneous execution. |
| Multivariate event collections (MVE) | exists, public | `POST /multivariate_event_collections/{ticker}` mints **a new market** whose rule is "resolves YES only if every selected leg resolves YES". Capped at 5,000 creations per week; `size_min` 2. |
| Same-market netting | exists | Buying YES and NO in the same market **always nets**. |
| Collateral return | exists, opt-in | Real collateral relief for buying the NO side across several markets of a mutually exclusive group. Disabled by default; the flag locks at the first order in an event. |
| RFQ / communications API | exists | A quoting workflow, not an execution primitive. |

## The three things people mistake for an atomic route

**A batch is not a transaction.** `BatchCreateOrders` puts N orders in one HTTP
request, which removes N−1 round trips of latency - genuinely useful - but the
response reports each order's outcome separately, so the state where leg one filled
and leg two was rejected is a state the endpoint is *designed to return*. Latency is
not the risk being priced. The risk is that the second book moves between the two
matches, and a batch does not make the two matches simultaneous.

**A bundle is not a conversion.** The MVE collection is the one place where several
outcomes really do trade as a single instrument, and it is tempting to read that as
"Kalshi supports combinations". It does, in one direction only: you can *create* a
parlay market and buy it with one order. There is no mechanism to deliver existing
leg positions into the bundle, and none to redeem the bundle back into legs. That is
the difference between an options exchange's conversion - where a position in the
parts and a position in the whole are fungible, which is what collapses the arbitrage
- and a product that merely exists alongside the parts.

The relationship that *does* hold is an inequality. A parlay resolves YES only if
every leg does, so `YES(parlay)` implies `YES(leg_i)` for every leg, and therefore
`YES(leg_i) + NO(parlay)` covers every state and floors at $1 per set. That is a real
deterministic structure and it is two legs, not twenty. It is still two orders, so
everything in this document about sequencing still applies to it.

**Netting is not execution.** Same-market netting and collateral return both reduce
*capital*, not *risk in the gap between fills*. Collateral return in particular is a
genuine improvement to a basket structure's capital efficiency - the money comes back
immediately rather than at settlement - and it is worth switching on before any basket
is sized. But it pays back collateral on a position you already hold. It does nothing
about the leg you are still trying to get.

## What this closes off

The sequencing loss term is not a modelling assumption that a better implementation
could remove. It is a property of the venue:

- there is no order type that fills two markets together,
- the batch endpoint reports partial failure by design,
- FIX is single-order,
- and the one native combination product cannot absorb or emit leg positions.

So a taker route across two markets must price the failure branch, and on the observed
books that branch is roughly eleven times the size of the success branch. The only
mitigations the venue offers are second-order: `fill_or_kill` on the second leg turns a
bad fill into no fill (which converts a loss into the *rescue* loss, not into zero),
`post_only` avoids crossing, and an order group caps how far a runaway can run.

## What it leaves open

Nothing here rules out a **single-leg** strategy, because a single leg has no
sequencing risk at all. That is why the maker experiment is the next thing measured
rather than a better multi-leg executor: the question worth asking is no longer "can
the two legs be landed together" - they cannot - but "is there a one-leg position
whose unconditional expectancy is positive after adverse selection".

## Sources

- Kalshi API reference: [create order](https://docs.kalshi.com/api-reference/orders/create-order-v2),
  [batch create orders](https://docs.kalshi.com/api-reference/orders/batch-create-orders-v2),
  [create order group](https://docs.kalshi.com/api-reference/order-groups/create-order-group),
  [FIX order entry](https://docs.kalshi.com/fix/order-entry),
  [create market in a multivariate event collection](https://docs.kalshi.com/api-reference/multivariate/create-market-in-multivariate-event-collection)
- Kalshi help centre: [collateral return](https://help.kalshi.com/en/articles/13823816-collateral-return)
- Live probes of `api.elections.kalshi.com/trade-api/v2`, 2026-09-18 (GET only).
