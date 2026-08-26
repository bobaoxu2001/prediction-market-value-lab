# Stripe entitlement concurrency: what is fixed, and what is not

Billing is **disabled** (`BILLING_MODE=disabled`) and has no live mode; a
`sk_live_` key is rejected rather than honoured. This document is about the code
being correct before it is ever switched on, not about an incident.

Implementation: [`apps/web/lib/billing/entitlement.ts`](../apps/web/lib/billing/entitlement.ts).
Tests: [`apps/web/tests/entitlement-concurrency.test.ts`](../apps/web/tests/entitlement-concurrency.test.ts).

---

## The store, and what it cannot do

Entitlement state is a **cache of what Stripe last confirmed**, kept in Clerk
private metadata. Stripe is the source of truth; the cache exists so a page
render is not a Stripe API call.

Clerk's Backend API offers exactly one write primitive for it:

```ts
await client.users.updateUserMetadata(userId, { privateMetadata: patch });
```

It takes a patch and nothing else. In `@clerk/backend` 3.15.0 there is **no
ETag, no `If-Match`, no version token, and no conditional update**. Every write
is unconditional and last-writer-wins.

That single fact determines everything below. A database would let the store
decide the race in one round trip:

```sql
UPDATE billing SET ... WHERE user_id = ? AND last_event_created < ?
```

There is no equivalent here.

---

## The two hazards

Stripe delivers **at-least-once** and **out of order**. A customer clicking
"subscribe" produces a checkout write and a burst of webhooks inside one second.

### Hazard A — an unguarded writer sharing a guarded key *(fixed)*

Two different code paths wrote the same `billing` metadata key:

- the **webhook**, through `applyEntitlement`, guarded on event ID (idempotency)
  and `event.created` (ordering);
- the **checkout route**, through `rememberStripeCustomerId`, guarded on nothing.

The second was a read-modify-write of the *whole* object: read the cache, splice
in a customer ID, write it back. So:

1. checkout reads metadata — subscription still `active`
2. a cancellation webhook lands and writes `canceled`
3. checkout writes back what it read in step 1, plus the customer ID

The account is `active` again. No event caused it, nothing logged it, and the
ordering guard was bypassed entirely because the writer that clobbered it never
consulted one.

**The fix is structural, not hopeful.** The customer ID moved to its own
top-level metadata key, `billingCustomer`. Clerk shallow-merges at the top level,
so two top-level keys are genuinely independent: a write to one cannot revert the
other. `billing` is now written only by the ordering-guarded path.

The remaining overlap — `applyEntitlement` also sets `billingCustomer` when an
event restates the customer ID — is harmless: a Clerk user maps to one Stripe
customer, so both writers write the same constant. Both writes go out in a single
`updateUserMetadata` call, so the ID cannot be lost by a concurrent write to the
cache.

Accounts written before the split keep their ID inside `billing`;
`readCustomerId` falls back to it. Dropping that fallback would take the Customer
Portal away from exactly the users who already paid.

### Hazard B — two webhooks racing *(detected, not prevented)*

Both invocations read the same "before" state, both pass their guards, and the
**later write wins regardless of which event was newer**.

This one cannot be closed with the primitive Clerk provides. What is done
instead:

1. **Read back after writing.** If a newer event won, that is the right answer
   and `applyEntitlement` reports success. If an *older* one won, the write was
   lost, and it re-applies — up to `LOST_UPDATE_ATTEMPTS` (3).
2. **Exhausting the attempts returns `conflict`, not `applied`.** The webhook
   turns `conflict` (and `failed`) into a **503**, so Stripe redelivers. The
   redelivery re-reads the subscription from Stripe rather than trusting the
   payload, so the system converges on the truth through a retry path that
   already exists.

`duplicate` and `stale` deliberately stay 200. Both mean the stored state is
already right, and asking Stripe to redeliver either would be a retry loop that
never terminates.

**The honest guarantee: writes are eventually correct via Stripe redelivery, not
atomic.** A residual window remains — if a stale writer's write lands after the
newer writer has finished all its attempts, the cache holds the stale value and
the stale writer reported success, because it never saw the state it clobbered.

---

## Why no lock was invented

The tempting fix is a `lock` key written before the real write. It would be two
racy unconditional writes wearing a mutex's clothes: acquiring the "lock" is
itself last-writer-wins, so two holders is not an edge case, it is the normal
outcome under contention. What it *would* reliably do is make the code read as
though the race were handled, which converts a visible limitation into a silent
one.

The failure it would hide is entitlement corruption — a customer charged and not
entitled, or entitled and not charged. That is the wrong thing to be quietly
wrong about, so the blocker is stated instead.

---

## What would actually fix it

Any of these, none of which exist today:

1. **A row in a real database.** Postgres is already a supported backend for the
   research stack (`DATABASE_URL`), but the web app has no database connection
   and no migration path of its own. `UPDATE ... WHERE last_event_created < ?`
   resolves the race in the store, in one round trip.
2. **A conditional-write KV** (Vercel KV / Upstash `SET ... IF`, DynamoDB
   conditional expressions). Smaller than (1); still new infrastructure, new
   credentials and a new failure domain.
3. **Serialising per user** through a queue keyed on the Clerk user ID, so
   concurrent events for one account cannot interleave.
4. **A Clerk conditional-update primitive**, if one ever ships.

Until one of those exists, Stripe remains authoritative and reconciliation
against it remains available — which is what makes the current behaviour
defensible rather than merely tolerable.

---

## Outcomes

| Outcome | Meaning | HTTP | Stripe redelivers |
|---|---|---:|---|
| `applied` | The event's effect is on record. | 200 | no |
| `duplicate` | Already applied; a redelivery. | 200 | no |
| `stale` | Older than the recorded event; correctly dropped. | 200 | no |
| `failed` | The write did not land. | 503 | yes |
| `conflict` | The write landed and an older event overwrote it. | 503 | yes |

Before this change, `failed` also returned 200 — a metadata write that never
landed was acknowledged as handled, and nothing would ever have corrected it.
