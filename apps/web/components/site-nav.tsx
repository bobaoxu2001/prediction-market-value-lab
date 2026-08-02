import Link from "next/link";
import { SignOutButton } from "@clerk/nextjs";

import { ThemeToggle } from "@/components/ThemeToggle";
import { isAuthUiConfigured } from "@/lib/auth";
import { SITE_LONG_NAME, SITE_NAME } from "@/lib/site";
import type { Entitlement } from "@/lib/billing/entitlement";

/**
 * The public site's header.
 *
 * Server-rendered against the real session, so the signed-in and signed-out
 * navigations are decided before the HTML leaves the server. A client-side
 * swap would flash "Create free account" at someone who is already signed in,
 * and - worse in the other direction - would put the shape of the account
 * navigation in front of someone who is not.
 *
 * The mobile disclosure is a plain `<details>`. It needs no JavaScript, it is
 * keyboard-operable and screen-reader-announced by the platform, it survives
 * hydration because there is nothing to hydrate, and it cannot widen the
 * document: the panel is absolutely positioned inside a `relative` header that
 * is already constrained to the viewport.
 */

const PUBLIC_LINKS = [
  { href: "/#product", label: "Product" },
  { href: "/app", label: "Research" },
  { href: "/methodology", label: "Methodology" },
  { href: "/pricing", label: "Pricing" },
] as const;

const AUTHED_LINKS = [
  { href: "/app", label: "Open research" },
  { href: "/account", label: "Account" },
  { href: "/account/billing", label: "Manage billing" },
] as const;

export function SiteHeader({ entitlement }: { entitlement: Entitlement }) {
  const signedIn = entitlement.signedIn;
  const links = signedIn ? AUTHED_LINKS : PUBLIC_LINKS;

  return (
    // `sticky` is itself a positioned ancestor, so the mobile panel's `absolute`
    // resolves against this header rather than the document. Adding `relative`
    // here would collide with it, not help it.
    <header className="sticky top-0 z-30 border-b border-line bg-base">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between gap-4 px-4">
        <Link href="/" className="flex min-w-0 shrink items-center gap-2">
          <span className="font-mono text-sm font-bold tracking-tight">{SITE_NAME}</span>
          <span className="hidden truncate text-xs text-ink-faint sm:inline">
            {SITE_LONG_NAME}
          </span>
        </Link>

        <nav aria-label="Primary" className="hidden items-center gap-1 md:flex">
          {links.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="rounded-[2px] px-2 py-1 text-sm text-ink-muted transition-colors hover:bg-sunken hover:text-ink"
            >
              {link.label}
            </Link>
          ))}
        </nav>

        <div className="flex shrink-0 items-center gap-2">
          <div className="hidden items-center gap-2 md:flex">
            {signedIn ? (
              <SignOutButton>
                <button type="button" className="btn-quiet">
                  Sign out
                </button>
              </SignOutButton>
            ) : (
              <>
                <Link href="/sign-in" className="text-sm text-ink-muted hover:text-ink">
                  Sign in
                </Link>
                <Link href="/sign-up" className="btn-primary">
                  Create free account
                </Link>
              </>
            )}
          </div>
          <ThemeToggle />
          <MobileMenu links={links} signedIn={signedIn} />
        </div>
      </div>
    </header>
  );
}

