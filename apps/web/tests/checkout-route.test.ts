import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

/**
 * The Checkout endpoint.
 *
 * Every test here is an attempt to make the server do something it must refuse:
 * charge on a deployment with billing off, honour a price the client chose,
 * check out on behalf of another account, or redirect somewhere off-site
 * afterwards.
 */

const user = { id: "user_1", email: "a@example.com", name: null, createdAt: 1 };
let currentUser: typeof user | null = user;

vi.mock("@/lib/auth-server", () => ({
  getCurrentUser: vi.fn(async () => currentUser),
  isAuthConfigured: () => true,
}));

const billingCache: { stripeCustomerId?: string; subscriptionStatus?: string } = {};
vi.mock("@/lib/billing/entitlement", async () => {
  // The live-status predicate is the real one: the point of these tests is that
  // the route and the pricing page agree, and a stubbed predicate could not
  // catch them drifting apart.
  const actual = await vi.importActual<typeof import("@/lib/billing/entitlement")>(
    "@/lib/billing/entitlement",
  );
  return {
    isLiveSubscriptionStatus: actual.isLiveSubscriptionStatus,
    getBillingCache: vi.fn(async () => billingCache),
    rememberStripeCustomerId: vi.fn(async () => true),
  };
});

const sessionsCreate = vi.fn();
const customersCreate = vi.fn();
const customersRetrieve = vi.fn();

vi.mock("@/lib/billing/stripe", () => ({
  getStripe: () => ({
    checkout: { sessions: { create: sessionsCreate } },
    customers: { create: customersCreate, retrieve: customersRetrieve },
  }),
}));

const { POST } = await import("@/app/api/billing/checkout/route");
const { __resetRateLimitForTests } = await import("@/lib/http");

function enableBilling(): void {
  process.env.BILLING_MODE = "test";
  process.env.STRIPE_SECRET_KEY = "sk_test_dummy";
  process.env.STRIPE_WEBHOOK_SECRET = "whsec_dummy";
  process.env.STRIPE_PRO_MONTHLY_PRICE_ID = "price_monthly_test";
  process.env.STRIPE_PRO_ANNUAL_PRICE_ID = "price_annual_test";
}

function post(
  body: Record<string, string>,
  headers: Record<string, string> = {},
): NextRequest {
  const form = new URLSearchParams(body);
  return new NextRequest("http://localhost:3000/api/billing/checkout", {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      "sec-fetch-site": "same-origin",
      host: "localhost:3000",
      ...headers,
    },
    body: form.toString(),
  });
}

beforeEach(() => {
  currentUser = user;
  delete billingCache.stripeCustomerId;
  delete billingCache.subscriptionStatus;
  __resetRateLimitForTests();
  sessionsCreate.mockReset().mockResolvedValue({
    id: "cs_test_1",
    url: "https://checkout.stripe.com/c/pay/cs_test_1",
  });
  customersCreate.mockReset().mockResolvedValue({ id: "cus_new", metadata: {} });
  customersRetrieve.mockReset();
});

