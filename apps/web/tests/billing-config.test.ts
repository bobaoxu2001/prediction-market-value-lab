import { describe, expect, it } from "vitest";

import {
  getBillingConfig,
  isBillingEnabled,
  isPlanId,
  priceIdForPlan,
  shouldShowCheckoutUi,
} from "@/lib/billing/config";

/** A complete, valid test-mode configuration. */
function configureTestBilling(): void {
  process.env.BILLING_MODE = "test";
  process.env.STRIPE_SECRET_KEY = "sk_test_not_a_real_key";
  process.env.STRIPE_WEBHOOK_SECRET = "whsec_not_a_real_secret";
  process.env.STRIPE_PRO_MONTHLY_PRICE_ID = "price_monthly_test";
  process.env.STRIPE_PRO_ANNUAL_PRICE_ID = "price_annual_test";
}

describe("billing gate", () => {
  it("is disabled when nothing is configured, which is the production state", () => {
    // Requirement: production billing remains disabled by default.
    const config = getBillingConfig();
    expect(config.enabled).toBe(false);
    expect(config.mode).toBe("disabled");
    expect(config.reason).toBe("billing_mode_not_test");
    expect(isBillingEnabled()).toBe(false);
  });

  it("stays disabled when only the public flag is set", () => {
    // The property the whole design rests on: a public flag is inlined into the
    // browser bundle and can be edited by anyone holding the JavaScript, so it
    // must not be able to activate anything on its own.
    process.env.NEXT_PUBLIC_BILLING_ENABLED = "true";
    expect(isBillingEnabled()).toBe(false);
    expect(shouldShowCheckoutUi()).toBe(false);
  });

  it("rejects a live secret key instead of honouring it", () => {
    // A live key is a mistake. The safe response to a mistake about money is to
    // do nothing, not to do it for real.
    configureTestBilling();
    process.env.STRIPE_SECRET_KEY = "sk_live_not_a_real_key";
    const config = getBillingConfig();
    expect(config.enabled).toBe(false);
    expect(config.reason).toBe("live_secret_key_rejected");
    expect(config.secretKey).toBeNull();
  });

  it("requires a webhook secret, because an unconfirmable grant is worse than none", () => {
    configureTestBilling();
    delete process.env.STRIPE_WEBHOOK_SECRET;
    expect(getBillingConfig().reason).toBe("missing_webhook_secret");
  });

  it("requires both allowlisted price IDs", () => {
    configureTestBilling();
    delete process.env.STRIPE_PRO_ANNUAL_PRICE_ID;
    expect(getBillingConfig().reason).toBe("missing_price_ids");
  });

  it("enables only on a complete test-mode configuration", () => {
    configureTestBilling();
    const config = getBillingConfig();
    expect(config.enabled).toBe(true);
    expect(config.mode).toBe("test");
    expect(config.reason).toBeNull();
  });

  it("shows checkout UI only when the public flag AND the server gate agree", () => {
    configureTestBilling();
    expect(shouldShowCheckoutUi()).toBe(false);
    process.env.NEXT_PUBLIC_BILLING_ENABLED = "true";
    expect(shouldShowCheckoutUi()).toBe(true);
  });

  it("never resolves an unknown billing mode to an enabled state", () => {
    configureTestBilling();
    process.env.BILLING_MODE = "live";
    expect(getBillingConfig().enabled).toBe(false);
    process.env.BILLING_MODE = "TEST ";
    // Trimmed and lowercased, so ordinary whitespace is tolerated...
    expect(getBillingConfig().enabled).toBe(true);
    process.env.BILLING_MODE = "enabled";
    // ...but an invented value is not.
    expect(getBillingConfig().enabled).toBe(false);
  });
});

describe("plan allowlist", () => {
  it("accepts only the two server-side plan identifiers", () => {
    expect(isPlanId("pro_monthly")).toBe(true);
    expect(isPlanId("pro_annual")).toBe(true);
    for (const value of [
      "price_1AbCdEfGhIjKlMnO",
      "pro_free",
      "",
      "__proto__",
      "pro_monthly ",
      null,
      undefined,
      42,
      { plan: "pro_monthly" },
    ]) {
      expect(isPlanId(value)).toBe(false);
    }
  });

  it("maps a plan to a price only when billing is enabled", () => {
    expect(priceIdForPlan("pro_monthly")).toBeNull();
    configureTestBilling();
    expect(priceIdForPlan("pro_monthly")).toBe("price_monthly_test");
    expect(priceIdForPlan("pro_annual")).toBe("price_annual_test");
  });
});
