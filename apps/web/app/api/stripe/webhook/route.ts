import { NextResponse, type NextRequest } from "next/server";
import type Stripe from "stripe";

import { userExists } from "@/lib/auth-server";
import { getBillingConfig } from "@/lib/billing/config";
import {
  applyEntitlement,
  cacheFromSubscription,
  type ApplyOutcome,
} from "@/lib/billing/entitlement";
import { getStripe } from "@/lib/billing/stripe";
import { logBilling } from "@/lib/http";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * The Stripe webhook. The only writer of entitlement state.
 *
 * Everything about this handler follows from one fact: a browser returning from
 * Checkout proves nothing. It can be replayed, forged, or simply never happen -
 * a visitor who closes the tab after paying still paid. So the success URL
 * updates no state at all, and this endpoint, which Stripe signs, is where an
 * entitlement is granted, suspended and revoked.
 *
 * The three properties that make it safe to run unauthenticated:
 *
 *   - **Signature verification over the raw body.** `request.text()` before any
 *     parsing, because `constructEvent` hashes the exact bytes Stripe signed and
 *     a round-trip through `JSON.parse`/`stringify` changes them.
 *   - **Idempotency.** Stripe retries on any non-2xx, and at-least-once delivery
 *     means a duplicate is normal traffic, not an attack. The last applied event
 *     ID is stored with the state it produced.
 *   - **Ordering.** Delivery is not ordered. An event older than the last one
 *     applied is dropped, and the state written is read back from Stripe rather
 *     than taken from the event payload wherever the payload could be stale.
 */

/** Events that can change an entitlement. Everything else is acknowledged and ignored. */
const HANDLED = new Set<Stripe.Event["type"]>([
  "checkout.session.completed",
  "customer.subscription.created",
  "customer.subscription.updated",
  "customer.subscription.deleted",
  "invoice.paid",
  "invoice.payment_failed",
]);

export async function POST(request: NextRequest) {
  const config = getBillingConfig();
  const stripe = getStripe();

  if (!config.enabled || !config.webhookSecret || !stripe) {
    // 503 rather than 200: Stripe should retry once the deployment is
    // configured, instead of recording a delivery that did nothing.
    logBilling("webhook.rejected", { reason: config.reason ?? "billing_disabled" });
    return NextResponse.json({ error: "Billing is not enabled." }, { status: 503 });
  }

  const signature = request.headers.get("stripe-signature");
  if (!signature) {
    logBilling("webhook.rejected", { reason: "missing_signature" });
    return NextResponse.json({ error: "Missing signature." }, { status: 400 });
  }

  // Raw bytes, before anything parses them.
  const payload = await request.text();

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(
      payload,
      signature,
      config.webhookSecret,
    );
  } catch (error) {
    // A bad signature is the one case that must never be retried into success,
    // so it is a 400 and the reason is never echoed back to the caller.
    logBilling("webhook.rejected", {
      reason: "invalid_signature",
      errorName: error instanceof Error ? error.name : "unknown",
    });
    return NextResponse.json({ error: "Invalid signature." }, { status: 400 });
  }

  if (!HANDLED.has(event.type)) {
    logBilling("webhook.ignored", { eventId: event.id, type: event.type });
    return NextResponse.json({ received: true, handled: false });
  }

  // Test-mode only, belt and braces. `getBillingConfig` already refuses a live
  // secret key, so a livemode event cannot be signed by a secret this
  // deployment holds - but if that ever changed, dropping the event is the
  // behaviour we want, not applying it.
  if (event.livemode) {
    logBilling("webhook.rejected", { reason: "livemode_event", eventId: event.id });
    return NextResponse.json({ error: "Live mode is not enabled." }, { status: 400 });
  }

  try {
    const outcome = await handleEvent(stripe, event);
    logBilling("webhook.handled", {
      eventId: event.id,
      type: event.type,
      outcome: outcome.outcome,
      userId: outcome.userId,
      reason: outcome.reason,
    });
    return NextResponse.json({ received: true, outcome: outcome.outcome });
  } catch (error) {
    // A 500 asks Stripe to retry, which is right for a transient fault. The
    // idempotency guard makes that retry safe.
    logBilling("webhook.failed", {
      eventId: event.id,
      type: event.type,
      errorName: error instanceof Error ? error.name : "unknown",
    });
    return NextResponse.json({ error: "Handler failed." }, { status: 500 });
  }
}