describe("preconditions", () => {
  it("rejects a cross-site post", async () => {
    // These are cookie-authenticated form targets, so any page on the internet
    // could otherwise submit to them with the visitor's session attached.
    enableBilling();
    const response = await POST(
      post({ plan: "pro_monthly" }, { "sec-fetch-site": "cross-site" }),
    );
    expect(response.status).toBe(403);
    expect(sessionsCreate).not.toHaveBeenCalled();
  });

  it("rejects checkout when billing is disabled on the server", async () => {
    // Requirement: the server rejects checkout when billing is disabled,
    // independently of any public flag.
    process.env.NEXT_PUBLIC_BILLING_ENABLED = "true";
    const response = await POST(post({ plan: "pro_monthly" }));
    expect(response.status).toBe(503);
    expect(sessionsCreate).not.toHaveBeenCalled();
  });

  it("refuses even with a live key present, rather than charging for real", async () => {
    enableBilling();
    process.env.STRIPE_SECRET_KEY = "sk_live_not_a_real_key";
    const response = await POST(post({ plan: "pro_monthly" }));
    expect(response.status).toBe(503);
    expect(sessionsCreate).not.toHaveBeenCalled();
  });

  it("sends an unauthenticated visitor to sign-up rather than erroring", async () => {
    enableBilling();
    currentUser = null;
    const response = await POST(post({ plan: "pro_monthly" }));
    expect(response.status).toBe(303);
    const location = new URL(response.headers.get("location") ?? "");
    expect(location.pathname).toBe("/sign-up");
    // The plan is carried through so the funnel resumes where it stopped.
    expect(location.searchParams.get("redirect_url")).toContain("pro_monthly");
    expect(sessionsCreate).not.toHaveBeenCalled();
  });

  it("does not echo an unrecognised plan into the sign-up redirect", async () => {
    // Unvalidated request data must not reach a redirect an auth provider will
    // consume, whether or not this particular spelling is exploitable today.
    enableBilling();
    currentUser = null;
    const response = await POST(
      post({ plan: "https://evil.example/#" }),
    );
    const redirectUrl = new URL(
      response.headers.get("location") ?? "",
    ).searchParams.get("redirect_url");
    expect(redirectUrl).toBe("/pricing");
    expect(redirectUrl).not.toContain("evil.example");
  });

  it("rate-limits repeated requests from one user", async () => {
    enableBilling();
    const responses = [];
    for (let i = 0; i < 7; i += 1) {
      responses.push(await POST(post({ plan: "pro_monthly" })));
    }
    expect(responses.some((r) => r.status === 429)).toBe(true);
  });
});

describe("plan allowlisting", () => {
  it("rejects a Stripe price ID supplied by the client", async () => {
    // The central rule: a client that could name its own price could name a $0
    // one. There is no path from a request body to a price ID.
    enableBilling();
    for (const plan of [
      "price_1AbCdEfGhIjKlMnO",
      "price_monthly_test",
      "pro_free",
      "",
      "PRO_MONTHLY",
    ]) {
      const response = await POST(post({ plan }));
      expect(response.status).toBe(400);
    }
    expect(sessionsCreate).not.toHaveBeenCalled();
  });

  it("also ignores a priceId field entirely", async () => {
    enableBilling();
    await POST(post({ plan: "pro_monthly", priceId: "price_attacker_chosen" }));
    const args = sessionsCreate.mock.calls[0][0];
    expect(args.line_items).toEqual([{ price: "price_monthly_test", quantity: 1 }]);
  });

  it("accepts the allowlisted monthly plan in test mode", async () => {
    enableBilling();
    const response = await POST(post({ plan: "pro_monthly" }));
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toContain("checkout.stripe.com");
    const args = sessionsCreate.mock.calls[0][0];
    expect(args.mode).toBe("subscription");
    expect(args.line_items[0].price).toBe("price_monthly_test");
  });

  it("accepts the allowlisted annual plan in test mode", async () => {
    enableBilling();
    const response = await POST(post({ plan: "pro_annual" }));
    expect(response.status).toBe(303);
    expect(sessionsCreate.mock.calls[0][0].line_items[0].price).toBe("price_annual_test");
  });
});

describe("duplicate subscriptions", () => {
  it.each(["active", "trialing", "past_due", "unpaid"])(
    "refuses a second checkout while a %s subscription exists",
    async (status) => {
      // Stripe would create a second subscription on the same customer without
      // complaint: two subscriptions, two invoices, one person. The interface
      // hides the button, but a form post reaches the handler regardless.
      enableBilling();
      billingCache.subscriptionStatus = status;
      const response = await POST(post({ plan: "pro_monthly" }));
      expect(response.status).toBe(409);
      expect(sessionsCreate).not.toHaveBeenCalled();
      // The response points at the remedy rather than just refusing.
      expect(JSON.stringify(await response.json())).toMatch(/billing portal/i);
    },
  );

  it("blocks a past-due subscriber even though they are not Pro", async () => {
    // The case the narrower `!isPro` check missed. Someone whose payment failed
    // pressing "subscribe" to fix it is an ordinary way to arrive here, and it
    // would have added an invoice rather than repaired the failed one.
    enableBilling();
    billingCache.subscriptionStatus = "past_due";
    expect((await POST(post({ plan: "pro_monthly" }))).status).toBe(409);
    expect(sessionsCreate).not.toHaveBeenCalled();
  });

  it.each(["canceled", "paused", "incomplete", "incomplete_expired", undefined])(
    "allows a fresh checkout when the previous subscription is %s",
    async (status) => {
      // A cancelled subscriber resubscribing is the point of the product, and
      // `incomplete` never took a payment, so a retry is correct.
      enableBilling();
      if (status) billingCache.subscriptionStatus = status;
      const response = await POST(post({ plan: "pro_monthly" }));
      expect(response.status).toBe(303);
      expect(sessionsCreate).toHaveBeenCalledOnce();
    },
  );
});

