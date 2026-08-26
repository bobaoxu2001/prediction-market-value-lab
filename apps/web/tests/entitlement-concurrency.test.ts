/**
 * What happens when two writers touch one user's billing metadata at once.
 *
 * Stripe delivers at-least-once and out of order, and a customer who clicks
 * "subscribe" produces a checkout write and a burst of webhooks within the same
 * second. The entitlement cache lives in Clerk private metadata, which offers no
 * compare-and-set, so "read, check, write" is three separate steps with two gaps
 * a concurrent writer can slip into.
 *
 * These tests drive those gaps deliberately. The store below lets a test hold a
 * write open, run the other writer to completion in the meantime, and then let
 * the first one land - which is the interleaving that actually corrupted state
 * and which no amount of ordinary test concurrency reproduces reliably.
 *
 * Two properties are asserted:
 *
 *   1. the unguarded customer-ID write can no longer revert entitlement state,
 *      because the two writers no longer share a metadata key;
 *   2. a lost update is *detected*. It is not prevented - Clerk cannot do that -
 *      so the contract is that the caller learns about it and Stripe redelivers,
 *      never that the caller is told everything is fine.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

type Metadata = Record<string, unknown>;

const store = new Map<string, Metadata>();

/** Resolvers for writes a test is deliberately holding open. */
let pendingGate: (() => void) | null = null;
/** How many more writes should be intercepted and held. */
let gateWrites = 0;

vi.mock("@/lib/auth-server", () => ({
  isAuthConfigured: () => true,
  getCurrentUser: vi.fn(),
  getCurrentUserId: vi.fn(),
  getPrivateMetadata: vi.fn(),
  mergePrivateMetadata: vi.fn(),
  userExists: vi.fn(async () => true),
}));

async function readMetadata(userId: string): Promise<Metadata> {
  return { ...(store.get(userId) ?? {}) };
}

async function writeMetadata(userId: string, patch: Metadata): Promise<boolean> {
  if (gateWrites > 0) {
    gateWrites -= 1;
    // Park the write. The test resumes it once the competing writer has run,
    // which is how the "both read the same before-state" window is forced.
    await new Promise<void>((resolve) => {
      pendingGate = resolve;
    });
  }
  // Top-level shallow merge, exactly as Clerk's updateUserMetadata behaves.
  // Getting this wrong would make the key-splitting fix look like it works
  // when it does not.
  store.set(userId, { ...(store.get(userId) ?? {}), ...patch });
  return true;
}

const {
  applyEntitlement,
  BILLING_CUSTOMER_METADATA_KEY,
  BILLING_METADATA_KEY,
  getBillingCache,
  rememberStripeCustomerId,
} = await import("@/lib/billing/entitlement");

const USER = "user_race";

