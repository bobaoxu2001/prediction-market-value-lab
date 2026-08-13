/**
 * Per-test environment hygiene.
 *
 * Every billing test asserts against a specific configuration, so the process
 * must start each one with a known-empty billing environment rather than
 * whatever the developer happens to have exported. In particular a real
 * STRIPE_SECRET_KEY leaking in from a shell would make the "billing is disabled
 * by default" test pass for the wrong reason.
 */
import { afterEach, beforeEach } from "vitest";

const MANAGED = [
  "BILLING_MODE",
  "NEXT_PUBLIC_BILLING_ENABLED",
  "STRIPE_SECRET_KEY",
  "STRIPE_WEBHOOK_SECRET",
  "STRIPE_PRO_MONTHLY_PRICE_ID",
  "STRIPE_PRO_ANNUAL_PRICE_ID",
  "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
  "CLERK_SECRET_KEY",
  "NEXT_PUBLIC_SITE_URL",
  "NEXT_PUBLIC_CHROME_EXTENSION_ID",
  "VERCEL_PROJECT_PRODUCTION_URL",
  "VERCEL_ENV",
  "VERCEL_BRANCH_URL",
  "VERCEL_URL",
];

beforeEach(() => {
  for (const key of MANAGED) delete process.env[key];
});

afterEach(() => {
  for (const key of MANAGED) delete process.env[key];
});
