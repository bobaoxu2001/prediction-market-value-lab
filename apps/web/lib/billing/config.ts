import "server-only";

/**
 * The billing gate.
 *
 * The rule this module exists to enforce:
 *
 *     Setting a public UI flag alone must never activate billing.
 *
 * `NEXT_PUBLIC_BILLING_ENABLED` is inlined into the browser bundle and is
 * therefore attacker-controlled in every sense that matters - anyone can edit it
 * out of their own copy of the JavaScript. It decides only whether checkout
 * buttons are rendered. Whether a Checkout Session can actually be created is
 * decided here, on the server, from variables that never reach the client:
 *
 *   - `BILLING_MODE` must be exactly `test`;
 *   - `STRIPE_SECRET_KEY` must be present AND a test key;
 *   - `STRIPE_WEBHOOK_SECRET` must be present, so an entitlement granted at
 *     checkout can actually be confirmed and later revoked;
 *   - both allowlisted price IDs must be present.
 *
 * Any one of those missing means billing is off. There is no configuration of
 * this file that permits a live charge: a `sk_live_` key is rejected rather
 * than honoured, so shipping the wrong secret disables billing instead of
 * enabling real payments.
 */

export const BILLING_MODES = ["disabled", "test"] as const;
export type BillingMode = (typeof BILLING_MODES)[number];

/** Server-side plan identifiers. The client may send only these. */
export const PLAN_IDS = ["pro_monthly", "pro_annual"] as const;
export type PlanId = (typeof PLAN_IDS)[number];

export function isPlanId(value: unknown): value is PlanId {
  return typeof value === "string" && (PLAN_IDS as readonly string[]).includes(value);
}

/**
 * Why billing is off, in words a log line and an owner checklist can both use.
 * Never rendered to an end user - it names which variables are unset.
 */
export type BillingDisabledReason =
  | "billing_mode_not_test"
  | "missing_secret_key"
  | "live_secret_key_rejected"
  | "missing_webhook_secret"
  | "missing_price_ids";

export interface BillingConfig {
  readonly enabled: boolean;
  readonly mode: BillingMode;
  readonly reason: BillingDisabledReason | null;
  readonly secretKey: string | null;
  readonly webhookSecret: string | null;
  readonly prices: Readonly<Record<PlanId, string>> | null;
}

function rawMode(): BillingMode {
  const value = (process.env.BILLING_MODE ?? "disabled").trim().toLowerCase();
  return (BILLING_MODES as readonly string[]).includes(value)
    ? (value as BillingMode)
    : "disabled";
}

/**
 * Resolve the billing configuration for this request.
 *
 * Read fresh every call rather than memoised at module scope: a memoised value
 * computed during a build would outlive an environment-variable change, and the
 * failure mode of a stale "enabled" is a live-adjacent surface nobody asked for.
 */
export function getBillingConfig(): BillingConfig {
  const mode = rawMode();
  const off = (reason: BillingDisabledReason): BillingConfig => ({
    enabled: false,
    mode,
    reason,
    secretKey: null,
    webhookSecret: null,
    prices: null,
  });

  if (mode !== "test") return off("billing_mode_not_test");

  const secretKey = process.env.STRIPE_SECRET_KEY?.trim();
  if (!secretKey) return off("missing_secret_key");

  // The hard stop. A live key here is a mistake, and the safe response to a
  // mistake about money is to do nothing rather than to do it for real.
  if (!secretKey.startsWith("sk_test_") && !secretKey.startsWith("rk_test_")) {
    return off("live_secret_key_rejected");
  }

  const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET?.trim();
  if (!webhookSecret) return off("missing_webhook_secret");

  const monthly = process.env.STRIPE_PRO_MONTHLY_PRICE_ID?.trim();
  const annual = process.env.STRIPE_PRO_ANNUAL_PRICE_ID?.trim();
  if (!monthly || !annual) return off("missing_price_ids");

  return {
    enabled: true,
    mode,
    reason: null,
    secretKey,
    webhookSecret,
    prices: { pro_monthly: monthly, pro_annual: annual },
  };
}

/** The canonical server-side answer to "can this deployment charge anyone?". */
export function isBillingEnabled(): boolean {
  return getBillingConfig().enabled;
}

/**
 * Whether to render checkout affordances.
 *
 * Requires BOTH the public flag and the server gate, so a Preview that has the
 * flag but not the keys shows the honest "early access" state instead of a
 * button that 503s when pressed.
 */
export function shouldShowCheckoutUi(): boolean {
  return process.env.NEXT_PUBLIC_BILLING_ENABLED === "true" && isBillingEnabled();
}

/**
 * Translate an allowlisted plan ID into a Stripe price ID.
 *
 * The only function permitted to produce a price ID. A price arriving from the
 * browser is never consulted: a client that could name its own price could name
 * a $0 one.
 */
export function priceIdForPlan(plan: PlanId): string | null {
  const config = getBillingConfig();
  if (!config.enabled || !config.prices) return null;
  return config.prices[plan] ?? null;
}

export const PLAN_LABELS: Readonly<Record<PlanId, string>> = {
  pro_monthly: "Pro — monthly (test)",
  pro_annual: "Pro — annual (test)",
};
