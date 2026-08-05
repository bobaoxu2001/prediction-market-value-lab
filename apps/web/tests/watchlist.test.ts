/**
 * @vitest-environment jsdom
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The browser-local watchlist.
 *
 * `localStorage` is writable by anything on the origin and by the reader's own
 * devtools, so what comes back out is input rather than state we can assume we
 * wrote. Most of what is pinned here is that assumption never being made.
 */

import {
  clearWatchlist,
  getWatchlistServerSnapshot,
  getWatchlistSnapshot,
  isWatched,
  readWatchlist,
  removeWatch,
  subscribeToWatchlist,
  toggleWatch,
  watchKey,
} from "@/lib/watchlist";

const KEY = "pmvl.watchlist.v1";

const CONTRACT = {
  marketId: 42,
  title: "Will it rain?",
  platform: "kalshi",
  side: "yes" as const,
  size: "100",
};

beforeEach(() => {
  window.localStorage.clear();
  // The module caches its parse; clearing through the public API also
  // invalidates that cache, which a bare `localStorage.clear()` does not.
  clearWatchlist();
});

describe("round trip", () => {
  it("adds, reports and removes", () => {
    expect(readWatchlist()).toEqual([]);

    toggleWatch(CONTRACT);
    expect(isWatched(42, "yes")).toBe(true);
    expect(readWatchlist()).toHaveLength(1);

    toggleWatch(CONTRACT);
    expect(isWatched(42, "yes")).toBe(false);
    expect(readWatchlist()).toEqual([]);
  });

  it("treats the two sides of one contract as separate entries", () => {
    // Buying YES and buying NO are different trades with different costs, so
    // watching one must not silently watch or unwatch the other.
    toggleWatch(CONTRACT);
    toggleWatch({ ...CONTRACT, side: "no" });

    expect(isWatched(42, "yes")).toBe(true);
    expect(isWatched(42, "no")).toBe(true);
    expect(readWatchlist()).toHaveLength(2);

    removeWatch(42, "no");
    expect(isWatched(42, "yes")).toBe(true);
    expect(isWatched(42, "no")).toBe(false);
  });

  it("keeps the newest entry first", () => {
    toggleWatch(CONTRACT);
    toggleWatch({ ...CONTRACT, marketId: 43, title: "Another" });
    expect(readWatchlist()[0].marketId).toBe(43);
  });

  it("preserves the size and side that were being priced", () => {
    // The premium a reader saved is the premium at *their* size; restoring the
    // row at a default size would show a different number than the one that
    // made them save it.
    toggleWatch({ ...CONTRACT, size: "7", side: "no" });
    const [entry] = readWatchlist();
    expect(entry.size).toBe("7");
    expect(entry.side).toBe("no");
  });
});

describe("hostile and damaged storage", () => {
  it("ignores a value that is not an array", () => {
    window.localStorage.setItem(KEY, JSON.stringify({ marketId: 1 }));
    clearWatchlist();
    window.localStorage.setItem(KEY, JSON.stringify({ marketId: 1 }));
    expect(readWatchlist()).toEqual([]);
  });

  it("ignores unparseable JSON rather than throwing", () => {
    window.localStorage.setItem(KEY, "{not json");
    expect(() => readWatchlist()).not.toThrow();
    expect(readWatchlist()).toEqual([]);
  });

  it("drops malformed entries but keeps valid ones", () => {
    window.localStorage.setItem(
      KEY,
      JSON.stringify([
        CONTRACT,
        { marketId: "not a number", title: "x", platform: "kalshi", side: "yes", size: "1" },
        { marketId: 9, title: "no side", platform: "kalshi", size: "1" },
        { ...CONTRACT, marketId: 10, side: "maybe" },
      ]),
    );
    const items = readWatchlist();
    expect(items).toHaveLength(1);
    expect(items[0].marketId).toBe(42);
  });

  it("caps the list so one origin cannot exhaust the storage budget", () => {
    const many = Array.from({ length: 250 }, (_, index) => ({
      ...CONTRACT,
      marketId: index,
    }));
    window.localStorage.setItem(KEY, JSON.stringify(many));
    expect(readWatchlist().length).toBeLessThanOrEqual(100);
  });
});

describe("external store bindings", () => {
  it("returns a stable reference when nothing changed", () => {
    // `useSyncExternalStore` compares snapshots by identity; a fresh array each
    // call would re-render forever.
    toggleWatch(CONTRACT);
    expect(getWatchlistSnapshot()).toBe(getWatchlistSnapshot());
  });

  it("returns a new reference after a write", () => {
    const before = getWatchlistSnapshot();
    toggleWatch(CONTRACT);
    expect(getWatchlistSnapshot()).not.toBe(before);
  });

  it("gives the server an empty, stable snapshot", () => {
    // The server cannot see localStorage. Returning anything else would make the
    // server HTML disagree with the first client render.
    toggleWatch(CONTRACT);
    expect(getWatchlistServerSnapshot()).toEqual([]);
    expect(getWatchlistServerSnapshot()).toBe(getWatchlistServerSnapshot());
  });

  it("notifies subscribers on write and stops after unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeToWatchlist(listener);

    toggleWatch(CONTRACT);
    expect(listener).toHaveBeenCalled();

    unsubscribe();
    listener.mockClear();
    toggleWatch({ ...CONTRACT, marketId: 99 });
    expect(listener).not.toHaveBeenCalled();
  });

  it("picks up a write from another tab", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeToWatchlist(listener);

    window.localStorage.setItem(KEY, JSON.stringify([CONTRACT]));
    window.dispatchEvent(new StorageEvent("storage", { key: KEY }));

    expect(listener).toHaveBeenCalled();
    expect(readWatchlist()).toHaveLength(1);
    unsubscribe();
  });
});

describe("watchKey", () => {
  it("distinguishes sides and contracts", () => {
    expect(watchKey(1, "yes")).not.toBe(watchKey(1, "no"));
    expect(watchKey(1, "yes")).not.toBe(watchKey(2, "yes"));
    expect(watchKey(1, "yes")).toBe(watchKey(1, "yes"));
  });
});
