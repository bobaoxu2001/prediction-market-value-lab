import "server-only";

import type { NextRequest } from "next/server";

import { SITE_URL } from "@/lib/site";

/**
 * Request-level guards shared by the billing route handlers.
 */

/**
 * Cross-site request forgery check for state-changing form posts.
 *
 * The billing routes are `<form method="post">` targets, so they are reachable
 * from any page on the internet by default. Clerk's session cookie would be sent
 * with such a request, which is the whole shape of a CSRF.
 *
 * Two signals, either of which suffices:
 *
 *   - `Sec-Fetch-Site: same-origin`, which the browser sets and page JavaScript
 *     cannot forge. This is the strong one.
 *   - `Origin` matching this deployment's own origin, for the older clients that
 *     do not send fetch metadata.
 *
 * A request with neither is rejected. Note the deliberate asymmetry: a *missing*
 * `Origin` is not treated as trustworthy, because that is exactly what a
 * stripped-header request looks like.
 */
export function isSameOriginRequest(request: NextRequest): boolean {
  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite === "same-origin" || fetchSite === "none") return true;
  if (fetchSite) return false;

  const origin = request.headers.get("origin");
  if (!origin) return false;
  if (origin === SITE_URL) return true;

  // A Preview deployment is served from a hostname that `SITE_URL` deliberately
  // does not name (see lib/site.ts). Accept the request's own host as the
  // origin of record, which still excludes every third-party site.
  const host = request.headers.get("host");
  if (!host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

/**
 * A best-effort per-instance rate limiter.
 *
 * Explicitly NOT a distributed one. On serverless this counts requests within a
 * single warm instance, so a determined caller spread across instances gets more
 * than the stated budget. It is here to stop an accidental loop and a
 * double-submitting form, not to withstand an attacker, and the honest place to
 * record that is here rather than in a document claiming rate limiting exists.
 *
 * The real protection against duplicate checkouts is the Stripe idempotency key
 * in the checkout route, which is deterministic and therefore instance-agnostic.
 */
const buckets = new Map<string, { count: number; resetAt: number }>();

export function rateLimit(
  key: string,
  limit: number,
  windowMs: number,
): { allowed: boolean; retryAfterSeconds: number } {
  const now = Date.now();
  const bucket = buckets.get(key);

  if (!bucket || bucket.resetAt <= now) {
    buckets.set(key, { count: 1, resetAt: now + windowMs });
    // Opportunistic sweep: without it the map grows for the life of the
    // instance, one entry per user that ever hit the route.
    if (buckets.size > 500) {
      for (const [existing, value] of buckets) {
        if (value.resetAt <= now) buckets.delete(existing);
      }
    }
    return { allowed: true, retryAfterSeconds: 0 };
  }

  bucket.count += 1;
  if (bucket.count > limit) {
    return {
      allowed: false,
      retryAfterSeconds: Math.max(1, Math.ceil((bucket.resetAt - now) / 1000)),
    };
  }
  return { allowed: true, retryAfterSeconds: 0 };
}

/** Reset the limiter. Tests only. */
export function __resetRateLimitForTests(): void {
  buckets.clear();
}

/**
 * Read a field from either a form post or a JSON body.
 *
 * Both shapes are supported because the pages submit real forms (which work
 * without JavaScript) while tests and any future client code find JSON easier.
 * Returns an empty object on a malformed body rather than throwing: a parse
 * failure should become a 400 from the caller's own validation, not a 500.
 */
export async function readBodyFields(
  request: NextRequest,
): Promise<Record<string, string>> {
  const contentType = request.headers.get("content-type") ?? "";
  try {
    if (contentType.includes("application/json")) {
      const parsed: unknown = await request.json();
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
      const out: Record<string, string> = {};
      for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof value === "string") out[key] = value;
      }
      return out;
    }
    const form = await request.formData();
    const out: Record<string, string> = {};
    for (const [key, value] of form.entries()) {
      if (typeof value === "string") out[key] = value;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Structured log line for a billing event.
 *
 * Takes only fields the caller has chosen. Nothing in the billing routes passes
 * a secret, a signature, a raw body or an email address through here, and the
 * `redactions` guard makes a future mistake loud rather than silent.
 */
const FORBIDDEN_LOG_KEYS = /secret|signature|key$|token|password|card|cvc/i;

export function logBilling(
  event: string,
  fields: Record<string, string | number | boolean | null | undefined>,
): void {
  const safe: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(fields)) {
    safe[key] = FORBIDDEN_LOG_KEYS.test(key) ? "[redacted]" : value;
  }
  console.info(JSON.stringify({ event: `billing.${event}`, ...safe }));
}
