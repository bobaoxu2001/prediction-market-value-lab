import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The entitlement cache, and the two guards that make an at-least-once,
 * unordered webhook stream safe to apply.
 */

const metadataStore = new Map<string, Record<string, unknown>>();

vi.mock("@/lib/auth-server", () => ({
  isAuthConfigured: () => true,
  getCurrentUser: vi.fn(),
  getCurrentUserId: vi.fn(),
  getPrivateMetadata: vi.fn(async (userId: string) => metadataStore.get(userId) ?? {}),
  mergePrivateMetadata: vi.fn(async (userId: string, patch: Record<string, unknown>) => {
    metadataStore.set(userId, { ...(metadataStore.get(userId) ?? {}), ...patch });
    return true;
  }),
  userExists: vi.fn(async () => true),
}));

const {
  applyEntitlement,
  BILLING_METADATA_KEY,
  cacheFromSubscription,
  getBillingCache,
  getCurrentEntitlement,
  stateFromStripeStatus,
  stateGrantsPro,
} = await import("@/lib/billing/entitlement");

const USER = "user_test_1";

function subscription(overrides: Record<string, unknown> = {}) {
  return {
    id: "sub_123",
    status: "active",
    cancel_at_period_end: false,
    customer: "cus_123",
    items: {
      data: [{ current_period_end: 1_800_000_000, price: { id: "price_monthly_test" } }],
    },
    ...overrides,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

beforeEach(() => {
  metadataStore.clear();
});

describe("Stripe status to entitlement state", () => {
  it.each([
    ["trialing", false, "pro_trialing"],
    ["active", false, "pro_active"],
    ["active", true, "pro_canceling"],
    ["past_due", false, "pro_past_due"],
    ["unpaid", false, "pro_past_due"],
    ["canceled", false, "pro_canceled"],
    ["paused", false, "pro_canceled"],
    ["incomplete", false, "free"],
    ["incomplete_expired", false, "free"],
    [undefined, false, "free"],
  ])("maps %s (cancelAtPeriodEnd=%s) to %s", (status, cancelling, expected) => {
    expect(stateFromStripeStatus(status as string | undefined, cancelling)).toBe(expected);
  });

  it("maps a status it has never seen to unknown, which does not grant access", () => {
    // A status added upstream must not silently become an entitlement.
    expect(stateFromStripeStatus("some_future_status", false)).toBe("unknown");
    expect(stateGrantsPro("unknown")).toBe(false);
  });

  it("grants Pro for exactly three states", () => {
    expect(stateGrantsPro("pro_active")).toBe(true);
    expect(stateGrantsPro("pro_trialing")).toBe(true);
    expect(stateGrantsPro("pro_canceling")).toBe(true);
    // Fail closed: a failed payment suspends access rather than continuing it.
    expect(stateGrantsPro("pro_past_due")).toBe(false);
    expect(stateGrantsPro("pro_canceled")).toBe(false);
    expect(stateGrantsPro("free")).toBe(false);
  });
});

describe("cacheFromSubscription", () => {
  it("reads the period end off the subscription items, not the subscription", () => {
    // `current_period_end` moved onto items; reading the old top-level field
    // would silently store `undefined` on every subscription.
    const cache = cacheFromSubscription(subscription());
    expect(cache.currentPeriodEnd).toBe(1_800_000_000);
    expect(cache.priceId).toBe("price_monthly_test");
    expect(cache.stripeCustomerId).toBe("cus_123");
    expect(cache.subscriptionStatus).toBe("active");
  });

  it("takes the latest period across multiple items", () => {
    const cache = cacheFromSubscription(
      subscription({
        items: {
          data: [
            { current_period_end: 100, price: { id: "price_monthly_test" } },
            { current_period_end: 900, price: { id: "price_addon" } },
          ],
        },
      }),
    );
    expect(cache.currentPeriodEnd).toBe(900);
  });
});

describe("applyEntitlement", () => {
  it("writes the state and records the event that justified it", async () => {
    const outcome = await applyEntitlement(
      USER,
      cacheFromSubscription(subscription()),
      { id: "evt_1", created: 1000 },
    );
    expect(outcome).toBe("applied");

    const cache = await getBillingCache(USER);
    expect(cache.subscriptionStatus).toBe("active");
    expect(cache.lastStripeEventId).toBe("evt_1");
    expect(cache.lastStripeEventCreated).toBe(1000);
  });

  it("is idempotent: a redelivered event is a no-op", async () => {
    // Stripe retries on any non-2xx, so duplicates are ordinary traffic. A
    // captured request replayed by an attacker takes the same path.
    await applyEntitlement(USER, cacheFromSubscription(subscription()), {
      id: "evt_1",
      created: 1000,
    });
    const second = await applyEntitlement(
      USER,
      cacheFromSubscription(subscription({ status: "canceled" })),
      { id: "evt_1", created: 1000 },
    );
    expect(second).toBe("duplicate");
    expect((await getBillingCache(USER)).subscriptionStatus).toBe("active");
  });

  it("drops an event older than the last one applied", async () => {
    // The failure this prevents: a delayed `customer.subscription.updated` from
    // before a cancellation, resurrecting a cancelled subscription.
    await applyEntitlement(
      USER,
      cacheFromSubscription(subscription({ status: "canceled" })),
      { id: "evt_cancel", created: 2000 },
    );
    const stale = await applyEntitlement(
      USER,
      cacheFromSubscription(subscription({ status: "active" })),
      { id: "evt_older", created: 1500 },
    );
    expect(stale).toBe("stale");
    expect((await getBillingCache(USER)).subscriptionStatus).toBe("canceled");
  });

  it("accepts a newer event and moves the state forward", async () => {
    await applyEntitlement(USER, cacheFromSubscription(subscription()), {
      id: "evt_1",
      created: 1000,
    });
    const outcome = await applyEntitlement(
      USER,
      cacheFromSubscription(subscription({ status: "past_due" })),
      { id: "evt_2", created: 2000 },
    );
    expect(outcome).toBe("applied");
    expect((await getBillingCache(USER)).subscriptionStatus).toBe("past_due");
  });

  it("preserves the customer ID when an update does not restate it", async () => {
    // The portal needs this handle; a partial update must not lose it.
    await applyEntitlement(USER, { stripeCustomerId: "cus_123" }, {
      id: "evt_0",
      created: 500,
    });
    await applyEntitlement(USER, { subscriptionStatus: "active" }, {
      id: "evt_1",
      created: 1000,
    });
    expect((await getBillingCache(USER)).stripeCustomerId).toBe("cus_123");
  });
});

describe("lifecycle transitions the account page reports", () => {
  async function apply(status: string, cancelAtPeriodEnd: boolean, created: number) {
    await applyEntitlement(
      USER,
      cacheFromSubscription(
        subscription({ status, cancel_at_period_end: cancelAtPeriodEnd }),
      ),
      { id: `evt_${created}`, created },
    );
    process.env.BILLING_MODE = "test";
    process.env.STRIPE_SECRET_KEY = "sk_test_x";
    process.env.STRIPE_WEBHOOK_SECRET = "whsec_x";
    process.env.STRIPE_PRO_MONTHLY_PRICE_ID = "price_monthly_test";
    process.env.STRIPE_PRO_ANNUAL_PRICE_ID = "price_annual_test";
  }

  it("a scheduled cancellation keeps access and says so", async () => {
    await apply("active", true, 1000);
    const cache = await getBillingCache(USER);
    const state = stateFromStripeStatus(
      cache.subscriptionStatus,
      cache.cancelAtPeriodEnd === true,
    );
    expect(state).toBe("pro_canceling");
    expect(stateGrantsPro(state)).toBe(true);
  });

  it("a completed cancellation removes access", async () => {
    await apply("canceled", false, 2000);
    const cache = await getBillingCache(USER);
    expect(stateGrantsPro(stateFromStripeStatus(cache.subscriptionStatus, false))).toBe(
      false,
    );
  });

  it("a failed payment produces past-due, not continued access", async () => {
    await apply("past_due", false, 3000);
    const cache = await getBillingCache(USER);
    const state = stateFromStripeStatus(cache.subscriptionStatus, false);
    expect(state).toBe("pro_past_due");
    expect(stateGrantsPro(state)).toBe(false);
  });
});

describe("getCurrentEntitlement", () => {
  it("reports free and hides billing references when billing is disabled", async () => {
    // Fail closed: with billing off, a cached Pro state could neither have been
    // granted by this deployment nor be revoked by it.
    metadataStore.set(USER, {
      [BILLING_METADATA_KEY]: {
        subscriptionStatus: "active",
        stripeCustomerId: "cus_123",
      },
    });
    const authServer = await import("@/lib/auth-server");
    vi.mocked(authServer.getCurrentUser).mockResolvedValue({
      id: USER,
      email: "a@example.com",
      name: null,
      createdAt: 1,
    });

    const entitlement = await getCurrentEntitlement();
    expect(entitlement.signedIn).toBe(true);
    expect(entitlement.billingDisabled).toBe(true);
    expect(entitlement.state).toBe("free");
    expect(entitlement.isPro).toBe(false);
    expect(entitlement.stripeCustomerId).toBeNull();
  });

  it("is anonymous and never Pro for a signed-out visitor", async () => {
    const authServer = await import("@/lib/auth-server");
    vi.mocked(authServer.getCurrentUser).mockResolvedValue(null);
    const entitlement = await getCurrentEntitlement();
    expect(entitlement.signedIn).toBe(false);
    expect(entitlement.isPro).toBe(false);
    expect(entitlement.state).toBe("free");
  });
});
