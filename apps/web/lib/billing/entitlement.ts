import "server-only";

import type Stripe from "stripe";

import {
  getCurrentUser,
  getCurrentUserId,
  getPrivateMetadata,
  isAuthConfigured,
  mergePrivateMetadata,
  type AuthedUser,
} from "@/lib/auth-server";
import { isBillingEnabled, isPlanId, type PlanId } from "./config";

/**
 * The entitlement layer.
 *
 * One module answers "what is this user allowed to do?", and page components ask
 * it rather than reading Stripe fields themselves. Scattering
 * `status === "active"` through the UI is how a product ends up with three
 * subtly different definitions of "subscribed", one of which is wrong.
 *
 * Source of truth is Stripe. Clerk private metadata is a *cache* of what the
 * webhook last confirmed with Stripe - it is never written from the browser and
 * never trusted beyond what a Stripe event or a Stripe read put there.
 *
 * Everything fails closed. An unreadable cache, a status this code does not
 * recognise, or a deployment with billing switched off all resolve to "not
 * entitled". The one thing that must NOT be gated on any of this is the public
 * research product, which is why no research route calls into here at all.
 */

export const ENTITLEMENT_STATES = [
  "free",
  "pro_trialing",
  "pro_active",
  "pro_past_due",
  "pro_canceling",
  "pro_canceled",
  "unknown",
] as const;

export type EntitlementState = (typeof ENTITLEMENT_STATES)[number];

/** Key under which the billing cache lives in Clerk private metadata. */
export const BILLING_METADATA_KEY = "billing";

export interface BillingCache {
  stripeCustomerId?: string;
  stripeSubscriptionId?: string;
  /** Stripe's own subscription status string, stored verbatim. */
  subscriptionStatus?: string;
  priceId?: string;
  /** Unix seconds. */
  currentPeriodEnd?: number;
  cancelAtPeriodEnd?: boolean;
  /** Idempotency key: the last Stripe event applied to this user. */
  lastStripeEventId?: string;
  /** Ordering key: `event.created`, in unix seconds. */
  lastStripeEventCreated?: number;
}

export interface Entitlement {
  readonly state: EntitlementState;
  readonly signedIn: boolean;
  readonly user: AuthedUser | null;
  /** Whether Pro-only surfaces may be shown. Never true when billing is off. */
  readonly isPro: boolean;
  readonly plan: PlanId | null;
  readonly stripeCustomerId: string | null;
  readonly stripeSubscriptionId: string | null;
  /** Unix seconds, when known. */
  readonly currentPeriodEnd: number | null;
  readonly cancelAtPeriodEnd: boolean;
  /** True when this deployment cannot charge anyone. */
  readonly billingDisabled: boolean;
  /**
   * Whether a subscription already exists that Stripe is still billing.
   *
   * Wider than `isPro`: a past-due subscription grants no access but must still
   * block a second checkout. The pricing page and the checkout route both read
   * this, so the button a visitor sees and the answer the server gives cannot
   * disagree.
   */
  readonly hasLiveSubscription: boolean;
}

const ANONYMOUS: Entitlement = {
  state: "free",
  signedIn: false,
  user: null,
  isPro: false,
  plan: null,
  stripeCustomerId: null,
  stripeSubscriptionId: null,
  currentPeriodEnd: null,
  cancelAtPeriodEnd: false,
  billingDisabled: true,
  hasLiveSubscription: false,
};

/**
 * Map a Stripe subscription status onto an entitlement state.
 *
 * `incomplete` and `incomplete_expired` mean the first payment never succeeded,
 * so they are indistinguishable from never having subscribed: `free`, not a
 * degraded Pro state. `paused` and `unpaid` mean service should stop. A status
 * string this code has never seen resolves to `unknown`, which is not entitled -
 * a new Stripe status must not silently grant access.
 */
export function stateFromStripeStatus(
  status: string | undefined,
  cancelAtPeriodEnd: boolean,
): EntitlementState {
  switch (status) {
    case undefined:
    case "":
    case "incomplete":
    case "incomplete_expired":
      return "free";
    case "trialing":
      return "pro_trialing";
    case "active":
      return cancelAtPeriodEnd ? "pro_canceling" : "pro_active";
    case "past_due":
    case "unpaid":
      return "pro_past_due";
    case "canceled":
    case "paused":
      return "pro_canceled";
    default:
      return "unknown";
  }
}

/** The states that grant Pro access. Past-due and unknown deliberately do not. */
export function stateGrantsPro(state: EntitlementState): boolean {
  return state === "pro_active" || state === "pro_trialing" || state === "pro_canceling";
}

