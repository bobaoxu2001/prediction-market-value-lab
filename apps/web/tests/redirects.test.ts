import { afterEach, describe, expect, it, vi } from "vitest";

import {
  checkoutCancelUrl,
  checkoutSuccessUrl,
  DEFAULT_RETURN_PATH,
  portalReturnUrl,
  RETURN_PATHS,
  safeReturnPath,
} from "@/lib/billing/urls";

afterEach(() => {
  // These tests reload the module graph to re-resolve the origin; restore it so
  // the statically imported helpers above stay the ones under test.
  vi.resetModules();
});

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

describe("which origin a Stripe redirect returns to", () => {
  // The canonical origin and the return-trip origin want opposite things, and
  // using one for both sent a completed Preview checkout to production - a
  // different deployment, with billing disabled, that knows nothing about the
  // subscription just created. The flow appeared broken exactly when someone
  // was verifying that it worked.
  async function urlsWithEnv(env: Record<string, string | undefined>) {
    for (const [key, value] of Object.entries(env)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    vi.resetModules();
    return import("@/lib/billing/urls");
  }

  it("returns to the Preview deployment when running on a Preview", async () => {
    const urls = await urlsWithEnv({
      VERCEL_ENV: "preview",
      VERCEL_BRANCH_URL: "pmvl-web-git-some-branch.vercel.app",
      VERCEL_PROJECT_PRODUCTION_URL: "pmvl-web.vercel.app",
    });
    const success = urls.checkoutSuccessUrl("/account/billing");
    expect(new URL(success).host).toBe("pmvl-web-git-some-branch.vercel.app");
    expect(success).not.toContain("pmvl-web.vercel.app/");
  });

  it("still uses the production host off Preview", async () => {
    const urls = await urlsWithEnv({
      VERCEL_ENV: "production",
      VERCEL_BRANCH_URL: "pmvl-web-git-some-branch.vercel.app",
      VERCEL_PROJECT_PRODUCTION_URL: "pmvl-web.vercel.app",
    });
    expect(new URL(urls.checkoutSuccessUrl("/account/billing")).host).toBe(
      "pmvl-web.vercel.app",
    );
  });

  it("lets an explicit NEXT_PUBLIC_SITE_URL win everywhere", async () => {
    const urls = await urlsWithEnv({
      NEXT_PUBLIC_SITE_URL: "https://pmvl.example",
      VERCEL_ENV: "preview",
      VERCEL_BRANCH_URL: "pmvl-web-git-some-branch.vercel.app",
    });
    expect(new URL(urls.portalReturnUrl("/account")).host).toBe("pmvl.example");
  });

  it("never produces an off-site origin from a hostile branch host", async () => {
    const urls = await urlsWithEnv({
      VERCEL_ENV: "preview",
      VERCEL_BRANCH_URL: "evil.example/path?x=1#frag",
    });
    // `normaliseOrigin` strips everything but the origin, so a path smuggled
    // into the variable cannot prefix the redirect target.
    const success = urls.checkoutSuccessUrl("/account/billing");
    expect(success.startsWith("https://evil.example/account/billing")).toBe(true);
    expect(success).not.toContain("/path");
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
