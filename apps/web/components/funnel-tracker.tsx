"use client";

import { useEffect } from "react";

import {
  validFunnelEvent,
  type FunnelEventName,
  type FunnelPlacement,
} from "@/lib/funnel";

/**
 * Send one first-party aggregate event.
 *
 * No cookie or browser identifier is created and no page, referrer, market,
 * account or order context is included. `keepalive` lets a navigation click
 * finish without holding the visitor on the current page.
 */
export function trackFunnelEvent(
  name: FunnelEventName,
  placement?: FunnelPlacement,
): void {
  const body = JSON.stringify({ name, source: "web", ...(placement ? { placement } : {}) });
  void fetch("/api/funnel", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
    credentials: "omit",
    keepalive: true,
  }).catch(() => {
    // Measurement must never interrupt the product action it observes.
  });
}

/**
 * One delegated click listener covers static server components and future CTA
 * placements. An element opts in with `data-pmvl-funnel`; the event vocabulary
 * remains closed even if a malformed or untrusted attribute reaches the DOM.
 */
export function FunnelTracker() {
  useEffect(() => {
    function onClick(event: MouseEvent) {
      if (!(event.target instanceof Element)) return;
      const element = event.target.closest<HTMLElement>("[data-pmvl-funnel]");
      if (!element) return;

      const name = element.dataset.pmvlFunnel;
      const placement = element.dataset.pmvlPlacement;
      const tracked = validFunnelEvent(name, "web", placement);
      if (!tracked) return;
      trackFunnelEvent(tracked.name, tracked.placement);
    }

    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  return null;
}
