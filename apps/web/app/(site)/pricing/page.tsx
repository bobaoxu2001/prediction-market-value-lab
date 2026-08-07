import type { Metadata } from "next";
import Link from "next/link";

import { Faq, Section, SectionHeading } from "@/components/marketing";
import { PricingPlans } from "@/components/pricing";
import { getCurrentEntitlement } from "@/lib/billing/entitlement";
import { absoluteUrl } from "@/lib/site";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Pricing",
  description:
    "The PMVL research product is public and free, and nothing on this site is for sale. A paid data tier is described but not open: it requires a live track record and a published Brier score against the market first.",
  alternates: { canonical: absoluteUrl("/pricing") },
};

export default async function PricingPage() {
  const entitlement = await getCurrentEntitlement();

  return (
    <>
      <div className="mx-auto max-w-6xl px-4 pb-14 pt-16">
        <PricingPlans entitlement={entitlement} headingLevel="h1" />
      </div>

      <Section>
        <SectionHeading
          eyebrow="What has to be true first"
          title="Two conditions before anything is sold"
          lead="Written as conditions rather than intentions, because an intention can be quietly revised and a condition either holds or does not."
        />
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">A real track record exists</h3>
            <p className="t-body mt-2">
              At least 60 recommendations published live and settled, from a
              pipeline that has not stalled. Today that count is zero: everything
              downstream of the daily snapshot has only ever run on synthetic
              history.
            </p>
          </div>
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">
              Its Brier score against the market is published
            </h3>
            <p className="t-body mt-2">
              Whatever the sign. Replayed against already-settled markets, the
              estimate currently scores marginally <em>worse</em> than the
              market&rsquo;s own price — so the claim a subscription would rest on
              is one we have not been able to demonstrate. That figure goes on{" "}
              <Link href="/track-record" className="underline underline-offset-2">
                the track record
              </Link>{" "}
              before any tier opens.
            </p>
          </div>
        </div>
        <p className="t-body mt-8 max-w-2xl">
          The Founding Research Pilot that used to be offered here has been
          withdrawn for the same reason. Nobody had paid, so there was nothing to
          unwind.{" "}
          <Link href="/founding-pilot" className="underline underline-offset-2">
            What it was, and why it is closed
          </Link>
          .
        </p>
      </Section>

      <Section className="bg-sunken">
        <SectionHeading
          eyebrow="Before you subscribe"
          title="What a subscription would and would not buy"
          lead="Written now, while nothing can be charged, so it cannot be quietly softened later."
        />
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">It does not buy signals</h3>
            <p className="t-body mt-2">
              PMVL publishes research. It does not sell trade recommendations,
              does not promise returns, and does not claim an edge over the
              venues it scans. On most days its ranked list is empty, and that is
              the intended behaviour rather than a shortfall. Read the{" "}
              <Link href="/risk-disclosure" className="underline underline-offset-2">
                risk disclosure
              </Link>{" "}
              before treating anything here as actionable.
            </p>
          </div>
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">It does not gate the public research</h3>
            <p className="t-body mt-2">
              Everything currently on this site stays free. The paid tier is
              being designed as additional capability, not as a fence around what
              already exists. If that changes, it will be announced before it
              happens, not discovered by a reader hitting a paywall on a page
              that used to be open.
            </p>
          </div>
        </div>
      </Section>

      <Section>
        <SectionHeading eyebrow="Billing questions" title="How billing will work" />
        <Faq
          items={[
            {
              q: "Can I be charged right now?",
              a: "No. The production deployment has billing switched off at the server, not merely hidden in the interface. Even if the checkout buttons were forced to render, the server rejects the request. Activating billing requires a server-side billing mode, valid Stripe credentials, allowlisted price IDs, completed legal review and the owner's approval.",
            },
            {
              q: "Who processes payments?",
              a: "Stripe, on Stripe's own hosted checkout page. PMVL never sees or stores a card number: the browser goes to Stripe, and this application receives only a customer reference and a subscription status back.",
            },
            {
              q: "How do I cancel?",
              a: (
                <>
                  Through Stripe&apos;s customer portal, reachable from{" "}
                  <Link
                    href="/account/billing"
                    className="underline underline-offset-2"
                  >
                    your billing page
                  </Link>
                  . Cancelling stops the next renewal; access continues until the
                  end of the period already paid for.
                </>
              ),
            },
            {
              q: "What happens if a payment fails?",
              a: "The account moves to a payment-failed state and Pro access is suspended until a payment succeeds. Public research is unaffected — it is not gated on billing at all, so a billing problem can never take it away.",
            },
            {
              q: "What data is kept about my subscription?",
              a: (
                <>
                  A Stripe customer ID, a subscription ID, the subscription
                  status, the price ID, the current period end and whether a
                  cancellation is scheduled. No card details, ever. The{" "}
                  <Link href="/privacy" className="underline underline-offset-2">
                    privacy page
                  </Link>{" "}
                  lists this exactly.
                </>
              ),
            },
          ]}
        />
      </Section>
    </>
  );
}
