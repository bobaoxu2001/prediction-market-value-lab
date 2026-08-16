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
    "PMVL is free. A proposed $29 Founding Lifetime local-first plan is shown as a non-charging demand test: no purchase, reservation or entitlement is created.",
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
          title="Evidence before a real offer opens"
          lead="An interest click answers whether the proposition attracts attention. It does not prove retention, delivery feasibility or willingness to complete a purchase."
        />
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">A real track record exists</h3>
            <p className="t-body mt-2">
              Before accepting money, PMVL needs evidence that people repeatedly
              use the free cost workflow — not just visit the pricing page. The
              Founding Lifetime test measures intent while keeping that distinction
              visible.
            </p>
          </div>
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">The promised capability is built and bounded</h3>
            <p className="t-body mt-2">
              A real offer needs working local entitlements, a precise definition
              of lifetime, tested restoration and revocation, final refund and tax
              handling, and legal review. None exists in the demand-test flow.
            </p>
          </div>
        </div>
        <p className="t-body mt-8 max-w-2xl">
          The previous $49 Founding Research Pilot remains withdrawn. It sold a
          forecast-based email digest; this $29 test is a different, local-first
          cost-tool proposition and does not revive that offer.{" "}
          <Link href="/founding-pilot" className="underline underline-offset-2">
            What it was, and why it is closed
          </Link>
          .
        </p>
      </Section>

      <Section className="bg-sunken">
        <SectionHeading
          eyebrow="Product boundary"
          title="What the founding idea would and would not buy"
          lead="Written while nothing can be charged, so the demand test does not quietly become a promise."
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
        <SectionHeading eyebrow="Interest-test questions" title="What happens now" />
        <Faq
          items={[
            {
              q: "Can I be charged right now?",
              a: "No. The production deployment has billing switched off at the server. The $29 action opens an email draft and never calls the billing route. No card, purchase, reservation or entitlement is created.",
            },
            {
              q: "What is measured?",
              a: "The flow measures that someone chose the $29 interest action and, only if they send the draft, the message they chose to email. A click is evidence of interest, not a unique user, purchase or conversion.",
            },
            {
              q: "Does expressing interest hold the $29 price?",
              a: "No. It does not reserve a place or price. Scope, price and availability may change, and PMVL may decide not to launch the plan.",
            },
            {
              q: "What stays free?",
              a: "The working public research, snapshot cost calculator and current live overlay stay free. The proposed plan adds local workflow capability rather than moving today's public product behind a paywall.",
            },
            {
              q: "What happens to my email?",
              a: (
                <>
                  Nothing is sent unless you send the draft in your own mail app.
                  If you do, PMVL uses the address and message to evaluate the
                  concept and may reply. The{" "}
                  <Link href="/privacy" className="underline underline-offset-2">
                    privacy page
                  </Link>{" "}
                  describes the flow and retention.
                </>
              ),
            },
          ]}
        />
      </Section>
    </>
  );
}
