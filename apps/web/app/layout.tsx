import type { Metadata } from "next";
import { apiGet } from "@/lib/api";
import { SnapshotBanner } from "@/components/ui";
import { ModeNav, ModeSwitch } from "@/components/mode-nav";
import { Suspense } from "react";
import Link from "next/link";
import "./globals.css";
import { ThemeToggle } from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Prediction Market Value Lab",
  description:
    "Read-only research platform scanning Kalshi and Polymarket for executable value and arbitrage. Not investment advice.",
};

const NAV = [
  { href: "/", label: "Today" },
  { href: "/arbitrage", label: "Arbitrage" },
  { href: "/markets", label: "Markets" },
  { href: "/backtest", label: "Backtest" },
  { href: "/track-record", label: "Track record" },
  { href: "/methodology", label: "Methodology" },
  { href: "/system", label: "System" },
];

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Probed once per request, above the header, because the mode switch needs to know
  // whether this deployment serves a frozen snapshot before it can name the mode.
  // A failure here must never blank the site.
  let snapshotActive = false;
  let latestQuoteAt: string | null = null;
  let arbitrageScanAt: string | null = null;
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
  } catch {
    // leave defaults
  }

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Applied before paint so the page never flashes the wrong theme. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('pmvl-theme');var d=window.matchMedia('(prefers-color-scheme: dark)').matches;if(t==='dark'||(!t&&d))document.documentElement.classList.add('dark');}catch(e){}})();`,
          }}
        />
      </head>
      <body>
        {/* Keyboard users should not have to tab the whole nav to reach the data. */}
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-[2px] focus:bg-accent focus:px-3 focus:py-2 focus:text-sm focus:text-accent-ink"
        >
          Skip to content
        </a>
        <div className="min-h-screen">
          {/* Opaque, not translucent: a backdrop-blur header puts shifting
              contrast behind the one element that must stay legible while dense
              numeric rows scroll under it. */}
          <header className="sticky top-0 z-20 border-b border-line bg-base">
            <div className="mx-auto max-w-7xl px-4">
              <div className="flex h-14 items-center justify-between gap-4">
                <Link href="/" className="flex shrink-0 items-center gap-2">
                  <span className="font-mono text-sm font-bold tracking-tight">
                    PMVL
                  </span>
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
              latestQuoteAt={latestQuoteAt}
              arbitrageScanAt={arbitrageScanAt}
            />
            {children}
          </main>

          <footer className="mt-12 border-t border-line py-6 text-xs text-ink-faint">
            <div className="mx-auto max-w-7xl space-y-2 px-4">
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
            </div>
          </footer>
        </div>
      </body>
    </html>
  );
}
