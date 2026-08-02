import { NextResponse, type NextRequest } from "next/server";

import { getCurrentUser } from "@/lib/auth-server";
import { isPlanId, isBillingEnabled, priceIdForPlan } from "@/lib/billing/config";
import {
  getBillingCache,
  isLiveSubscriptionStatus,
  rememberStripeCustomerId,
} from "@/lib/billing/entitlement";
import { getStripe } from "@/lib/billing/stripe";
import {
  checkoutCancelUrl,
  checkoutSuccessUrl,
  safeReturnPath,
} from "@/lib/billing/urls";
import {
  isSameOriginRequest,
  logBilling,
  rateLimit,
  readBodyFields,
} from "@/lib/http";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Create a Stripe Checkout Session.
 *
 * The order of the checks below is the design. Each one is cheap and refuses
 * before the next becomes reachable:
 *
 *   1. Same-origin, because this is a cookie-authenticated form target.
 *   2. Billing enabled *on the server*. The public flag has no vote here; a
 *      deployment with `NEXT_PUBLIC_BILLING_ENABLED=true` and no server-side
 *      billing mode gets a 503 from this line, which is the property that makes
 *      the public flag safe to expose at all.
 *   3. Authenticated. Anonymous callers are sent to sign-up and returned to the
 *      plan they asked for, rather than being told to find it again.
 *   4. Rate limit, per user.
 *   5. Plan on the server-side allowlist. A price ID in the request body is
 *      never read - the only mapping from a plan to a price lives in
 *      `priceIdForPlan`, on the server, keyed by an identifier the client can
 *      only choose from a fixed set of two.
 *
 * The session is then bound to the caller: a Stripe customer that this server
 * created for THIS Clerk user, `client_reference_id` set to that user, and the
 * user ID in both session and subscription metadata so the webhook can resolve
 * a subject without trusting anything the browser sent back.
 */
