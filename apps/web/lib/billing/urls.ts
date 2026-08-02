import "server-only";

import { deploymentOrigin } from "@/lib/site";

/**
 * Redirect construction for Stripe.
 *
 * A Checkout Session's `success_url` and `cancel_url` are absolute URLs that
 * Stripe will send a browser to. Building either one from a client-supplied
 * string is a textbook open redirect, and one with unusual leverage: the visitor
 * arrives at it straight from a payment page, in exactly the frame of mind that
 * makes a convincing credential-harvest landing page work.
 *
 * So no caller may supply a URL. A caller may supply a *path*, and only one from
 * the allowlist below. Everything else silently falls back to the default -
 * silently, because telling a prober which paths were rejected is free
 * reconnaissance.
 */

/** Paths a post-checkout or post-portal redirect may land on. */
const RETURN_PATH_ALLOWLIST = [
  "/account",
  "/account/billing",
  "/pricing",
  "/app",
] as const;

export type ReturnPath = (typeof RETURN_PATH_ALLOWLIST)[number];

export const DEFAULT_RETURN_PATH: ReturnPath = "/account/billing";

/**
 * Normalise a caller-supplied return path.
 *
 * Rejects anything that is not an exact, literal member of the allowlist. That
 * rules out absolute URLs, protocol-relative `//evil.example`, backslash
 * variants, encoded traversal and query-string smuggling in a single check,
 * rather than trying to filter a hostile string into safety.
 */
export function safeReturnPath(raw: unknown): ReturnPath {
  if (typeof raw !== "string") return DEFAULT_RETURN_PATH;
  const match = RETURN_PATH_ALLOWLIST.find((path) => path === raw);
  return match ?? DEFAULT_RETURN_PATH;
}

function absolute(path: string): string {
  // `deploymentOrigin()` is normalised to a bare origin in lib/site.ts, so this
  // cannot produce a cross-origin URL however the environment is configured -
  // and it returns the deployment the visitor is actually on, so a Preview
  // checkout returns to that Preview rather than to production.
  return `${deploymentOrigin()}${path}`;
}

/**
 * Where Stripe returns a completed checkout.
 *
 * `{CHECKOUT_SESSION_ID}` is substituted by Stripe. The landing page uses it
 * only to look the session up server-side and show an accurate status - it is
 * never treated as proof of payment. Payment is confirmed by the webhook, which
 * is the only path that writes an entitlement.
 */
export function checkoutSuccessUrl(returnPath: ReturnPath): string {
  return absolute(`${returnPath}?checkout=complete&session_id={CHECKOUT_SESSION_ID}`);
}

export function checkoutCancelUrl(returnPath: ReturnPath): string {
  return absolute(`${returnPath}?checkout=cancelled`);
}

export function portalReturnUrl(returnPath: ReturnPath): string {
  return absolute(`${returnPath}?portal=return`);
}

/** Exposed for tests and for the setup documentation. */
export const RETURN_PATHS = RETURN_PATH_ALLOWLIST;
