import type { Metadata, Viewport } from "next";
import { AuthProvider } from "@/lib/auth";
import { SITE_NAME, SITE_URL } from "@/lib/site";
import "./globals.css";

/**
 * The root layout owns the document and nothing else.
 *
 * Two shells now sit under it - the public site under `(site)` and the research
 * terminal under `(research)` - and each renders its own header, footer and
 * `<main id="main">`. Chrome that used to live here would otherwise have wrapped
 * the marketing homepage in the research terminal's navigation.
 */

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: `${SITE_NAME} — prediction-market research`,
    template: `%s — ${SITE_NAME}`,
  },
  description:
    "Read-only research on Kalshi and Polymarket contracts: independent probability estimates compared with executable prices after fees, liquidity and contract rules. Not investment advice.",
  applicationName: SITE_NAME,
  robots: { index: true, follow: true },
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    url: SITE_URL,
    title: `${SITE_NAME} — prediction-market research`,
    description:
      "Independent probability estimates compared with executable prices, after fees, liquidity, stale quotes and contract rules. Research only — PMVL places no trades.",
  },
  twitter: {
    card: "summary_large_image",
    title: `${SITE_NAME} — prediction-market research`,
    description:
      "Independent probability estimates compared with executable prices, after fees, liquidity and contract rules. Research only.",
  },
};

export const viewport: Viewport = {
  // No `maximum-scale`: capping zoom is the single most common way a site breaks
  // pinch-to-zoom for people who need it.
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
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
          {/* Keyboard users should not have to tab the whole nav to reach the
              content. Both shells expose `<main id="main">`. */}
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-[2px] focus:bg-accent focus:px-3 focus:py-2 focus:text-sm focus:text-accent-ink"
          >
            Skip to content
          </a>
          {children}
        </body>
      </html>
    </AuthProvider>
  );
}
