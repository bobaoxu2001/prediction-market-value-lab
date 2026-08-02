/**
 * Which routes are private, and which can never be gated.
 *
 * Extracted from `proxy.ts` so the policy is a plain, importable function that
 * a test can enumerate against. The proxy's own matcher config is a regex in a
 * build-time export and cannot be exercised directly, which is exactly the kind
 * of thing that silently starts protecting - or silently stops protecting - the
 * wrong paths.
 *
 * Safe on the client: it contains no secret and reads no environment.
 */

/** Routes that require an authenticated session. */
export function isProtectedPath(pathname: string): boolean {
  return pathname === "/account" || pathname.startsWith("/account/");
}

/**
 * Routes that must never be gated, whatever `isProtectedPath` grows into.
 *
 * `/api/stripe/webhook` is the one that matters: Stripe is not a signed-in
 * user, and an auth redirect in front of it would silently swallow every event
 * while returning a 200-looking redirect that Stripe records as delivered.
 */
export function isAlwaysOpenPath(pathname: string): boolean {
  return (
    pathname.startsWith("/api/stripe/") ||
    pathname.startsWith("/sign-in") ||
    pathname.startsWith("/sign-up")
  );
}

/**
 * The public research surfaces.
 *
 * Listed explicitly rather than derived, so that a change which accidentally
 * brings one of them under authentication fails a test that names it. The
 * research product being public is a product decision, not an implementation
 * detail.
 */
export const PUBLIC_RESEARCH_PATHS = [
  "/",
  "/app",
  "/markets",
  "/market/630",
  "/arbitrage",
  "/backtest",
  "/track-record",
  "/methodology",
  "/system",
  "/demo",
  "/case-study",
  "/pricing",
  "/terms",
  "/privacy",
  "/risk-disclosure",
] as const;
