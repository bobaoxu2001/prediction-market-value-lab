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

/** Absolute URL for a site-relative path. Canonical/metadata origin. */
export function absoluteUrl(path: string): string {
  return `${SITE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * The origin a browser should be sent back to after leaving this deployment.
 *
 * Deliberately NOT `SITE_URL`. Those two want opposite things:
 *
 *   - `SITE_URL` is the site's *published identity*. It must be the production
 *     host even when a Preview renders the page, or every Preview would publish
 *     canonical tags and a sitemap pointing at its own ephemeral hostname.
 *   - A Stripe `success_url` is a *round trip back to the deployment the visitor
 *     is actually using*. Building it from the production host means completing
 *     a test checkout on a Preview lands the visitor on production - a different
 *     deployment, with billing disabled and no knowledge of the subscription
 *     they just created. The flow looks broken precisely when someone is trying
 *     to verify that it works.
 *
 * Resolution order:
 *
 *   1. `NEXT_PUBLIC_SITE_URL` - an explicit origin always wins.
 *   2. On Preview, the branch alias (`VERCEL_BRANCH_URL`), which is stable
 *      across builds of the same branch, then the per-deployment `VERCEL_URL`.
 *   3. Otherwise `SITE_URL`.
 *
 * Every branch goes through `normaliseOrigin`, so the result is always a bare
 * http(s) origin. That property is what `lib/billing/urls.ts` relies on to
 * guarantee its redirects cannot leave this site.
 */
export function deploymentOrigin(): string {
  const explicit = normaliseOrigin(process.env.NEXT_PUBLIC_SITE_URL);
  if (explicit) return explicit;

  if (process.env.VERCEL_ENV === "preview") {
    const preview =
      normaliseOrigin(process.env.VERCEL_BRANCH_URL) ??
      normaliseOrigin(process.env.VERCEL_URL);
    if (preview) return preview;
  }

  return SITE_URL;
}

/**
 * The contact address, confirmed by the owner and approved for the public Beta.
 *
 * One constant rather than a literal repeated across the footer, the terms and
 * the privacy notice: a contact address that is wrong in one of three places is
 * worse than one that is missing, because a reader has no way to tell which
 * copy is current.
 *
 * This is the *only* legal-page value that has been resolved. Everything else -
 * legal entity, registered address, jurisdiction, dispute venue, refund policy,
 * retention policy, liability cap, minimum age, prices, currency - is still an
 * unresolved `<Placeholder>` and must stay visibly marked. Approving a support
 * address is not approving the documents.
 */
export const SUPPORT_EMAIL = "ax2183@nyu.edu";
