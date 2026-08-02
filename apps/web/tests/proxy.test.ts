import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

/**
 * The request proxy.
 *
 * The encoded-separator case is here because it was a real, deployed **500**:
 * `GET /account%2Fbilling` on Vercel matched no App Router route, fell through
 * to the platform's page launcher, and threw MODULE_NOT_FOUND while trying to
 * `require()` a module named after the raw path. `next start` returns 404 for
 * the same request, so a local test could never have found it - which is why
 * the regression test asserts the behaviour rather than the platform.
 */

vi.mock("@clerk/nextjs/server", () => ({
  clerkMiddleware: (handler: unknown) => handler,
}));

const { default: proxy } = await import("@/proxy");

// The proxy's second argument is only forwarded to Clerk, which is stubbed out.
const EVENT = {} as never;

function get(path: string): NextRequest {
  return new NextRequest(`https://pmvl.example${path}`, { method: "GET" });
}

/**
 * `proxy` returns synchronously on the unconfigured path and delegates to Clerk
 * otherwise, so its declared type is a union with a promise. Awaiting collapses
 * both without the test asserting which branch it happened to take.
 */
async function run(path: string) {
  return (await proxy(get(path), EVENT)) ?? undefined;
}

beforeEach(() => {
  delete process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
  delete process.env.CLERK_SECRET_KEY;
});

describe("encoded path separators", () => {
  it.each([
    "/account%2Fbilling",
    "/account%2fbilling",
    "/account%5Cbilling",
    "/markets%2Ffoo",
    "/%2Faccount",
    "/api%2Fstripe%2Fwebhook",
  ])("404s %s instead of letting it reach route matching", async (path) => {
    const response = await run(path);
    expect(response?.status).toBe(404);
  });

  it("leaves an encoded slash in the QUERY STRING alone", async () => {
    // Encoded slashes are ordinary in a query value; only the path is ambiguous.
    const response = await run("/pricing?next=%2Faccount");
    expect(response?.status).not.toBe(404);
  });

  it("does not disturb ordinary paths", async () => {
    for (const path of ["/", "/app", "/pricing", "/market/630", "/sign-in"]) {
      expect((await run(path))?.status).not.toBe(404);
    }
  });
});

describe("protected routes with no auth provider configured", () => {
  it("redirects /account to a sign-in page that explains why", async () => {
    const response = await run("/account");
    expect(response?.status).toBe(307);
    const location = new URL(response?.headers.get("location") ?? "");
    expect(location.pathname).toBe("/sign-in");
    expect(location.searchParams.get("reason")).toBe("auth-unavailable");
  });

  it("fails closed for every path under /account", async () => {
    for (const path of ["/account", "/account/billing", "/account/anything"]) {
      expect((await run(path))?.status).toBe(307);
    }
  });

  it("never gates public research or the Stripe webhook", async () => {
    // A misconfigured auth provider must not be able to take the research away,
    // and an auth redirect in front of the webhook would swallow every event.
    for (const path of [
      "/",
      "/app",
      "/markets",
      "/arbitrage",
      "/system",
      "/pricing",
      "/api/stripe/webhook",
    ]) {
      expect((await run(path))?.status, path).not.toBe(307);
    }
  });

  it("does not protect a lookalike prefix", async () => {
    for (const path of ["/accounts", "/account-recovery"]) {
      expect((await run(path))?.status, path).not.toBe(307);
    }
  });
});
