/**
 * Site-level constants shared by metadata, canonical URLs, the sitemap and the
 * Stripe redirect allowlist.
 *
 * `SITE_URL` is the one place a deployment's public origin is decided. It must be
 * an absolute origin with no trailing slash, because it is concatenated with
 * paths in `metadataBase`, `sitemap.ts` and - most importantly - in the checkout
 * success/cancel URLs, where a mistake becomes an open redirect rather than a
 * cosmetic bug.
 */

function normaliseOrigin(raw: string | undefined): string | null {
  if (!raw) return null;
  const withScheme = /^https?:\/\//.test(raw) ? raw : `https://${raw}`;
  try {
    const url = new URL(withScheme);
    // Only http/https, and only an origin: a path, query or fragment smuggled
    // into this variable would end up prefixing every redirect target.
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return url.origin;
  } catch {
    return null;
  }
}

export const SITE_NAME = "PMVL";

export const SITE_LONG_NAME = "Prediction Market Value Lab";

/**
 * Resolution order:
 *
 * 1. `NEXT_PUBLIC_SITE_URL` - set this on any deployment with a real domain.
 * 2. `VERCEL_PROJECT_PRODUCTION_URL` - the project's stable production host,
 *    which is the same on every Preview, so a Preview build does not bake its
 *    own ephemeral hostname into canonical tags.
 * 3. localhost, for development.
 *
 * `VERCEL_URL` is deliberately NOT used for metadata: it is the per-deployment
 * hostname, and publishing it as a canonical URL leaks a deployment-specific
 * address into search results.
 */
export const SITE_URL: string =
  normaliseOrigin(process.env.NEXT_PUBLIC_SITE_URL) ??
  normaliseOrigin(process.env.VERCEL_PROJECT_PRODUCTION_URL) ??
  "http://localhost:3000";

/** Absolute URL for a site-relative path. */
export function absoluteUrl(path: string): string {
  return `${SITE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

/** Placeholder contact. Replaced once the owner confirms a monitored address. */
export const SUPPORT_EMAIL_PLACEHOLDER = "[SUPPORT EMAIL — OWNER INPUT REQUIRED]";

/** Placeholder legal entity. Never invented; see docs/legal-placeholders.md. */
export const LEGAL_ENTITY_PLACEHOLDER = "[LEGAL ENTITY NAME — OWNER INPUT REQUIRED]";

/** Placeholder jurisdiction. Never invented; see docs/legal-placeholders.md. */
export const JURISDICTION_PLACEHOLDER =
  "[GOVERNING JURISDICTION — OWNER INPUT REQUIRED]";
