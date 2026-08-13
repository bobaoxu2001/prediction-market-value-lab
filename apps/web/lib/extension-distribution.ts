const CHROME_STORE_ORIGIN = "https://chromewebstore.google.com";
const CHROME_EXTENSION_ID = /^[a-p]{32}$/;

/**
 * Return a publishable Chrome Web Store listing URL, or keep the site on its
 * honest developer-mode fallback.
 *
 * The store assigns the extension ID only after the owner creates a listing, so
 * the URL cannot safely live in source yet. A malformed or unrelated URL must
 * never turn the primary install button into an open redirect.
 */
export function chromeWebStoreUrl(
  raw: string | undefined = process.env.NEXT_PUBLIC_CHROME_EXTENSION_ID,
): string | null {
  if (!raw) return null;

  const id = raw.trim();
  if (!CHROME_EXTENSION_ID.test(id)) return null;
  return `${CHROME_STORE_ORIGIN}/detail/pmvl-entry-cost/${id}`;
}