describe("binding the session to the caller", () => {
  it("attaches the Clerk user to the session and to the subscription", async () => {
    enableBilling();
    await POST(post({ plan: "pro_monthly" }));
    const args = sessionsCreate.mock.calls[0][0];
    expect(args.client_reference_id).toBe("user_1");
    expect(args.metadata.clerkUserId).toBe("user_1");
    // Copied onto the subscription, or lifecycle events would arrive with no
    // way back to a user.
    expect(args.subscription_data.metadata.clerkUserId).toBe("user_1");
    expect(customersCreate.mock.calls[0][0].metadata.clerkUserId).toBe("user_1");
  });

  it("reuses a cached customer only when Stripe agrees it belongs to this user", async () => {
    enableBilling();
    billingCache.stripeCustomerId = "cus_existing";
    customersRetrieve.mockResolvedValue({
      id: "cus_existing",
      deleted: false,
      metadata: { clerkUserId: "user_1" },
    });
    await POST(post({ plan: "pro_monthly" }));
    expect(sessionsCreate.mock.calls[0][0].customer).toBe("cus_existing");
    expect(customersCreate).not.toHaveBeenCalled();
  });

  it("refuses to check out against a customer belonging to someone else", async () => {
    // The ownership assertion. A cached ID that points at another user's
    // customer is discarded, not used.
    enableBilling();
    billingCache.stripeCustomerId = "cus_someone_else";
    customersRetrieve.mockResolvedValue({
      id: "cus_someone_else",
      deleted: false,
      metadata: { clerkUserId: "user_2" },
    });
    await POST(post({ plan: "pro_monthly" }));
    expect(sessionsCreate.mock.calls[0][0].customer).toBe("cus_new");
  });

  it("replaces a deleted customer rather than passing it to Checkout", async () => {
    enableBilling();
    billingCache.stripeCustomerId = "cus_deleted";
    customersRetrieve.mockResolvedValue({ id: "cus_deleted", deleted: true, metadata: {} });
    await POST(post({ plan: "pro_monthly" }));
    expect(sessionsCreate.mock.calls[0][0].customer).toBe("cus_new");
  });

  it("uses a deterministic idempotency key so a double submit reuses one session", async () => {
    enableBilling();
    await POST(post({ plan: "pro_monthly" }));
    await POST(post({ plan: "pro_monthly" }));
    const [first, second] = sessionsCreate.mock.calls;
    expect(first[1].idempotencyKey).toBe(second[1].idempotencyKey);
    expect(first[1].idempotencyKey).toContain("user_1");
  });
});

describe("redirect safety", () => {
  it("ignores an arbitrary success or cancel destination", async () => {
    enableBilling();
    await POST(post({ plan: "pro_monthly", returnTo: "https://evil.example/phish" }));
    const args = sessionsCreate.mock.calls[0][0];
    expect(args.success_url).not.toContain("evil.example");
    expect(args.cancel_url).not.toContain("evil.example");
    expect(args.success_url).toContain("/account/billing");
  });

  it("honours an allowlisted destination", async () => {
    enableBilling();
    await POST(post({ plan: "pro_monthly", returnTo: "/pricing" }));
    expect(sessionsCreate.mock.calls[0][0].success_url).toContain("/pricing");
  });
});

describe("failure handling", () => {
  it("returns an opaque error when Stripe fails", async () => {
    // A caller probing the configuration must learn nothing from the response.
    enableBilling();
    sessionsCreate.mockRejectedValue(
      new Error("No such price: price_monthly_test; card 4242 declined"),
    );
    const response = await POST(post({ plan: "pro_monthly" }));
    expect(response.status).toBe(502);
    const body = await response.json();
    expect(JSON.stringify(body)).not.toContain("price_monthly_test");
    expect(JSON.stringify(body)).not.toContain("4242");
  });
});
