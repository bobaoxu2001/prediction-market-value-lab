"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";

/**
 * Navigation that carries the current data mode across pages.
 *
 * Without this, opening /backtest?mode=demo and clicking "Track record" landed on
 * /track-record with no mode - silently switching the visitor back to live data,
 * which is usually empty. The result looked like a broken product rather than a
 * deliberate mode change.
 */
export function ModeNav({
  items,
}: {
  items: readonly { href: string; label: string }[];
}) {
  const pathname = usePathname();
  const params = useSearchParams();
  const mode = params.get("mode");

  const withMode = (href: string) =>
    mode && mode !== "live" ? `${href}${href.includes("?") ? "&" : "?"}mode=${mode}` : href;

  return (
    <nav className="table-wrap flex items-center gap-1 text-sm">
      {items.map((item) => {
        const active =
          item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={withMode(item.href)}
            aria-current={active ? "page" : undefined}
            className={
              active
                ? "rounded-[2px] bg-sunken px-2 py-1 font-medium text-ink"
                : "rounded-[2px] px-2 py-1 text-ink-muted hover:bg-sunken hover:text-ink"
            }
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

/**
 * Segmented Live/Demo control. Keeps the current path and every other query
 * parameter, so switching mode never also resets a filter or a horizon tab.
 */
export function ModeSwitch({ snapshot = false }: { snapshot?: boolean }) {
  const pathname = usePathname();
  const params = useSearchParams();
  // The guided demo always serves demo data, so the control must show Demo there
  // even if the URL has not been rewritten yet - otherwise the header contradicts
  // the page it sits above.
  const mode =
    params.get("mode") === "demo" || pathname.startsWith("/demo") ? "demo" : "live";

  const hrefFor = (target: "live" | "demo") => {
    const next = new URLSearchParams(params.toString());
    if (target === "live") next.delete("mode");
    else next.set("mode", target);
    const qs = next.toString();
    return qs ? `${pathname}?${qs}` : pathname;
  };

  return (
    <div className="flex shrink-0 items-center rounded-[3px] border border-line bg-sunken p-0.5 text-xs">
      {(["live", "demo"] as const).map((target) => (
        <Link
          key={target}
          href={hrefFor(target)}
          aria-current={mode === target ? "true" : undefined}
          className={
            mode === target
              ? "rounded-[2px] bg-raised px-2.5 py-1 font-medium text-ink shadow-[0_1px_0_rgb(var(--line))]"
              : "rounded-[2px] px-2.5 py-1 text-ink-faint hover:text-ink"
          }
        >
          {/* Full wording from sm up; abbreviated below, where the long labels
              pushed the nav off screen entirely. The meaning has to survive the
              abbreviation - "Live" vs "Snapshot" is the distinction that matters. */}
          <span className="hidden sm:inline">
            {target === "live"
              ? snapshot
                ? "Research snapshot"
                : "Live pipeline"
              : "Synthetic demo"}
          </span>
          <span className="sm:hidden">
            {target === "live" ? (snapshot ? "Snapshot" : "Live") : "Demo"}
          </span>
        </Link>
      ))}
    </div>
  );
}
