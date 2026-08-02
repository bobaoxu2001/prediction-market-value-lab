import { NextResponse, type NextRequest } from "next/server";

import { getCurrentUser } from "@/lib/auth-server";
import { isBillingEnabled } from "@/lib/billing/config";
import { getBillingCache } from "@/lib/billing/entitlement";
import { getStripe } from "@/lib/billing/stripe";
import { portalReturnUrl, safeReturnPath } from "@/lib/billing/urls";
import {
  isSameOriginRequest,
  logBilling,
  rateLimit,
  readBodyFields,
} from "@/lib/http";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Open a Stripe Customer Portal session.
 *
 * The customer ID is never read from the request. It comes from this user's own
 * server-side billing cache, and is then checked against Stripe: the customer
 * must exist, must not be deleted, and must carry this Clerk user's ID in the
 * metadata that only this server writes. A caller who guesses or steals another
 * account's `cus_…` gains nothing, because there is no parameter through which
 * to supply it and no code path that would honour it.
 *
 * The portal is also the whole of subscription management. Cancellation, payment
 * method changes and invoices happen inside Stripe's own interface, so this
 * application never handles a card number and never needs a cancel endpoint of
 * its own to get wrong.
 */
export async function POST(request: NextRequest) {
  if (!isSameOriginRequest(request)) {
    logBilling("portal.rejected", { reason: "cross_origin" });
    return NextResponse.json({ error: "Bad request." }, { status: 403 });
  }

  const user = await getCurrentUser();
  if (!user) {
    logBilling("portal.rejected", { reason: "unauthenticated" });
    return NextResponse.redirect(new URL("/sign-in", request.url), 303);
  }

  if (!isBillingEnabled()) {
    logBilling("portal.rejected", { reason: "billing_disabled", userId: user.id });
    return NextResponse.json(
      { error: "Billing is not available on this deployment." },
      { status: 503 },
    );
  }

  const limit = rateLimit(`portal:${user.id}`, 5, 60_000);
  if (!limit.allowed) {
    logBilling("portal.rejected", { reason: "rate_limited", userId: user.id });
    return NextResponse.json(
      { error: "Too many requests. Try again shortly." },
      { status: 429, headers: { "Retry-After": String(limit.retryAfterSeconds) } },
    );
  }

  const fields = await readBodyFields(request);
  const returnPath = safeReturnPath(fields.returnTo);

  const cache = await getBillingCache(user.id);
  if (!cache.stripeCustomerId) {
    logBilling("portal.rejected", { reason: "no_customer", userId: user.id });
    return NextResponse.json(
      { error: "No billing account exists for this user." },
      { status: 404 },
    );
  }

  const stripe = getStripe();
  if (!stripe) {
    return NextResponse.json(
      { error: "Billing is not available on this deployment." },
      { status: 503 },
    );
  }

  try {
    const customer = await stripe.customers.retrieve(cache.stripeCustomerId);
    if (customer.deleted || customer.metadata?.clerkUserId !== user.id) {
      // A mismatch is a real anomaly - the cached ID points at a customer this
      // server did not create for this user - so it is logged as such and
      // refused, rather than repaired by silently issuing a new customer.
      logBilling("portal.rejected", {
        reason: "customer_ownership_mismatch",
        userId: user.id,
        customerId: cache.stripeCustomerId,
      });
      return NextResponse.json({ error: "Billing account unavailable." }, { status: 403 });
    }

    const session = await stripe.billingPortal.sessions.create({
      customer: customer.id,
      return_url: portalReturnUrl(returnPath),
    });

    logBilling("portal.opened", { userId: user.id, customerId: customer.id });
    return NextResponse.redirect(session.url, 303);
  } catch (error) {
    logBilling("portal.failed", {
      reason: "stripe_error",
      userId: user.id,
      errorName: error instanceof Error ? error.name : "unknown",
    });
    return NextResponse.json({ error: "Billing portal unavailable." }, { status: 502 });
  }
}
