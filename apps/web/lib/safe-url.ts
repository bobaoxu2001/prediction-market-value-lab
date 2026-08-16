/**
 * The one place that decides which external URLs may become clickable links.
 *
 * Data-derived URLs reach <a href> in two places today: research evidence links
 * (third-party scraped content) and data-source docs on /system. A string that
 * arrives from an API must never become an executable scheme - href="javascript:"
 * runs when clicked, and React's own filtering does not reliably cover
 * server-rendered anchors. This helper allows only http(s) and returns null for
 * everything else, so the caller renders a link only when a safe URL exists.
 *
 * Deliberately separate from the origin-specific validators in lib/pilot.ts and
 * lib/extension-distribution.ts: those guard against open redirects to a known
 * good host; this one guards against executable schemes for arbitrary external
 * references. Both kinds of check belong where they are used.
 */
export function safeExternalUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return null;
  }
  return url.toString();
}
