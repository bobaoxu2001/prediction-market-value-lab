/**
 * A watchlist that lives in the browser.
 *
 * The API is read-only by construction — no write endpoint, no credential intake
 * — and that property is worth more than a synced watchlist. So this one is
 * stored in `localStorage`: it needs no account, no server state, and no new
 * attack surface, and it cannot leak anything because nothing leaves the device.
 *
 * The honest cost of that choice is that the list is per-browser and does not
 * follow the reader anywhere. Every surface that renders it says so; a watchlist
 * that silently fails to appear on a second device is a worse outcome than one
 * that told you it wouldn't.
 */

export interface WatchedContract {
  marketId: number;
  title: string;
  platform: string;
  /** Which side and size the reader was pricing, so it can be restored exactly. */
  side: "yes" | "no";
  size: string;
  addedAt: string;
}

/**
 * Versioned, so a future shape change can be detected and discarded rather than
 * crashing on a value written by an older build.
 */
const KEY = "pmvl.watchlist.v1";

/**
 * A cap, because `localStorage` is a shared, synchronous, ~5MB budget for the
 * whole origin. An unbounded list grows until an unrelated write starts throwing.
 */
const MAX_ITEMS = 100;

function isBrowser(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined";
}

/** Identity of a watch entry: one contract can be watched on each side. */
export function watchKey(marketId: number, side: string): string {
  return `${marketId}:${side}`;
}

function isValid(value: unknown): value is WatchedContract {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.marketId === "number" &&
    Number.isFinite(item.marketId) &&
    typeof item.title === "string" &&
    typeof item.platform === "string" &&
    (item.side === "yes" || item.side === "no") &&
    typeof item.size === "string"
  );
}

/**
 * Cached parse of the stored list.
 *
 * `useSyncExternalStore` compares snapshots by reference and re-renders whenever
 * it gets a new one, so a `getSnapshot` that parsed the JSON afresh each call
 * would return a new array every time and spin forever. The cache is invalidated
 * on every write and on the `storage` event, which is the only other way the
 * value changes.
 */
let cache: WatchedContract[] | null = null;

/** Stable identity for the server and pre-hydration snapshots. */
const EMPTY: WatchedContract[] = [];

const listeners = new Set<() => void>();

function invalidate(): void {
  cache = null;
  for (const listener of listeners) listener();
}

export function readWatchlist(): WatchedContract[] {
  if (!isBrowser()) return EMPTY;
  if (cache !== null) return cache;
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return (cache = EMPTY);
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return (cache = EMPTY);
    // Anything malformed is dropped rather than rendered. This store is writable
    // by any script on the origin and by the user's own devtools, so its contents
    // are input, not state we can assume we wrote.
    return (cache = parsed.filter(isValid).slice(0, MAX_ITEMS));
  } catch {
    return (cache = EMPTY);
  }
}

/**
 * `useSyncExternalStore` bindings.
 *
 * Using the external-store hook rather than `useEffect` + `useState` is what
 * makes the pre-hydration render correct by construction: `getServerSnapshot`
 * returns the empty list, so the server HTML and the first client pass agree,
 * and React swaps in the real value after hydration without a mismatch.
 */
export function subscribeToWatchlist(listener: () => void): () => void {
  listeners.add(listener);
  // Another tab writing the same key fires `storage` here. Keeping two open tabs
  // consistent costs one line and is free correctness.
  const onStorage = (event: StorageEvent) => {
    if (event.key === KEY || event.key === null) invalidate();
  };
  if (isBrowser()) window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    if (isBrowser()) window.removeEventListener("storage", onStorage);
  };
}

export const getWatchlistSnapshot = readWatchlist;

export function getWatchlistServerSnapshot(): WatchedContract[] {
  return EMPTY;
}

function write(items: WatchedContract[]): WatchedContract[] {
  if (!isBrowser()) return items;
  const capped = items.slice(0, MAX_ITEMS);
  try {
    window.localStorage.setItem(KEY, JSON.stringify(capped));
  } catch {
    // Quota exceeded, or storage disabled (private mode, or a blocked origin).
    // A failed save must not take down the page the button sits on.
  }
  // Notified even when the write threw: the in-memory list is what the UI is
  // about to render, and leaving subscribers on a stale snapshot would show a
  // button that disagrees with itself.
  invalidate();
  return capped;
}

export function isWatched(marketId: number, side: string): boolean {
  const key = watchKey(marketId, side);
  return readWatchlist().some((item) => watchKey(item.marketId, item.side) === key);
}

/** Adds if absent, removes if present. Returns the new list. */
export function toggleWatch(
  entry: Omit<WatchedContract, "addedAt">,
): WatchedContract[] {
  const key = watchKey(entry.marketId, entry.side);
  const current = readWatchlist();
  const without = current.filter(
    (item) => watchKey(item.marketId, item.side) !== key,
  );
  if (without.length !== current.length) return write(without);
  // Newest first: the list is read top-down and the thing just added is the
  // thing most likely to be wanted.
  return write([{ ...entry, addedAt: new Date().toISOString() }, ...without]);
}

export function removeWatch(marketId: number, side: string): WatchedContract[] {
  const key = watchKey(marketId, side);
  return write(
    readWatchlist().filter((item) => watchKey(item.marketId, item.side) !== key),
  );
}

export function clearWatchlist(): WatchedContract[] {
  return write([]);
}
