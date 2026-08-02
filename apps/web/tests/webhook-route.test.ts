import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import Stripe from "stripe";

/**
 * The Stripe webhook.
 *
 * Signature verification is exercised with Stripe's own
 * `generateTestHeaderString` helper rather than a hand-rolled HMAC, so the test
 * fails if the SDK's scheme changes rather than agreeing with a stale copy of
 * it. No network call is involved: verification is pure crypto.
 */

const WEBHOOK_SECRET = "whsec_test_secret_for_signing_only";

// A real client, so `webhooks.constructEventAsync` is the genuine implementation.
const stripe = new Stripe("sk_test_dummy_key_for_tests", {
  apiVersion: "2026-07-29.dahlia",
});

const subscriptionsRetrieve = vi.fn();
const customersRetrieve = vi.fn();

vi.mock("@/lib/billing/stripe", () => ({
  getStripe: () => ({
    webhooks: stripe.webhooks,
    subscriptions: { retrieve: (...args: unknown[]) => subscriptionsRetrieve(...args) },
    customers: { retrieve: (...args: unknown[]) => customersRetrieve(...args) },
  }),
}));

const applyEntitlement = vi.fn(
  async (
    _userId: string,
    _next: Record<string, unknown>,
    _event: { id: string; created: number },
  ): Promise<string> => "applied",
);
const cacheFromSubscription = vi.fn((subscription: { status: string }) => ({
  subscriptionStatus: subscription.status,
}));

vi.mock("@/lib/billing/entitlement", () => ({
  applyEntitlement: (...args: Parameters<typeof applyEntitlement>) =>
    applyEntitlement(...args),
  cacheFromSubscription: (...args: Parameters<typeof cacheFromSubscription>) =>
    cacheFromSubscription(...args),
}));

const userExists = vi.fn(async (_userId: string): Promise<boolean> => true);
vi.mock("@/lib/auth-server", () => ({
  userExists: (...args: Parameters<typeof userExists>) => userExists(...args),
  isAuthConfigured: () => true,
}));

const { POST } = await import("@/app/api/stripe/webhook/route");

function enableBilling(): void {
  process.env.BILLING_MODE = "test";
  process.env.STRIPE_SECRET_KEY = "sk_test_dummy_key_for_tests";
  process.env.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET;
  process.env.STRIPE_PRO_MONTHLY_PRICE_ID = "price_monthly_test";
  process.env.STRIPE_PRO_ANNUAL_PRICE_ID = "price_annual_test";
}

function event(
  type: string,
  object: Record<string, unknown>,
  overrides: Record<string, unknown> = {},
) {
  return {
    id: "evt_test_1",
    object: "event",
    api_version: "2026-07-29.dahlia",
    created: 1_700_000_000,
    livemode: false,
    pending_webhooks: 0,
    request: { id: null, idempotency_key: null },
    type,
    data: { object },
    ...overrides,
  };
}

function signedRequest(
  payload: unknown,
  { secret = WEBHOOK_SECRET, signature }: { secret?: string; signature?: string } = {},
): NextRequest {
  const body = JSON.stringify(payload);
  const header =
    signature ??
    stripe.webhooks.generateTestHeaderString({ payload: body, secret });
  return new NextRequest("http://localhost:3000/api/stripe/webhook", {
    method: "POST",
    headers: { "content-type": "application/json", "stripe-signature": header },
    body,
  });
}

const SUBSCRIPTION = {
  id: "sub_123",
  status: "active",
  cancel_at_period_end: false,
  customer: "cus_123",
  metadata: { clerkUserId: "user_1" },
  items: { data: [{ current_period_end: 1_800_000_000, price: { id: "price_monthly_test" } }] },
};

beforeEach(() => {
  enableBilling();
  subscriptionsRetrieve.mockReset().mockResolvedValue(SUBSCRIPTION);
  customersRetrieve.mockReset();
  applyEntitlement.mockReset().mockResolvedValue("applied");
  userExists.mockReset().mockResolvedValue(true);
});

describe("signature verification", () => {
  it("rejects a body with no signature header", async () => {
    const request = new NextRequest("http://localhost:3000/api/stripe/webhook", {
      method: "POST",
      body: JSON.stringify(event("customer.subscription.updated", SUBSCRIPTION)),
    });
    const response = await POST(request);
    expect(response.status).toBe(400);
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("rejects a signature made with the wrong secret", async () => {
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION), {
        secret: "whsec_an_attackers_own_secret",
      }),
    );
    expect(response.status).toBe(400);
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("rejects a forged signature header", async () => {
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION), {
        signature: "t=1700000000,v1=deadbeef",
      }),
    );
    expect(response.status).toBe(400);
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("rejects a body altered after signing", async () => {
    // The reason the raw body is read before any parsing: the signature covers
    // exact bytes, so a tampered payload must not verify.
    const payload = JSON.stringify(event("customer.subscription.updated", SUBSCRIPTION));
    const header = stripe.webhooks.generateTestHeaderString({
      payload,
      secret: WEBHOOK_SECRET,
    });
    const tampered = payload.replace("sub_123", "sub_999");
    const request = new NextRequest("http://localhost:3000/api/stripe/webhook", {
      method: "POST",
      headers: { "content-type": "application/json", "stripe-signature": header },
      body: tampered,
    });
    expect((await POST(request)).status).toBe(400);
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("accepts a correctly signed event", async () => {
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION)),
    );
    expect(response.status).toBe(200);
    expect(applyEntitlement).toHaveBeenCalledOnce();
  });
});