interface HandleResult {
  outcome: ApplyOutcome | "skipped";
  userId?: string | null;
  reason?: string;
}

async function handleEvent(
  stripe: Stripe,
  event: Stripe.Event,
): Promise<HandleResult> {
  const subscriptionId = subscriptionIdFor(event);
  if (!subscriptionId) {
    // An invoice unrelated to a subscription, or a checkout session in payment
    // mode. Neither changes an entitlement.
    return { outcome: "skipped", reason: "no_subscription" };
  }

  // Always read the subscription back from Stripe rather than trusting the
  // event's embedded copy.
  //
  // This is the strongest of the ordering protections: a late-delivered event
  // retrieves the state as it is NOW, so a stale payload cannot be written even
  // if it slipped past the timestamp guard. It also fills in what the thinner
  // payloads - an invoice event carries no subscription status - simply do not
  // contain.
  const subscription = await stripe.subscriptions.retrieve(subscriptionId);

  const userId = await resolveUserId(stripe, event, subscription);
  if (!userId) {
    return { outcome: "skipped", reason: "unresolved_user" };
  }
  if (!(await userExists(userId))) {
    return { outcome: "skipped", userId, reason: "user_not_found" };
  }

  const outcome = await applyEntitlement(
    userId,
    cacheFromSubscription(subscription),
    { id: event.id, created: event.created },
  );
  return { outcome, userId };
}

/** The subscription an event concerns, if any. */
function subscriptionIdFor(event: Stripe.Event): string | null {
  switch (event.type) {
    case "checkout.session.completed": {
      const session = event.data.object;
      if (session.mode !== "subscription") return null;
      return idOf(session.subscription);
    }
    case "customer.subscription.created":
    case "customer.subscription.updated":
    case "customer.subscription.deleted":
      return event.data.object.id;
    case "invoice.paid":
    case "invoice.payment_failed": {
      // `invoice.subscription` was replaced by the `parent` block; an invoice
      // that did not come from a subscription has no parent of that type.
      const invoice = event.data.object;
      const details = invoice.parent?.subscription_details;
      return details ? idOf(details.subscription) : null;
    }
    default:
      return null;
  }
}

function idOf(value: string | { id: string } | null | undefined): string | null {
  if (!value) return null;
  return typeof value === "string" ? value : value.id;
}

/**
 * Resolve the Clerk user an event belongs to.
 *
 * Only metadata this server wrote is consulted, in order of directness:
 * the checkout session's `client_reference_id`, then subscription metadata,
 * then the customer's metadata. Nothing here reads a browser-supplied value,
 * so an attacker who could forge a webhook body still could not point one at
 * another account - and they cannot forge one, because the signature is checked
 * first.
 */
async function resolveUserId(
  stripe: Stripe,
  event: Stripe.Event,
  subscription: Stripe.Subscription,
): Promise<string | null> {
  if (event.type === "checkout.session.completed") {
    const session = event.data.object;
    const fromSession =
      session.client_reference_id ?? session.metadata?.clerkUserId ?? null;
    if (fromSession) return fromSession;
  }

  const fromSubscription = subscription.metadata?.clerkUserId;
  if (fromSubscription) return fromSubscription;

  const customerId = idOf(subscription.customer);
  if (!customerId) return null;
  try {
    const customer = await stripe.customers.retrieve(customerId);
    if (customer.deleted) return null;
    return customer.metadata?.clerkUserId ?? null;
  } catch {
    return null;
  }
}
