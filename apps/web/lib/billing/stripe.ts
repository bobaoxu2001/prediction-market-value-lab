import "server-only";

import Stripe from "stripe";

import { getBillingConfig } from "./config";

/**
 * The Stripe client.
 *
 * Constructed lazily and only from a configuration that has already passed
 * `getBillingConfig()` - which rejects a live secret key outright. There is no
 * code path in this application that hands Stripe a `sk_live_` key.
 *
 * Cached per secret so a warm serverless instance reuses one HTTP agent, and so
 * a key rotation in the environment produces a new client rather than a stale
 * one.
 */
export const STRIPE_API_VERSION = "2026-07-29.dahlia" satisfies Stripe.LatestApiVersion;

let cached: { key: string; client: Stripe } | null = null;

export function getStripe(): Stripe | null {
  const config = getBillingConfig();
  if (!config.enabled || !config.secretKey) return null;
  if (cached && cached.key === config.secretKey) return cached.client;
  const client = new Stripe(config.secretKey, {
    // Pinned to the version this SDK was generated against. Left unpinned, the
    // payload shape would follow whatever version the Stripe account is set to,
    // and the webhook handler reads specific fields off those payloads.
    apiVersion: STRIPE_API_VERSION,
    appInfo: { name: "prediction-market-value-lab", version: "0.1.0" },
    maxNetworkRetries: 2,
  });
  cached = { key: config.secretKey, client };
  return client;
}

/** Reset the memoised client. Tests only. */
export function __resetStripeForTests(): void {
  cached = null;
}