describe("gating", () => {
  it("refuses events when billing is disabled, and asks Stripe to retry", async () => {
    delete process.env.BILLING_MODE;
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION)),
    );
    expect(response.status).toBe(503);
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("drops a livemode event", async () => {
    const response = await POST(
      signedRequest(
        event("customer.subscription.updated", SUBSCRIPTION, { livemode: true }),
      ),
    );
    expect(response.status).toBe(400);
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("acknowledges an unrelated event without touching entitlements", async () => {
    const response = await POST(
      signedRequest(event("payment_intent.succeeded", { id: "pi_1" })),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ handled: false });
    expect(applyEntitlement).not.toHaveBeenCalled();
  });
});

describe("event handling", () => {
  it("handles checkout.session.completed via client_reference_id", async () => {
    await POST(
      signedRequest(
        event("checkout.session.completed", {
          id: "cs_1",
          mode: "subscription",
          subscription: "sub_123",
          client_reference_id: "user_1",
          metadata: {},
        }),
      ),
    );
    expect(applyEntitlement).toHaveBeenCalledOnce();
    expect(applyEntitlement.mock.calls[0][0]).toBe("user_1");
  });

  it("ignores a one-off payment session with no subscription", async () => {
    await POST(
      signedRequest(
        event("checkout.session.completed", { id: "cs_1", mode: "payment" }),
      ),
    );
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("resolves an invoice event through the subscription parent block", async () => {
    // `invoice.subscription` was replaced by `parent.subscription_details`;
    // reading the old field would silently drop every invoice event.
    await POST(
      signedRequest(
        event("invoice.payment_failed", {
          id: "in_1",
          parent: { subscription_details: { subscription: "sub_123" } },
        }),
      ),
    );
    expect(subscriptionsRetrieve).toHaveBeenCalledWith("sub_123");
    expect(applyEntitlement).toHaveBeenCalledOnce();
  });

  it("re-reads the subscription from Stripe rather than trusting the payload", async () => {
    // The strongest ordering protection: a late event writes CURRENT state.
    subscriptionsRetrieve.mockResolvedValue({ ...SUBSCRIPTION, status: "canceled" });
    await POST(
      signedRequest(
        event("customer.subscription.updated", { ...SUBSCRIPTION, status: "active" }),
      ),
    );
    expect(cacheFromSubscription.mock.calls[0][0].status).toBe("canceled");
  });

  it("falls back to the customer's metadata when the subscription has none", async () => {
    subscriptionsRetrieve.mockResolvedValue({ ...SUBSCRIPTION, metadata: {} });
    customersRetrieve.mockResolvedValue({
      id: "cus_123",
      deleted: false,
      metadata: { clerkUserId: "user_7" },
    });
    await POST(signedRequest(event("customer.subscription.deleted", SUBSCRIPTION)));
    expect(applyEntitlement.mock.calls[0][0]).toBe("user_7");
  });

  it("skips an event whose user cannot be resolved", async () => {
    subscriptionsRetrieve.mockResolvedValue({ ...SUBSCRIPTION, metadata: {} });
    customersRetrieve.mockResolvedValue({ id: "cus_123", deleted: false, metadata: {} });
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION)),
    );
    expect(response.status).toBe(200);
    expect(applyEntitlement).not.toHaveBeenCalled();
  });

  it("skips an event naming a user who no longer exists", async () => {
    userExists.mockResolvedValue(false);
    await POST(signedRequest(event("customer.subscription.updated", SUBSCRIPTION)));
    expect(applyEntitlement).not.toHaveBeenCalled();
  });
});

describe("replay and ordering", () => {
  it("passes the event id and creation time to the entitlement guards", async () => {
    // These two fields are what make a replay a no-op and an out-of-order event
    // a drop; the handler must hand them over rather than inventing its own.
    await POST(
      signedRequest(
        event("customer.subscription.updated", SUBSCRIPTION, {
          id: "evt_abc",
          created: 1_700_000_123,
        }),
      ),
    );
    expect(applyEntitlement.mock.calls[0][2]).toEqual({
      id: "evt_abc",
      created: 1_700_000_123,
    });
  });

  it("reports a duplicate as handled so Stripe stops retrying it", async () => {
    applyEntitlement.mockResolvedValue("duplicate");
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION)),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ outcome: "duplicate" });
  });

  it("reports a stale event as handled rather than failing it into a retry loop", async () => {
    applyEntitlement.mockResolvedValue("stale");
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION)),
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ outcome: "stale" });
  });

  it("returns 500 on a transient fault so Stripe retries", async () => {
    subscriptionsRetrieve.mockRejectedValue(new Error("network"));
    const response = await POST(
      signedRequest(event("customer.subscription.updated", SUBSCRIPTION)),
    );
    expect(response.status).toBe(500);
  });
});
