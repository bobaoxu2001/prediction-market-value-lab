"use client";

import { useSyncExternalStore } from "react";

import { trackFunnelEvent } from "@/components/funnel-tracker";
import {
  getWatchlistServerSnapshot,
  getWatchlistSnapshot,
  subscribeToWatchlist,
  toggleWatch,
  watchKey,
  type WatchedContract,
} from "@/lib/watchlist";

/**
 * Add or remove a contract from the browser-local watchlist.
 *
 * Subscribes to the store rather than mirroring it into component state, so the
 * server snapshot (an empty list) matches the first client render and hydration
 * cannot mismatch — and so every WatchButton on a page, plus the watchlist page
 * itself, update together when any one of them is pressed.
 */
export function WatchButton(props: Omit<WatchedContract, "addedAt">) {
  const items = useSyncExternalStore(
    subscribeToWatchlist,
    getWatchlistSnapshot,
    getWatchlistServerSnapshot,
  );
  const key = watchKey(props.marketId, props.side);
  const watched = items.some((item) => watchKey(item.marketId, item.side) === key);

  function onToggle() {
    const next = toggleWatch(props);
    if (!watched && next.some((item) => watchKey(item.marketId, item.side) === key)) {
      trackFunnelEvent("watchlist_added");
    }
  }

  return (
    <button
      type="button"
      className="btn-quiet"
      aria-pressed={watched}
      onClick={onToggle}
    >
      {watched ? "Watching ✓" : "Watch"}
    </button>
  );
}
