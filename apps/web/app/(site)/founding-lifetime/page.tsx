import type { Metadata } from "next";
import Link from "next/link";

import { Faq, Section, SectionHeading } from "@/components/marketing";
import {
  FOUNDING_LIFETIME_INTEREST_MAILTO,
  FOUNDING_LIFETIME_PRICE_USD,
} from "@/lib/founding-lifetime";
import { absoluteUrl } from "@/lib/site";

export const metadata: Metadata = {
  title: "$29 Founding Lifetime — interest test",
  description:
    "A non-charging demand test for a proposed local-first PMVL plan. Expressing interest is not a purchase, reservation or promise that the plan will launch.",
  alternates: { canonical: absoluteUrl("/founding-lifetime") },
};

export default function FoundingLifetimePage() {
  return (
    <>
      <section className="mx-auto max-w-6xl px-4 pb-14 pt-16 sm:pt-24">
        <p className="t-label">Founding Lifetime · demand test</p>
        <h1 className="mt-3 max-w-4xl text-[2rem] leading-[1.12] sm:text-[2.75rem]">
          Would this local-first toolkit be worth ${FOUNDING_LIFETIME_PRICE_USD} once?
        </h1>
        <p className="t-lead mt-5 max-w-2xl">
          PMVL is measuring interest before building or selling this plan. The
          working cost overlay and public research remain free. There is no
          checkout here, and sending an interest email does not buy or reserve
          lifetime access.
        </p>

        <div className="mt-8 flex flex-wrap items-center gap-3">
          <a
            href={FOUNDING_LIFETIME_INTEREST_MAILTO}
            className="btn-primary"
            data-pmvl-funnel="founding_offer_intent"
            data-pmvl-placement="pricing"
          >
            I&apos;d consider paying ${FOUNDING_LIFETIME_PRICE_USD}
          </a>
          <Link href="/extension" className="btn-quiet">
            Use the free overlay
          </Link>
        </div>
        <p className="mt-4 max-w-2xl t-meta">
          The first link opens a pre-addressed draft in your email app. Nothing
          is sent unless you choose to send it. If sent, your email address and
          message are used only to evaluate this product idea and reply to you;
          see the <Link href="/privacy" className="underline underline-offset-2">privacy notice</Link>.
        </p>
      </section>

      <Section className="bg-sunken">
        <SectionHeading
          eyebrow="Proposed boundary"
          title="Local capability, not an unlimited cloud promise"
          lead="Lifetime is only credible when it covers capabilities whose ongoing cost does not grow with every use."
        />
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">What the idea would include</h3>
            <ul className="mt-3 space-y-2 text-sm text-ink-muted">
              {[
                "Everything in Free, which remains public",
                "Reusable cost assumptions and order templates saved locally",
                "Local cost history, comparison views and export of your results",
                "Advanced local scenarios and local watchlist tools",
                "Future low-marginal-cost, local-first Pro updates",
              ].map((item) => <li key={item}>— {item}</li>)}
            </ul>
          </div>
          <div className="border-t border-line-subtle pt-4">
            <h3 className="t-sub-title">What it would not include</h3>
            <ul className="mt-3 space-y-2 text-sm text-ink-muted">
              {[
                "Cloud sync, hosted alerts or server-side long-term history",
                "API access, AI usage, high-frequency refreshes or team features",
                "Human or priority support",
                "Signals, trade recommendations, returns or execution",
                "A promise that every proposed feature will ship",
              ].map((item) => <li key={item}>— {item}</li>)}
            </ul>
          </div>
        </div>
      </Section>

      <Section>
        <SectionHeading eyebrow="Before you signal interest" title="What this click means" />
        <Faq
          items={[
            {
              q: "Am I buying anything?",
              a: "No. There is no checkout, card field, charge, contract, reservation, account entitlement or delivery commitment in this flow.",
            },
            {
              q: "Is $29 a launched price?",
              a: "No. It is the one-time price proposition being tested. PMVL may change the scope, price, availability or decide not to launch after reviewing demand and delivery costs.",
            },
            {
              q: "What does lifetime mean here?",
              a: "If a real offer launches, its final terms would define it. The current concept is access to a bounded set of local-first capabilities for as long as PMVL makes them available; it is not a promise of perpetual cloud service, every future feature or the operator's lifetime.",
            },
            {
              q: "What happens if I send the email?",
              a: "PMVL receives the address and message you choose to send, uses them to evaluate demand and may reply about the concept. No purchase or reservation is created.",
            },
          ]}
        />
        <p className="mt-8 t-meta">
          Research and information only. PMVL places no orders, holds no funds
          and does not provide investment, legal, tax or financial advice. Read
          the <Link href="/risk-disclosure" className="underline underline-offset-2">risk disclosure</Link>.
        </p>
      </Section>
    </>
  );
}
