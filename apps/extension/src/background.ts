/**
 * The service worker, which exists only to make the venue requests.
 *
 * Content scripts inherit the page's origin, so a fetch to the venue's API from
 * inside kalshi.com is a cross-origin request the page's CSP can refuse. The
 * worker holds the `host_permissions` and is not subject to that, so every
 * network call is funnelled through here and the content script only ever talks
 * to this one message channel.
 *
 * The allowlist below is the security boundary. A content script running on a
 * page the extension does not control could ask this worker to fetch anything;
 * restricting it to the three read-only public endpoints the extension actually
 * uses means the worst a compromised page can do is make us read a price.
 */

const ALLOWED_PREFIXES = [
  "https://api.elections.kalshi.com/trade-api/v2/",
  "https://gamma-api.polymarket.com/",
  "https://clob.polymarket.com/",
];

/** Books move; a short cache stops a size slider from hammering the venue. */
const CACHE_TTL_MS = 10_000;
const cache = new Map<string, { at: number; value: unknown }>();

/**
 * The extension's first-run surface is packaged with it instead of hosted on
 * PMVL. Installation therefore succeeds even if the website is unavailable,
 * and opening the guide reveals no installation event to a PMVL server.
 */
function openOnboarding(): void {
  void chrome.tabs.create({ url: chrome.runtime.getURL("onboarding.html") });
}

chrome.runtime.onInstalled.addListener(({ reason }) => {
  // Updates should stay quiet. An existing user has already crossed the setup
  // path and Chrome may update the package while they are doing something else.
  if (reason === "install") openOnboarding();
});

// The overlay itself is always visible on a supported contract. The toolbar
// icon is a durable way back to setup, trust boundaries, and troubleshooting.
chrome.action.onClicked.addListener(openOnboarding);

function allowed(url: string): boolean {
  return ALLOWED_PREFIXES.some((prefix) => url.startsWith(prefix));
}

async function fetchJson(url: string): Promise<unknown> {
  const hit = cache.get(url);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.value;

  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    credentials: "omit",
  });
  if (!response.ok) throw new Error(`${response.status} for ${url}`);
  const value = await response.json();
  cache.set(url, { at: Date.now(), value });
  // Bounded so a long session on a busy board cannot grow this without limit.
  if (cache.size > 200) cache.delete(cache.keys().next().value as string);
  return value;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Defense in depth: only this extension's own contexts may ask the worker to
  // fetch. The manifest declares no `externally_connectable`, so a page script
  // cannot message here today - but the boundary should not depend on the
  // manifest staying that way, and a compromised content script from another
  // extension must not gain a proxy through this one.
  if (sender?.id !== chrome.runtime.id) {
    sendResponse({ ok: false, error: "blocked: sender is not this extension" });
    return false;
  }

  if (message?.type !== "pmvl:fetch" || typeof message.url !== "string") return false;

  if (!allowed(message.url)) {
    sendResponse({ ok: false, error: "blocked: not a permitted endpoint" });
    return false;
  }

  fetchJson(message.url)
    .then((value) => sendResponse({ ok: true, value }))
    .catch((error: unknown) => {
      sendResponse({ ok: false, error: String(error) });
    });
  // Keeps the message channel open for the async reply.
  return true;
});

// The service worker is a module (manifest v3 type: module), and bundling it
// keeps this file importable by the boundary tests.
export {};