export async function POST(request: NextRequest) {
  if (!isSameOriginRequest(request)) {
    logBilling("checkout.rejected", { reason: "cross_origin" });
    return NextResponse.json({ error: "Bad request." }, { status: 403 });
  }

  const fields = await readBodyFields(request);
  const returnPath = safeReturnPath(fields.returnTo);

  if (!isBillingEnabled()) {
    logBilling("checkout.rejected", { reason: "billing_disabled" });
    return NextResponse.json(
      { error: "Billing is not available on this deployment." },
      { status: 503 },
    );
  }

  const user = await getCurrentUser();
  if (!user) {
    // Not an error - a signed-out visitor pressing a plan button is the ordinary
    // path. Carry the plan through so the funnel resumes where it stopped.
    const target = new URL("/sign-up", request.url);
    target.searchParams.set("redirect_url", `/pricing?plan=${fields.plan ?? ""}`);
    logBilling("checkout.redirect_signup", { plan: fields.plan ?? null });
    return NextResponse.redirect(target, 303);
  }

  const limit = rateLimit(`checkout:${user.id}`, 5, 60_000);
  if (!limit.allowed) {
    logBilling("checkout.rejected", { reason: "rate_limited", userId: user.id });
    return NextResponse.json(
      { error: "Too many requests. Try again shortly." },
      { status: 429, headers: { "Retry-After": String(limit.retryAfterSeconds) } },
    );
  }

  const plan = fields.plan;
  if (!isPlanId(plan)) {
    logBilling("checkout.rejected", { reason: "unknown_plan", userId: user.id });
    return NextResponse.json({ error: "Unknown plan." }, { status: 400 });
  }

  // Refuse a second subscription for a customer who already has one.
  //
  // Stripe Checkout will happily create a second subscription on the same
  // customer, and the result is two concurrent subscriptions and two invoices
  // for one person. The interface hides the buttons, but the interface is not a
  // control: a form post reaches this handler regardless of what was rendered,
  // and a past-due subscriber pressing "subscribe" to fix a failed payment is
  // an ordinary, sympathetic way to arrive here by accident.
  //
  // The answer for an existing subscriber - changing plan, retrying a payment,
  // cancelling - is the Customer Portal, which is what the response points at.
  const existing = await getBillingCache(user.id);
  if (isLiveSubscriptionStatus(existing.subscriptionStatus)) {
    logBilling("checkout.rejected", {
      reason: "subscription_already_exists",
      userId: user.id,
      plan,
    });
    return NextResponse.json(
      {
        error:
          "This account already has a subscription. Manage it from the billing portal.",
      },
      { status: 409 },
    );
  }

  const priceId = priceIdForPlan(plan);
  const stripe = getStripe();
  if (!priceId || !stripe) {
    logBilling("checkout.rejected", { reason: "no_price_or_client", userId: user.id });
    return NextResponse.json(
      { error: "Billing is not available on this deployment." },
      { status: 503 },
    );
  }

  try {
    const customerId = await resolveCustomerId(stripe, user.id, user.email);

    const session = await stripe.checkout.sessions.create(
      {
        mode: "subscription",
        customer: customerId,
        line_items: [{ price: priceId, quantity: 1 }],
        // Ties the session to the Clerk user at Stripe's own level, so a session
        // created for one account cannot be completed into another.
        client_reference_id: user.id,
        metadata: { clerkUserId: user.id, plan },
        // Copied onto the subscription, which is the object the lifecycle
        // webhooks carry. Without it, `customer.subscription.updated` would
        // arrive with no way back to a user except a customer-ID lookup.
        subscription_data: { metadata: { clerkUserId: user.id, plan } },
        success_url: checkoutSuccessUrl(returnPath),
        cancel_url: checkoutCancelUrl(returnPath),
        allow_promotion_codes: false,
      },
      {
        // Deterministic across instances, so a double-submitted form reuses one
        // session instead of creating two. Bucketed by minute rather than
        // fixed, so a genuine retry after an abandoned checkout still works.
        idempotencyKey: `checkout:${user.id}:${plan}:${Math.floor(Date.now() / 60_000)}`,
      },
    );

    if (!session.url) {
      logBilling("checkout.failed", { reason: "no_session_url", userId: user.id });
      return NextResponse.json({ error: "Checkout unavailable." }, { status: 502 });
    }

    logBilling("checkout.created", {
      userId: user.id,
      plan,
      sessionId: session.id,
      customerId,
    });
    return NextResponse.redirect(session.url, 303);
  } catch (error) {
    // The Stripe error is logged by type only. Its message can contain the
    // request payload, and the response to the browser says nothing at all
    // about why - a caller probing for configuration learns nothing.
    logBilling("checkout.failed", {
      reason: "stripe_error",
      userId: user.id,
      plan,
      errorName: error instanceof Error ? error.name : "unknown",
    });
    return NextResponse.json({ error: "Checkout unavailable." }, { status: 502 });
  }
}

/**
 * Find or create this user's Stripe customer.
 *
 * Reuses the cached ID when it still resolves to a live, non-deleted customer
 * that Stripe agrees belongs to this Clerk user. A stale ID - one from a
 * different Stripe account, or a customer deleted in the dashboard - is
 * discarded and replaced rather than passed to Checkout, where it would fail
 * with an error the visitor cannot act on.
 */
async function resolveCustomerId(
  stripe: NonNullable<ReturnType<typeof getStripe>>,
  userId: string,
  email: string | null,
): Promise<string> {
  const cache = await getBillingCache(userId);

  if (cache.stripeCustomerId) {
    try {
      const existing = await stripe.customers.retrieve(cache.stripeCustomerId);
      if (
        !existing.deleted &&
        // The ownership assertion. Stripe metadata is written only by this
        // server, so a customer whose metadata names a different Clerk user is
        // not one this user may check out against.
        existing.metadata?.clerkUserId === userId
      ) {
        return existing.id;
      }
    } catch {
      // Fall through and create a fresh customer.
    }
  }

  const created = await stripe.customers.create(
    {
      email: email ?? undefined,
      metadata: { clerkUserId: userId },
    },
    { idempotencyKey: `customer:${userId}` },
  );
  await rememberStripeCustomerId(userId, created.id);
  return created.id;
}
