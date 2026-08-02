import { describe, expect, it } from "vitest";

import {
  checkoutCancelUrl,
  checkoutSuccessUrl,
  DEFAULT_RETURN_PATH,
  portalReturnUrl,
  RETURN_PATHS,
  safeReturnPath,
} from "@/lib/billing/urls";

/**
 * Redirect safety.
 *
 * An open redirect out of a payment flow has unusual leverage: the visitor
 * arrives at the attacker's page directly from Stripe, already in the frame of
 * mind that makes a credential-harvest page work.
 */

describe("safeReturnPath", () => {
  it("accepts the allowlisted paths verbatim", () => {
    for (const path of RETURN_PATHS) {
      expect(safeReturnPath(path)).toBe(path);
    }
  });

  it("falls back to the default for every hostile shape", () => {
    const hostile = [
      "https://evil.example/phish",
      "http://evil.example",
      "//evil.example",
      "\\\\evil.example",
      "/\\evil.example",
      "javascript:alert(1)",
      "data:text/html,<script>alert(1)</script>",
      "/account/../../evil",
      "/account%2F..%2Fevil",
      "/account?next=https://evil.example",
      "/account#@evil.example",
      "/account ",
      " /account",
      "/ACCOUNT",
      "/accountx",
      "/api/billing/checkout",
      "",
      null,
      undefined,
      42,
      ["/account"],
      { toString: () => "/account" },
    ];
    for (const value of hostile) {
      expect(safeReturnPath(value)).toBe(DEFAULT_RETURN_PATH);
    }
  });
});

describe("Stripe redirect URLs", () => {
  it("always builds an absolute URL on this deployment's own origin", () => {
    for (const value of ["https://evil.example/x", "//evil.example", "/account"]) {
      for (const url of [
        checkoutSuccessUrl(safeReturnPath(value)),
        checkoutCancelUrl(safeReturnPath(value)),
        portalReturnUrl(safeReturnPath(value)),
      ]) {
        const parsed = new URL(url);
        expect(parsed.host).not.toContain("evil.example");
        expect(["http:", "https:"]).toContain(parsed.protocol);
      }
    }
  });

  it("carries the Stripe session placeholder on the success URL only", () => {
    const success = checkoutSuccessUrl("/account/billing");
    expect(success).toContain("{CHECKOUT_SESSION_ID}");
    expect(success).toContain("checkout=complete");
    // The cancel URL has no session to look up and must not pretend otherwise.
    expect(checkoutCancelUrl("/account/billing")).not.toContain("{CHECKOUT_SESSION_ID}");
    expect(checkoutCancelUrl("/account/billing")).toContain("checkout=cancelled");
  });
});
