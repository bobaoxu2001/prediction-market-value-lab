import { Suspense } from "react";
import Link from "next/link";
import { apiGet } from "@/lib/api";
import { SnapshotBanner } from "@/components/ui";
import { ModeNav, ModeSwitch } from "@/components/mode-nav";
import { ThemeToggle } from "@/components/ThemeToggle";

/**
 * The research terminal's shell.
 *
 * Lifted verbatim out of the old root layout when the public site was added.
 * The terminal is a merged, reviewed design and this move is meant to change
 * nothing about it: same header, same snapshot banner, same disclaimer footer.
 *
 * Two deliberate additions, both about the funnel rather than the terminal:
 * the wordmark now returns to the public homepage, and the footer carries the
 * legal and pricing links every page on the site is expected to reach. Neither
 * adds an element to the header row, which is where the horizontal-overflow
 * problems on this page have always come from.
 */

const NAV = [
  { href: "/app", label: "Briefing" },
  // Placed second, ahead of the opportunity surfaces. It is the one page that has
  // an answer on every visit: cost needs no probability estimate, so it is not
  // gated on the independence rule that keeps the others empty most days.
  { href: "/cost", label: "Cost" },
  { href: "/arbitrage", label: "Arbitrage" },
  { href: "/markets", label: "Markets" },
  { href: "/watchlist", label: "Watchlist" },
  { href: "/backtest", label: "Backtest" },
  { href: "/track-record", label: "Track record" },
  { href: "/methodology", label: "Methodology" },
  { href: "/system", label: "System" },
];

export default async function ResearchLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Probed once per request, above the header, because the mode switch needs to know
  // whether this deployment serves a frozen snapshot before it can name the mode.
  // A failure here must never blank the site.
  let snapshotActive = false;
  let latestQuoteAt: string | null = null;
  let arbitrageScanAt: string | null = null;
  let staleLiveData = false;
  try {
    const sys = await apiGet<{
      snapshot_mode?: boolean;
      freshest_quote_observed_at?: string | null;
      snapshot_timing?: {
        freshest_quote_observed_at?: string | null;
        arbitrage_scan_at?: string | null;
      } | null;
    }>("/system");
    snapshotActive = Boolean(sys?.data?.snapshot_mode);
    // Prefer the timing block, which names each instant; fall back to the legacy
    // top-level field so an older API still labels the banner correctly.
    latestQuoteAt =
      sys?.data?.snapshot_timing?.freshest_quote_observed_at ??
      sys?.data?.freshest_quote_observed_at ??
      null;
    arbitrageScanAt = sys?.data?.snapshot_timing?.arbitrage_scan_at ?? null;
    const latestMs = latestQuoteAt ? Date.parse(latestQuoteAt) : Number.NaN;
    staleLiveData =
      !snapshotActive &&
      Number.isFinite(latestMs) &&
      Date.now() - latestMs > 30 * 60 * 1000;
  } catch {
    // leave defaults
  }

  return (
    <div className="app-shell min-h-screen">
      {/* Opaque, not translucent: a backdrop-blur header puts shifting
          contrast behind the one element that must stay legible while dense
          numeric rows scroll under it. */}
      <header className="sticky top-0 z-20 border-b border-line bg-base">
        <div className="mx-auto max-w-7xl px-4">
          <div className="flex h-14 items-center justify-between gap-4">
            <Link href="/" className="flex shrink-0 items-center gap-2">
              <span className="font-mono text-sm font-bold tracking-tight">PMVL</span>
              <span className="hidden text-xs text-ink-faint sm:inline">
                Prediction Market Value Lab
              </span>
            </Link>
            <Suspense fallback={null}>
              <ModeNav items={NAV} />
            </Suspense>
            <div className="flex items-center gap-2">
              <Suspense fallback={null}>
                <ModeSwitch snapshot={snapshotActive} />
              </Suspense>
              <ThemeToggle />
            </div>
          </div>
        </div>
      </header>

      <main id="main" className="mx-auto max-w-7xl px-4 py-6">
        <SnapshotBanner
          active={snapshotActive}
          staleLiveData={staleLiveData}
          latestQuoteAt={latestQuoteAt}
          arbitrageScanAt={arbitrageScanAt}
        />
        {children}
      </main>

      <footer className="mt-12 border-t border-line py-6 text-xs text-ink-faint">
        <div className="mx-auto max-w-7xl space-y-3 px-4">
          <p>
            <strong>Research and information only.</strong> Not investment
            advice, not a solicitation, and not an offer to trade. This
            platform is read-only: it holds no funds, stores no wallet keys,
            and places no orders. Simulated performance does not indicate
            future results.
          </p>
          <p>
            Market data from the public Kalshi and Polymarket APIs. Verify
            your own eligibility with each venue before trading anywhere.
          </p>
          <nav aria-label="Site" className="flex flex-wrap gap-x-4 gap-y-1">
            <Link href="/" className="hover:text-ink hover:underline">
              Public site
            </Link>
            <Link href="/pricing" className="hover:text-ink hover:underline">
              Pricing
            </Link>
            <Link href="/risk-disclosure" className="hover:text-ink hover:underline">
              Risk disclosure
            </Link>
            <Link href="/terms" className="hover:text-ink hover:underline">
              Terms
            </Link>
            <Link href="/privacy" className="hover:text-ink hover:underline">
              Privacy
            </Link>
          </nav>
        </div>
      </footer>
    </div>
  );
}
