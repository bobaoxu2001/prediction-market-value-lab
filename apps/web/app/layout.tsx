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
        <div className="min-h-screen">
          <header className="sticky top-0 z-20 border-b border-neutral-200 bg-white/90 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/90">
            <div className="mx-auto max-w-7xl px-4">
              <div className="flex h-14 items-center justify-between gap-4">
                <Link href="/" className="flex shrink-0 items-center gap-2">
                  <span className="font-mono text-sm font-bold tracking-tight">
                    PMVL
                  </span>
                  <span className="hidden text-xs text-neutral-500 sm:inline">
                    Prediction Market Value Lab
                  </span>
                </Link>
                <Suspense fallback={null}>
                  <ModeNav items={NAV} />
                </Suspense>
                <div className="flex items-center gap-2">
                  <Suspense fallback={null}>
                    <ModeSwitch />
                  </Suspense>
                  <ThemeToggle />
                </div>
              </div>
            </div>
          </header>

          <main className="mx-auto max-w-7xl px-4 py-6">{await (async () => {
            // One probe per request; a failure here must never blank the site.
            try {
              const sys = await apiGet<{ snapshot_mode?: boolean; freshest_quote_observed_at?: string | null }>("/system");
              return (
                <SnapshotBanner
                  active={sys?.data?.snapshot_mode}
                  capturedAt={sys?.data?.freshest_quote_observed_at}
                />
              );
            } catch {
              return null;
            }
          })()}
          {children}</main>

          <footer className="mt-12 border-t border-neutral-200 py-6 text-xs text-neutral-500 dark:border-neutral-800">
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
