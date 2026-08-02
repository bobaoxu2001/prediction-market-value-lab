import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { localTime } from "@/lib/format";
import { shouldShowCheckoutUi } from "@/lib/billing/config";
import {
  ENTITLEMENT_EXPLANATIONS,
  ENTITLEMENT_LABELS,
  getCurrentEntitlement,
} from "@/lib/billing/entitlement";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Billing",
  robots: { index: false, follow: false },
};

export default async function BillingPage({
  searchParams,
}: {
  searchParams: Promise<{ checkout?: string; portal?: string }>;
}) {
  const [entitlement, params] = await Promise.all([
    getCurrentEntitlement(),
    searchParams,
  ]);

  if (!entitlement.signedIn || !entitlement.user) redirect("/sign-in");

  const checkoutUi = shouldShowCheckoutUi();
  const { state } = entitlement;

  return (
    <div className="mx-auto max-w-3xl px-4 pb-16 pt-14">
      <p className="t-label">Account</p>
      <h1 className="t-page-title mt-2">Billing</h1>
      <p className="t-prose mt-3">
        Subscription status as Stripe last confirmed it. This page never decides
        your entitlement itself — it reports what a verified Stripe webhook
        wrote.
      </p>

      <CheckoutReturnNotice
        checkout={params.checkout}
        portal={params.portal}
        isPro={entitlement.isPro}
      />

      {entitlement.billingDisabled ? (
        <BillingDisabledPanel />
      ) : (
        <>
          <dl className="mt-8 divide-y divide-line-subtle border-y border-line">
            <Row label="Plan">
              <span className="font-medium">{ENTITLEMENT_LABELS[state]}</span>
              <p className="t-meta mt-2">{ENTITLEMENT_EXPLANATIONS[state]}</p>
            </Row>
            <Row label="Renews or ends">
              {entitlement.currentPeriodEnd ? (
                <>
                  {localTime(new Date(entitlement.currentPeriodEnd * 1000).toISOString())}
                  <p className="t-meta mt-1">
                    {entitlement.cancelAtPeriodEnd
                      ? "Cancellation is scheduled; access ends at this time."
                      : "The subscription renews at this time unless cancelled."}
                  </p>
                </>
              ) : (
                <span className="text-ink-faint">not applicable</span>
              )}
            </Row>
            <Row label="Stripe customer">
              {entitlement.stripeCustomerId ? (
                <span className="num break-all text-ink-muted">
                  {entitlement.stripeCustomerId}
                </span>
              ) : (
                <span className="text-ink-faint">none yet</span>
              )}
            </Row>
            <Row label="Research access">
              <span className="chip bg-edge/15 text-edge">Full — public</span>
              <p className="t-meta mt-2">
                Unaffected by billing state, including a failed payment.
              </p>
            </Row>
          </dl>

          <div className="mt-8 flex flex-wrap items-center gap-3">
            {entitlement.stripeCustomerId ? (
              <form action="/api/billing/portal" method="post">
                <input type="hidden" name="returnTo" value="/account/billing" />
                <button type="submit" className="btn-primary">
                  Open billing portal
                </button>
              </form>
            ) : null}
            {checkoutUi && !entitlement.isPro ? (
              <>
                <form action="/api/billing/checkout" method="post">
                  <input type="hidden" name="plan" value="pro_monthly" />
                  <input type="hidden" name="returnTo" value="/account/billing" />
                  <button type="submit" className="btn-quiet">
                    Start monthly test checkout
                  </button>
                </form>
                <form action="/api/billing/checkout" method="post">
                  <input type="hidden" name="plan" value="pro_annual" />
                  <input type="hidden" name="returnTo" value="/account/billing" />
                  <button type="submit" className="btn-quiet">
                    Start annual test checkout
                  </button>
                </form>
              </>
            ) : null}
            <Link href="/pricing" className="btn-quiet">
              Compare plans
            </Link>
          </div>

          <p className="mt-4 t-meta">
            Cancellation, payment-method changes and invoices are handled in
            Stripe&apos;s customer portal. Cancelling stops the next renewal;
            access continues until the end of the period already paid for.
          </p>
        </>
      )}

      <p className="mt-10 border-t border-line pt-6 t-meta">
        PMVL stores a Stripe customer ID, a subscription ID, the subscription
        status, the price ID, the period end and whether a cancellation is
        scheduled. It never receives or stores card details. See the{" "}
        <Link href="/privacy" className="underline underline-offset-2">
          privacy notice
        </Link>
        , the{" "}
        <Link href="/terms" className="underline underline-offset-2">
          terms
        </Link>{" "}
        and the{" "}
        <Link href="/risk-disclosure" className="underline underline-offset-2">
          risk disclosure
        </Link>
        .
      </p>
    </div>
  );
}

function BillingDisabledPanel() {
  return (
    <div className="mt-8 panel p-6">
      <h2 className="t-sub-title">Billing is not live on this deployment</h2>
      <p className="t-prose mt-2">
        No subscription can be started here and no payment method is held. The
        server rejects checkout requests regardless of what any interface offers,
        so there is nothing on this page that could charge you by accident.
      </p>
      <p className="t-prose mt-3">
        Public research is unaffected — it has never been gated on billing.
      </p>
      <div className="mt-5 flex flex-wrap items-center gap-3">
        <Link href="/app" className="btn-primary">
          Open research
        </Link>
        <Link href="/pricing" className="btn-quiet">
          See what is planned
        </Link>
      </div>
    </div>
  );
}

/**
 * The post-redirect notice.
 *
 * Note what it does NOT say: that payment succeeded. The browser arriving at a
 * success URL proves only that Stripe redirected it, and a redirect is trivially
 * forged by typing the URL. The entitlement shown on this page comes from the
 * webhook; this notice only explains why the status might not have caught up
 * yet.
 */
function CheckoutReturnNotice({
  checkout,
  portal,
  isPro,
}: {
  checkout?: string;
  portal?: string;
  isPro: boolean;
}) {
  if (checkout === "cancelled") {
    return (
      <p className="mt-6 rounded-[3px] border border-line bg-sunken px-4 py-3 text-sm text-ink-muted">
        Checkout was cancelled. Nothing was charged.
      </p>
    );
  }
  if (checkout === "complete") {
    return (
      <p className="mt-6 rounded-[3px] border border-info/40 bg-info/10 px-4 py-3 text-sm">
        {isPro
          ? "Checkout finished and the subscription has been confirmed by Stripe."
          : "Checkout finished. The subscription is confirmed by a signed webhook from Stripe rather than by this redirect, so the status above may take a moment to update — reload the page."}
      </p>
    );
  }
  if (portal === "return") {
    return (
      <p className="mt-6 rounded-[3px] border border-line bg-sunken px-4 py-3 text-sm text-ink-muted">
        Back from the Stripe billing portal. Any change made there reaches this
        page through a signed webhook and may take a moment to appear.
      </p>
    );
  }
  return null;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 py-4 sm:grid-cols-[12rem_1fr] sm:gap-4">
      <dt className="t-label sm:pt-0.5">{label}</dt>
      <dd className="min-w-0 text-sm text-ink">{children}</dd>
    </div>
  );
}
