import type { Metadata } from "next";
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

export default function RootLayout({ children }: { children: React.ReactNode }) {
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
                <nav className="table-wrap flex items-center gap-1 text-sm">
                  {NAV.map((item) => (
                    <Link
                      key={item.href}
                      href={item.href}
                      className="rounded px-2 py-1 text-neutral-600 hover:bg-neutral-100 hover:text-neutral-900 dark:text-neutral-400 dark:hover:bg-neutral-800 dark:hover:text-neutral-100"
                    >
                      {item.label}
                    </Link>
                  ))}
                </nav>
                <ThemeToggle />
              </div>
            </div>
          </header>

          <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>

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
