import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { SignOutButton } from "@clerk/nextjs";

import { localTime } from "@/lib/format";
import {
  ENTITLEMENT_EXPLANATIONS,
  ENTITLEMENT_LABELS,
  getCurrentEntitlement,
} from "@/lib/billing/entitlement";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Account",
  robots: { index: false, follow: false },
};

export default async function AccountPage() {
  const entitlement = await getCurrentEntitlement();

  // A second boundary behind the middleware. The middleware is the gate; this is
  // the assertion that the gate held. Rendering an account page whose `user` is
  // null - which is what a middleware misconfiguration would produce - must not
  // be possible, so the page refuses to render rather than degrading to an empty
  // shell that looks signed-in.
  if (!entitlement.signedIn || !entitlement.user) redirect("/sign-in");

  const { user, state } = entitlement;

  return (
    <div className="mx-auto max-w-3xl px-4 pb-16 pt-14">
      <p className="t-label">Account</p>
      <h1 className="t-page-title mt-2">{user.email ?? "Your account"}</h1>
      <p className="t-prose mt-3">
        What this account is, what it is entitled to, and what it is not. The
        research product is public and is not affected by anything on this page.
      </p>

      <dl className="mt-8 divide-y divide-line-subtle border-y border-line">
        <Row label="Name">{user.name ?? <Unset>not provided</Unset>}</Row>
        <Row label="Email">{user.email ?? <Unset>not available</Unset>}</Row>
        <Row label="Account created">
          {user.createdAt ? (
            localTime(new Date(user.createdAt).toISOString())
          ) : (
            <Unset>not reported</Unset>
          )}
        </Row>
        <Row label="Plan">
          <span className="font-medium">{ENTITLEMENT_LABELS[state]}</span>
        </Row>
        <Row label="Billing state">
          {entitlement.billingDisabled ? (
            <>
              <span className="chip bg-sunken text-ink-muted">
                Billing not yet live
              </span>
              <p className="t-meta mt-2">
                This deployment cannot take a payment. Nothing has been charged
                and no payment method is held.
              </p>
            </>
          ) : (
            <p className="text-sm text-ink-muted">
              {ENTITLEMENT_EXPLANATIONS[state]}
            </p>
          )}
        </Row>
        <Row label="Research access">
          <span className="chip bg-edge/15 text-edge">Full — public</span>
          <p className="t-meta mt-2">
            Every research surface is open to everyone, signed in or not. No part
            of it is gated on this account or on a subscription.
          </p>
        </Row>
      </dl>

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <Link href="/account/billing" className="btn-primary">
          Manage billing
        </Link>
        <Link href="/app" className="btn-quiet">
          Open research
        </Link>
        <SignOutButton redirectUrl="/">
          <button type="button" className="btn-quiet">
            Sign out
          </button>
        </SignOutButton>
      </div>

      <p className="mt-10 border-t border-line pt-6 t-meta">
        This page shows everything PMVL holds about you beyond the billing
        references listed on the{" "}
        <Link href="/account/billing" className="underline underline-offset-2">
          billing page
        </Link>
        . The{" "}
        <Link href="/privacy" className="underline underline-offset-2">
          privacy notice
        </Link>{" "}
        describes each field and who else sees it.
      </p>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 py-4 sm:grid-cols-[12rem_1fr] sm:gap-4">
      <dt className="t-label sm:pt-0.5">{label}</dt>
      <dd className="min-w-0 text-sm text-ink">{children}</dd>
    </div>
  );
}

function Unset({ children }: { children: React.ReactNode }) {
  return <span className="text-ink-faint">{children}</span>;
}
