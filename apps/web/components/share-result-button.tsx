"use client";

import { useState } from "react";

import { trackFunnelEvent } from "@/components/funnel-tracker";

export interface ShareResultButtonProps {
  contract: string;
  side: "yes" | "no";
  size: string;
  quoted: string;
  estimated: string;
  breakeven: string;
}

/**
 * Share the exact URL-backed result without moving any money arithmetic into the
 * browser. Every value is formatted by the server component that fetched it;
 * this client island only invokes the platform share/copy affordance.
 */
export function ShareResultButton({
  contract,
  side,
  size,
  quoted,
  estimated,
  breakeven,
}: ShareResultButtonProps) {
  const [status, setStatus] = useState("");

  async function share() {
    const url = window.location.href;
    const text =
      `${contract} — buy ${side.toUpperCase()} × ${size}: ` +
      `quoted ${quoted}, estimated entry ${estimated}, break-even ${breakeven}. ` +
      "PMVL research estimate; check the timestamp and assumptions.";

    if (typeof navigator.share === "function") {
      try {
        await navigator.share({ title: `${contract} — PMVL entry cost`, text, url });
        trackFunnelEvent("result_shared", "native_share");
        setStatus("Result shared.");
        return;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          setStatus("");
          return;
        }
        // Sharing can be unsupported for a particular payload even when the API
        // exists. Fall through to a copyable text result in that case.
      }
    }

    try {
      await navigator.clipboard.writeText(`${text}\n${url}`);
      trackFunnelEvent("result_shared", "clipboard");
      setStatus("Result and link copied.");
    } catch {
      setStatus("Could not copy automatically. Copy this page’s address from your browser.");
    }
  }

  return (
    <span className="inline-flex items-center gap-2">
      <button type="button" className="btn-quiet" onClick={share}>
        Share result
      </button>
      <span className="t-meta" role="status" aria-live="polite">
        {status}
      </span>
    </span>
  );
}
