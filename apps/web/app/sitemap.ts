import type { MetadataRoute } from "next";

import { absoluteUrl } from "@/lib/site";

/**
 * The sitemap lists only pages that are public, stable and useful to index.
 *
 * Excluded on purpose: `/account*` and `/sign-in` / `/sign-up`, which are either
 * private or worthless as search results; `/market/[id]`, because the snapshot's
 * two thousand market pages change identity between publications and would fill
 * an index with URLs that later resolve to a different contract; and every
 * `?mode=demo` variant, which would put synthetic data in search results; and
 * `/watchlist`, which renders one browser's local storage and is therefore
 * empty for every visitor who did not create it.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const entries: Array<{ path: string; priority: number; changeFrequency: "daily" | "weekly" | "monthly" }> = [
    { path: "/", priority: 1, changeFrequency: "weekly" },
    { path: "/extension", priority: 0.95, changeFrequency: "weekly" },
    { path: "/app", priority: 0.9, changeFrequency: "daily" },
    // High priority: it is the surface that answers on every visit, and the one
    // whose subject ("what can entry cost at this size") people search for.
    { path: "/cost", priority: 0.9, changeFrequency: "daily" },
    { path: "/pricing", priority: 0.8, changeFrequency: "monthly" },
    { path: "/founding-pilot", priority: 0.8, changeFrequency: "monthly" },
    { path: "/markets", priority: 0.7, changeFrequency: "daily" },
    { path: "/arbitrage", priority: 0.7, changeFrequency: "daily" },
    { path: "/backtest", priority: 0.6, changeFrequency: "weekly" },
    { path: "/track-record", priority: 0.6, changeFrequency: "weekly" },
    { path: "/methodology", priority: 0.7, changeFrequency: "monthly" },
    { path: "/system", priority: 0.5, changeFrequency: "daily" },
    { path: "/risk-disclosure", priority: 0.5, changeFrequency: "monthly" },
    { path: "/terms", priority: 0.4, changeFrequency: "monthly" },
    { path: "/privacy", priority: 0.4, changeFrequency: "monthly" },
  ];

  const lastModified = new Date();
  return entries.map(({ path, priority, changeFrequency }) => ({
    url: absoluteUrl(path),
    lastModified,
    changeFrequency,
    priority,
  }));
}