/**
 * Stripe statuses for a subscription that still exists and still bills.
 *
 * Deliberately WIDER than `stateGrantsPro`. Those two questions are different
 * and conflating them is how a customer ends up paying twice:
 *
 *   - "may this user see Pro surfaces?" excludes `past_due`, because a failed
 *     payment should suspend access.
 *   - "does this user already have a subscription?" includes `past_due`,
 *     because the subscription is still there and Stripe is still trying to
 *     collect on it. Letting someone start a second checkout to fix a failed
 *     payment gives them two subscriptions and two invoices; the fix for a
 *     failed payment is the Customer Portal.
 *
 * `incomplete` is excluded on purpose: its first payment never succeeded, Stripe
 * expires it within a day, and a retry is the reasonable thing to allow.
 */
const LIVE_SUBSCRIPTION_STATUSES = new Set([
  "trialing",
  "active",
  "past_due",
  "unpaid",
]);

export function isLiveSubscriptionStatus(status: string | undefined): boolean {
  return status !== undefined && LIVE_SUBSCRIPTION_STATUSES.has(status);
}

function readCache(metadata: Record<string, unknown>): BillingCache {
  const raw = metadata[BILLING_METADATA_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const cache = raw as Record<string, unknown>;
  const str = (key: string): string | undefined =>
    typeof cache[key] === "string" && cache[key] ? (cache[key] as string) : undefined;
  const num = (key: string): number | undefined =>
    typeof cache[key] === "number" && Number.isFinite(cache[key])
      ? (cache[key] as number)
      : undefined;
  return {
    stripeCustomerId: str("stripeCustomerId"),
    stripeSubscriptionId: str("stripeSubscriptionId"),
    subscriptionStatus: str("subscriptionStatus"),
    priceId: str("priceId"),
    currentPeriodEnd: num("currentPeriodEnd"),
    cancelAtPeriodEnd: cache.cancelAtPeriodEnd === true,
    lastStripeEventId: str("lastStripeEventId"),
    lastStripeEventCreated: num("lastStripeEventCreated"),
  };
}

/** Resolve the plan a stored price ID corresponds to, if it is still allowlisted. */
function planForPriceId(priceId: string | undefined): PlanId | null {
  if (!priceId) return null;
  if (priceId === process.env.STRIPE_PRO_MONTHLY_PRICE_ID?.trim()) return "pro_monthly";
  if (priceId === process.env.STRIPE_PRO_ANNUAL_PRICE_ID?.trim()) return "pro_annual";
  return null;
}

/**
 * The canonical entitlement for the current request.
 *
 * The only function a page or route handler should call to find out what the
 * visitor is entitled to.
 */
export async function getCurrentEntitlement(): Promise<Entitlement> {
  if (!isAuthConfigured()) return ANONYMOUS;

  const user = await getCurrentUser();
  if (!user) return ANONYMOUS;

  const billingOff = !isBillingEnabled();
  const base = {
    signedIn: true as const,
    user,
    billingDisabled: billingOff,
  };

  // With billing off there is nothing to be entitled TO, and the cache cannot be
  // reconciled against Stripe. Report the free tier rather than a Pro state this
  // deployment could neither have granted nor could now revoke.
  if (billingOff) {
    return {
      ...base,
      state: "free",
      isPro: false,
      plan: null,
      stripeCustomerId: null,
      stripeSubscriptionId: null,
      currentPeriodEnd: null,
      cancelAtPeriodEnd: false,
      hasLiveSubscription: false,
    };
  }

  const cache = readCache(await getPrivateMetadata(user.id));
  const cancelAtPeriodEnd = cache.cancelAtPeriodEnd === true;
  const state = stateFromStripeStatus(cache.subscriptionStatus, cancelAtPeriodEnd);

  return {
    ...base,
    state,
    isPro: stateGrantsPro(state),
    plan: planForPriceId(cache.priceId),
    stripeCustomerId: cache.stripeCustomerId ?? null,
    stripeSubscriptionId: cache.stripeSubscriptionId ?? null,
    currentPeriodEnd: cache.currentPeriodEnd ?? null,
    cancelAtPeriodEnd,
    hasLiveSubscription: isLiveSubscriptionStatus(cache.subscriptionStatus),
  };
}

/**
 * Assert an active subscription, for a route handler that must not proceed
 * without one. Returns the entitlement, or null when it is not satisfied - the
 * caller decides the status code, because a page and an API route owe the
 * visitor different responses.
 */
export async function requireActiveSubscription(): Promise<Entitlement | null> {
  const entitlement = await getCurrentEntitlement();
  return entitlement.isPro ? entitlement : null;
}

/** The signed-in user's ID, or null. Re-exported so callers need one import. */
export { getCurrentUserId };

export type ApplyOutcome = "applied" | "duplicate" | "stale" | "failed";

/**
 * Write a confirmed billing state into the entitlement cache.
 *
 * Two guards, both required, because Stripe guarantees neither exactly-once nor
 * ordered delivery:
 *
 *   - **Idempotency.** A redelivery of an event already applied is a no-op. The
 *     event ID is stored alongside the state precisely so a replay - whether an
 *     honest Stripe retry or a captured request replayed by an attacker - cannot
 *     move the account.
 *   - **Ordering.** An event created before the last one applied is dropped.
 *     Without this, a delayed `customer.subscription.updated` from before a
 *     cancellation would resurrect a cancelled subscription.
 *
 * The pair is deliberately stored in the same write as the state it justifies,
 * so the cache can never claim to have applied an event whose effect is missing.
 */
export async function applyEntitlement(
  userId: string,
  next: Omit<BillingCache, "lastStripeEventId" | "lastStripeEventCreated">,
  event: { id: string; created: number },
): Promise<ApplyOutcome> {
  const current = readCache(await getPrivateMetadata(userId));

  if (current.lastStripeEventId && current.lastStripeEventId === event.id) {
    return "duplicate";
  }
  if (
    typeof current.lastStripeEventCreated === "number" &&
    event.created < current.lastStripeEventCreated
  ) {
    return "stale";
  }

  const merged: BillingCache = {
    // Keep the customer ID if the incoming event does not restate it: it is the
    // handle used to open the Customer Portal and must survive a partial update.
    ...current,
    ...next,
    lastStripeEventId: event.id,
    lastStripeEventCreated: event.created,
  };

  const ok = await mergePrivateMetadata(userId, { [BILLING_METADATA_KEY]: merged });
  return ok ? "applied" : "failed";
}

/** Store the Stripe customer ID for a user, outside the event-ordering guards. */
export async function rememberStripeCustomerId(
  userId: string,
  stripeCustomerId: string,
): Promise<boolean> {
  const current = readCache(await getPrivateMetadata(userId));
  if (current.stripeCustomerId === stripeCustomerId) return true;
  return mergePrivateMetadata(userId, {
    [BILLING_METADATA_KEY]: { ...current, stripeCustomerId },
  });
}

/** Read the cached billing record for a user. Server-side callers only. */
export async function getBillingCache(userId: string): Promise<BillingCache> {
  return readCache(await getPrivateMetadata(userId));
}

/**
 * Project a Stripe subscription onto the cache shape.
 *
 * `current_period_end` moved off the subscription and onto its items, so the
 * period is the latest item period rather than a top-level field. Taking the
 * max is correct for the multi-item case and identical to reading item zero for
 * the single-price subscriptions this product sells.
 */
export function cacheFromSubscription(
  subscription: Stripe.Subscription,
): Omit<BillingCache, "lastStripeEventId" | "lastStripeEventCreated"> {
  const item = subscription.items?.data ?? [];
  const periodEnds = item
    .map((entry) => entry.current_period_end)
    .filter((value): value is number => typeof value === "number");
  const priceId = item[0]?.price?.id;
  const customer =
    typeof subscription.customer === "string"
      ? subscription.customer
      : subscription.customer?.id;

  return {
    stripeCustomerId: customer,
    stripeSubscriptionId: subscription.id,
    subscriptionStatus: subscription.status,
    priceId,
    currentPeriodEnd: periodEnds.length > 0 ? Math.max(...periodEnds) : undefined,
    cancelAtPeriodEnd: subscription.cancel_at_period_end === true,
  };
}

/** Human-readable label for an entitlement state. Used on the account page. */
export const ENTITLEMENT_LABELS: Readonly<Record<EntitlementState, string>> = {
  free: "Free",
  pro_trialing: "Pro — trial",
  pro_active: "Pro — active",
  pro_past_due: "Pro — payment failed",
  pro_canceling: "Pro — cancels at period end",
  pro_canceled: "Pro — cancelled",
  unknown: "Needs review",
};

/** One sentence explaining what a state means for access. */
export const ENTITLEMENT_EXPLANATIONS: Readonly<Record<EntitlementState, string>> = {
  free: "Public research, market discovery and the full methodology.",
  pro_trialing: "Trial period. Pro surfaces are available until the trial ends.",
  pro_active: "Subscription active. Pro surfaces are available.",
  pro_past_due:
    "The most recent payment failed. Pro access is suspended until a payment succeeds; update the payment method in the billing portal.",
  pro_canceling:
    "Cancellation is scheduled. Pro access continues until the end of the current period.",
  pro_canceled: "Subscription ended. The account is back on the free tier.",
  unknown:
    "The billing record could not be interpreted, so Pro access is withheld. This is a fail-closed state and needs a look, not a retry.",
};

export { isPlanId };
