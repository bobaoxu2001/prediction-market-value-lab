import type { Metadata } from "next";
import Link from "next/link";

import { Faq, Section, SectionHeading } from "@/components/marketing";
import { PricingPlans } from "@/components/pricing";
import { getCurrentEntitlement } from "@/lib/billing/entitlement";
import {
  PILOT_DURATION_DAYS,
  PILOT_MEMBER_CAP,
  PILOT_PRICE_USD,
} from "@/lib/pilot";
import { absoluteUrl } from "@/lib/site";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Pricing",
  description:
    "The PMVL research product is public and free. A paid Founding Pro tier is in preparation and is not yet being sold; billing is disabled on the production deployment.",
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
          eyebrow="Available now"
          title="The Founding Research Pilot"
          lead={`Subscriptions are not open, but a small, finite pilot is: ${PILOT_DURATION_DAYS} days of the daily written research digest, delivered by email, capped at ${PILOT_MEMBER_CAP} members.`}
        />
        <p className="t-body mt-6 max-w-2xl">
          One-time USD {PILOT_PRICE_USD}, no account, no renewal and nothing to
          cancel. Three
          real sample reports — generated from the published Snapshot, and
          labelled as historical rather than current research — are readable
          before paying anything.{" "}
          <Link href="/founding-pilot" className="underline underline-offset-2">
            Read what the pilot is, and what it is not
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
