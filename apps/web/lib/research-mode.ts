export type ResearchMode = "live" | "demo";

/**
 * Only the synthetic demo is URL-addressable. Every missing or unrecognised
 * value resolves to live data so a forged query cannot invent a third mode.
 */
export function normalizeResearchMode(mode: unknown): ResearchMode {
  return mode === "demo" ? "demo" : "live";
}

/**
 * Carry the validated research mode onto an internal URL.
 *
 * Live is the default and stays out of the address bar. Demo is explicit because
 * dropping it on a deep link would silently change the dataset underneath the
 * reader. Existing filters and fragments are preserved.
 */
export function withResearchMode(href: string, mode: unknown): string {
  const hashAt = href.indexOf("#");
  const hash = hashAt === -1 ? "" : href.slice(hashAt);
  const withoutHash = hashAt === -1 ? href : href.slice(0, hashAt);
  const queryAt = withoutHash.indexOf("?");
  const pathname = queryAt === -1 ? withoutHash : withoutHash.slice(0, queryAt);
  const search = new URLSearchParams(
    queryAt === -1 ? "" : withoutHash.slice(queryAt + 1),
  );

  if (normalizeResearchMode(mode) === "demo") {
    search.set("mode", "demo");
  } else {
    search.delete("mode");
  }

  const query = search.toString();
  return `${pathname}${query ? `?${query}` : ""}${hash}`;
}
