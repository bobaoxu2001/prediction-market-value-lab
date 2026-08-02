import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

/** The Customer Portal endpoint. Ownership is the whole test. */

const user = { id: "user_1", email: "a@example.com", name: null, createdAt: 1 };
let currentUser: typeof user | null = user;

vi.mock("@/lib/auth-server", () => ({
  getCurrentUser: vi.fn(async () => currentUser),
  isAuthConfigured: () => true,
}));

let billingCache: { stripeCustomerId?: string } = {};
vi.mock("@/lib/billing/entitlement", () => ({
  getBillingCache: vi.fn(async () => billingCache),
}));

const customersRetrieve = vi.fn();
const portalCreate = vi.fn();
vi.mock("@/lib/billing/stripe", () => ({
  getStripe: () => ({
    customers: { retrieve: customersRetrieve },
    billingPortal: { sessions: { create: portalCreate } },
  }),
}));

const { POST } = await import("@/app/api/billing/portal/route");
const { __resetRateLimitForTests } = await import("@/lib/http");

function enableBilling(): void {
  process.env.BILLING_MODE = "test";
  process.env.STRIPE_SECRET_KEY = "sk_test_dummy";
  process.env.STRIPE_WEBHOOK_SECRET = "whsec_dummy";
  process.env.STRIPE_PRO_MONTHLY_PRICE_ID = "price_monthly_test";
  process.env.STRIPE_PRO_ANNUAL_PRICE_ID = "price_annual_test";
}

function post(
  body: Record<string, string> = {},
  headers: Record<string, string> = {},
): NextRequest {
  return new NextRequest("http://localhost:3000/api/billing/portal", {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      "sec-fetch-site": "same-origin",
      host: "localhost:3000",
      ...headers,
    },
    body: new URLSearchParams(body).toString(),
  });
}

beforeEach(() => {
  currentUser = user;
  billingCache = { stripeCustomerId: "cus_123" };
  __resetRateLimitForTests();
  customersRetrieve.mockReset().mockResolvedValue({
    id: "cus_123",
    deleted: false,
    metadata: { clerkUserId: "user_1" },
  });
  portalCreate
    .mockReset()
    .mockResolvedValue({ url: "https://billing.stripe.com/p/session_1" });
});

describe("access", () => {
  it("requires authentication", async () => {
    enableBilling();
    currentUser = null;
    const response = await POST(post());
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toContain("/sign-in");
    expect(portalCreate).not.toHaveBeenCalled();
  });

  it("rejects a cross-site post", async () => {
    enableBilling();
    const response = await POST(post({}, { "sec-fetch-site": "cross-site" }));
    expect(response.status).toBe(403);
    expect(portalCreate).not.toHaveBeenCalled();
  });

  it("refuses when billing is disabled", async () => {
    const response = await POST(post());
    expect(response.status).toBe(503);
    expect(portalCreate).not.toHaveBeenCalled();
  });

  it("404s when the user has no billing account", async () => {
    enableBilling();
    billingCache = {};
    expect((await POST(post())).status).toBe(404);
    expect(portalCreate).not.toHaveBeenCalled();
  });
});

describe("customer ownership", () => {
  it("opens the portal for a customer this server created for this user", async () => {
    enableBilling();
    const response = await POST(post());
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toContain("billing.stripe.com");
    expect(portalCreate.mock.calls[0][0].customer).toBe("cus_123");
  });

  it("rejects a customer whose metadata names a different user", async () => {
    // The attack this closes: a stale or planted customer ID granting one
    // account access to another's invoices and payment methods.
    enableBilling();
    customersRetrieve.mockResolvedValue({
      id: "cus_123",
      deleted: false,
      metadata: { clerkUserId: "user_2" },
    });
    const response = await POST(post());
    expect(response.status).toBe(403);
    expect(portalCreate).not.toHaveBeenCalled();
  });

  it("rejects a customer with no ownership metadata at all", async () => {
    enableBilling();
    customersRetrieve.mockResolvedValue({ id: "cus_123", deleted: false, metadata: {} });
    expect((await POST(post())).status).toBe(403);
    expect(portalCreate).not.toHaveBeenCalled();
  });

  it("rejects a deleted customer", async () => {
    enableBilling();
    customersRetrieve.mockResolvedValue({ id: "cus_123", deleted: true, metadata: {} });
    expect((await POST(post())).status).toBe(403);
    expect(portalCreate).not.toHaveBeenCalled();
  });

  it("ignores a customer ID supplied in the request body", async () => {
    // There is no parameter for it, and adding one later must not start working
    // by accident.
    enableBilling();
    await POST(post({ customer: "cus_victim", customerId: "cus_victim" }));
    expect(customersRetrieve).toHaveBeenCalledWith("cus_123");
    expect(portalCreate.mock.calls[0][0].customer).toBe("cus_123");
  });
});

describe("return URL", () => {
  it("refuses an off-site return destination", async () => {
    enableBilling();
    await POST(post({ returnTo: "https://evil.example/phish" }));
    const returnUrl = portalCreate.mock.calls[0][0].return_url;
    expect(returnUrl).not.toContain("evil.example");
    expect(returnUrl).toContain("/account/billing");
  });
});
