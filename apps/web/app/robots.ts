import type { MetadataRoute } from "next";

import { absoluteUrl, SITE_URL } from "@/lib/site";

/**
 * Robots rules.
 *
 * Preview deployments are disallowed wholesale. A Preview serves the same
 * content as production from a different hostname, and letting one be indexed
 * splits the site's search identity between two origins - and publishes a
 * deployment URL that was never meant to be public.
 */
export default function robots(): MetadataRoute.Robots {
  const isProductionSite =
    process.env.VERCEL_ENV === "production" || SITE_URL.startsWith("http://localhost");

  if (!isProductionSite) {
    return { rules: [{ userAgent: "*", disallow: "/" }] };
  }

  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        // Private surfaces and synthetic-data variants. `/api/` is listed
        // because a crawler following a form action gains nothing and costs a
        // Stripe API call.
        disallow: ["/account", "/account/", "/api/", "/sign-in", "/sign-up", "/*?mode=demo"],
      },
    ],
    sitemap: absoluteUrl("/sitemap.xml"),
  };
}