/** Let a parked write proceed, and wait for the loop to pick it up. */
async function release(): Promise<void> {
  const resume = pendingGate;
  pendingGate = null;
  resume?.();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

/** Spin until a write has actually been parked, so a test cannot race itself. */
async function waitForParkedWrite(): Promise<void> {
  for (let i = 0; i < 200 && pendingGate === null; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  if (pendingGate === null) throw new Error("no write was parked");
}

const auth = await import("@/lib/auth-server");

beforeEach(() => {
  store.clear();
  pendingGate = null;
  gateWrites = 0;
  // Reinstated here, not in the mock factory: the suite runs with
  // `restoreMocks`, which wipes a `vi.fn()`'s implementation between tests. A
  // factory-supplied default therefore survives exactly one test, and every
  // later write silently returns undefined - which reads as "the write failed".
  vi.mocked(auth.getPrivateMetadata).mockImplementation(readMetadata);
  vi.mocked(auth.mergePrivateMetadata).mockImplementation(writeMetadata);
});

describe("the checkout write cannot revert entitlement state", () => {
  it("writes the customer ID to its own metadata key", async () => {
    await rememberStripeCustomerId(USER, "cus_abc");
    const metadata = store.get(USER) ?? {};
    expect(metadata[BILLING_CUSTOMER_METADATA_KEY]).toEqual({
      stripeCustomerId: "cus_abc",
    });
    // The key the ordering guards own must not have been touched at all.
    expect(metadata[BILLING_METADATA_KEY]).toBeUndefined();
  });

  it("does not resurrect a cancelled subscription", async () => {
    // The exact sequence that used to corrupt state:
    //
    //   1. checkout reads metadata (subscription still active)
    //   2. a cancellation webhook lands and writes `canceled`
    //   3. checkout writes back what it read, plus a customer ID
    //
    // Step 3 used to rewrite the whole billing object from the step-1 read, so
    // the account went back to `active` with no event and no trace.
    await applyEntitlement(
      USER,
      { subscriptionStatus: "active", stripeSubscriptionId: "sub_1" },
      { id: "evt_1", created: 1000 },
    );

    gateWrites = 1;
    const checkout = rememberStripeCustomerId(USER, "cus_abc");
    await waitForParkedWrite();

    await applyEntitlement(
      USER,
      { subscriptionStatus: "canceled", stripeSubscriptionId: "sub_1" },
      { id: "evt_2", created: 2000 },
    );
    expect((await getBillingCache(USER)).subscriptionStatus).toBe("canceled");

    await release();
    await checkout;

    expect((await getBillingCache(USER)).subscriptionStatus).toBe("canceled");
    expect((await getBillingCache(USER)).stripeCustomerId).toBe("cus_abc");
  });
});

describe("two webhooks for the same user", () => {
  it("ends on the newer event when the older one writes last", async () => {
    // Both read the same before-state and both pass their guards; the older
    // event then wins the write. The newer writer's read-back is what notices,
    // and its retry is what puts the correct state back.
    await applyEntitlement(
      USER,
      { subscriptionStatus: "trialing" },
      { id: "evt_0", created: 500 },
    );

    gateWrites = 1;
    const newer = applyEntitlement(
      USER,
      { subscriptionStatus: "canceled" },
      { id: "evt_new", created: 3000 },
    );
    await waitForParkedWrite();

    const older = await applyEntitlement(
      USER,
      { subscriptionStatus: "active" },
      { id: "evt_old", created: 1000 },
    );
    expect(older).toBe("applied");
    expect((await getBillingCache(USER)).subscriptionStatus).toBe("active");

    await release();
    expect(await newer).toBe("applied");
    expect((await getBillingCache(USER)).subscriptionStatus).toBe("canceled");
    expect((await getBillingCache(USER)).lastStripeEventId).toBe("evt_new");
  });

  it("reports conflict rather than success when its write keeps being lost", async () => {
    // The residual race Clerk cannot close. What matters is that it surfaces:
    // reporting "applied" here would leave a cache that disagrees with Stripe
    // and nothing that would ever notice.
    await applyEntitlement(
      USER,
      { subscriptionStatus: "trialing" },
      { id: "evt_0", created: 500 },
    );

    vi.mocked(auth.mergePrivateMetadata).mockImplementation(async (userId: string) => {
      // Every write is immediately overwritten by an older competing event.
      store.set(userId, {
        ...(store.get(userId) ?? {}),
        [BILLING_METADATA_KEY]: {
          subscriptionStatus: "active",
          lastStripeEventId: "evt_old",
          lastStripeEventCreated: 600,
        },
      });
      return true;
    });

    const outcome = await applyEntitlement(
      USER,
      { subscriptionStatus: "canceled" },
      { id: "evt_new", created: 3000 },
    );
    expect(outcome).toBe("conflict");
  });

  it("reports failed when the write does not land at all", async () => {
    vi.mocked(auth.mergePrivateMetadata).mockImplementation(async () => false);
    const outcome = await applyEntitlement(
      USER,
      { subscriptionStatus: "active" },
      { id: "evt_1", created: 1000 },
    );
    expect(outcome).toBe("failed");
  });
});

describe("the guards still behave on the ordinary paths", () => {
  it("treats a genuine Stripe redelivery as a duplicate, not a conflict", async () => {
    const event = { id: "evt_1", created: 1000 };
    expect(
      await applyEntitlement(USER, { subscriptionStatus: "active" }, event),
    ).toBe("applied");
    expect(
      await applyEntitlement(USER, { subscriptionStatus: "active" }, event),
    ).toBe("duplicate");
  });

  it("drops a late event that predates the recorded one", async () => {
    await applyEntitlement(
      USER,
      { subscriptionStatus: "canceled" },
      { id: "evt_2", created: 2000 },
    );
    expect(
      await applyEntitlement(
        USER,
        { subscriptionStatus: "active" },
        { id: "evt_1", created: 1000 },
      ),
    ).toBe("stale");
    expect((await getBillingCache(USER)).subscriptionStatus).toBe("canceled");
  });

  it("reads a customer ID written before the key split", async () => {
    // Accounts that paid under the old layout must keep their portal handle.
    store.set(USER, {
      [BILLING_METADATA_KEY]: {
        stripeCustomerId: "cus_legacy",
        subscriptionStatus: "active",
      },
    });
    expect((await getBillingCache(USER)).stripeCustomerId).toBe("cus_legacy");
  });

  it("does not leave two disagreeing copies of the customer ID", async () => {
    await applyEntitlement(
      USER,
      { subscriptionStatus: "active", stripeCustomerId: "cus_abc" },
      { id: "evt_1", created: 1000 },
    );
    const metadata = store.get(USER) ?? {};
    const billing = metadata[BILLING_METADATA_KEY] as Record<string, unknown>;
    expect(billing.stripeCustomerId).toBeUndefined();
    expect(metadata[BILLING_CUSTOMER_METADATA_KEY]).toEqual({
      stripeCustomerId: "cus_abc",
    });
    expect((await getBillingCache(USER)).stripeCustomerId).toBe("cus_abc");
  });
});