function MobileMenu({
  links,
  signedIn,
}: {
  links: readonly { href: string; label: string }[];
  signedIn: boolean;
}) {
  return (
    <details className="group md:hidden">
      <summary
        className="flex h-8 w-8 cursor-pointer list-none items-center justify-center rounded-[2px] border border-line text-ink-muted hover:text-ink [&::-webkit-details-marker]:hidden"
        aria-label="Open menu"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <path d="M2 4h12M2 8h12M2 12h12" strokeLinecap="round" />
        </svg>
      </summary>
      {/* `right-0` rather than a width: an absolutely positioned panel pinned to
          the right edge cannot push the document wider, which is the failure the
          research pages have hit twice. */}
      <div className="absolute right-3 top-14 w-56 rounded-[3px] border border-line bg-raised p-2 shadow-lg">
        <nav aria-label="Primary (mobile)" className="flex flex-col">
          {links.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="rounded-[2px] px-2 py-2 text-sm text-ink-muted hover:bg-sunken hover:text-ink"
            >
              {link.label}
            </Link>
          ))}
          <div className="mt-2 border-t border-line-subtle pt-2">
            {signedIn ? (
              <SignOutButton>
                <button
                  type="button"
                  className="w-full rounded-[2px] px-2 py-2 text-left text-sm text-ink-muted hover:bg-sunken hover:text-ink"
                >
                  Sign out
                </button>
              </SignOutButton>
            ) : (
              <>
                <Link
                  href="/sign-in"
                  className="block rounded-[2px] px-2 py-2 text-sm text-ink-muted hover:bg-sunken hover:text-ink"
                >
                  Sign in
                </Link>
                <Link
                  href="/sign-up"
                  className="mt-1 block rounded-[2px] bg-accent px-2 py-2 text-sm font-medium text-accent-ink"
                >
                  Create free account
                </Link>
              </>
            )}
          </div>
        </nav>
      </div>
    </details>
  );
}

export function SiteFooter() {
  return (
    <footer className="mt-16 border-t border-line">
      <div className="mx-auto max-w-6xl px-4 py-10">
        <div className="grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
          <FooterColumn
            title="Product"
            links={[
              { href: "/", label: "Overview" },
              { href: "/pricing", label: "Pricing" },
              { href: "/app", label: "Research briefing" },
            ]}
          />
          <FooterColumn
            title="Research"
            links={[
              { href: "/markets", label: "Markets" },
              { href: "/arbitrage", label: "Arbitrage" },
              { href: "/backtest?mode=demo", label: "Backtest" },
              { href: "/track-record", label: "Track record" },
            ]}
          />
          <FooterColumn
            title="Transparency"
            links={[
              { href: "/methodology", label: "Methodology" },
              { href: "/system", label: "System status" },
              { href: "/risk-disclosure", label: "Risk disclosure" },
            ]}
          />
          <FooterColumn
            title="Legal"
            links={[
              { href: "/terms", label: "Terms" },
              { href: "/privacy", label: "Privacy" },
              { href: "/risk-disclosure", label: "Risk disclosure" },
            ]}
          />
        </div>

        <div className="mt-10 space-y-2 border-t border-line pt-6 t-meta">
          <p>
            <strong className="text-ink-muted">
              Research and information only.
            </strong>{" "}
            Not investment, legal, tax or financial advice, not a solicitation,
            and not an offer to trade. {SITE_NAME} is read-only: it holds no
            funds, stores no wallet keys and places no orders. Estimates can be
            wrong, quotes can be stale and liquidity can be insufficient.
            Backtests and demo datasets are not live performance.
          </p>
          <p>
            The hosted deployment serves a frozen research snapshot rather than a
            continuous live scan. Market data comes from the public Kalshi and
            Polymarket APIs. Verify your own eligibility with each venue before
            trading anywhere.
          </p>
          <p>
            Contact:{" "}
            <span className="font-mono">
              [SUPPORT EMAIL — OWNER INPUT REQUIRED]
            </span>
          </p>
          {!isAuthUiConfigured() ? (
            <p className="text-ink-faint">
              Accounts are not enabled on this deployment.
            </p>
          ) : null}
        </div>
      </div>
    </footer>
  );
}

function FooterColumn({
  title,
  links,
}: {
  title: string;
  links: readonly { href: string; label: string }[];
}) {
  return (
    <div>
      <h2 className="t-label">{title}</h2>
      <ul className="mt-3 space-y-2">
        {links.map((link) => (
          <li key={`${title}-${link.href}-${link.label}`}>
            <Link
              href={link.href}
              className="text-sm text-ink-muted hover:text-ink hover:underline"
            >
              {link.label}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
