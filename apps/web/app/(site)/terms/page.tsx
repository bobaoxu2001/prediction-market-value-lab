import type { Metadata } from "next";
import Link from "next/link";

import { LegalPage, LegalSection, Placeholder } from "@/components/legal";
// The commercial terms come from the same constants the sales page renders, so
// the agreement and the offer cannot state different prices, durations or caps.
import {
  PILOT_DURATION_DAYS,
  PILOT_MEMBER_CAP,
  PILOT_PRICE_USD,
} from "@/lib/pilot";
import { absoluteUrl, SUPPORT_EMAIL } from "@/lib/site";

export const metadata: Metadata = {
  title: "Terms of service",
  description:
    "Draft terms for the PMVL research service: eligibility, acceptable use, one-time pilot billing, refunds, intellectual property, research limitations, availability and termination.",
  alternates: { canonical: absoluteUrl("/terms") },
};

export default function TermsPage() {
  return (
    <LegalPage
      title="Terms of service"
      updated="2 August 2026"
      summary="The agreement that will govern use of PMVL. It is a foundation: the commercial and jurisdictional terms are marked as placeholders because inventing them would be worse than leaving them open."
    >
      <LegalSection id="parties" title="1. Who these terms are between">
        <p>
          These terms are between you and <Placeholder>LEGAL ENTITY NAME</Placeholder>,
          the operator of the Prediction Market Value Lab service at{" "}
          <Placeholder>PRODUCT AND SERVICE NAME AS REGISTERED</Placeholder>{" "}
          (&ldquo;PMVL&rdquo;, &ldquo;we&rdquo;, &ldquo;the service&rdquo;).
          Registered address: <Placeholder>REGISTERED ADDRESS</Placeholder>.
        </p>
        <p>
          By using the service you agree to these terms, to the{" "}
          <Link href="/privacy" className="underline underline-offset-2">
            privacy notice
          </Link>{" "}
          and to the{" "}
          <Link href="/risk-disclosure" className="underline underline-offset-2">
            risk disclosure
          </Link>
          . If you do not agree, do not use the service.
        </p>
      </LegalSection>

      <LegalSection id="eligibility" title="2. Account eligibility">
        <p>
          You must be at least <Placeholder>MINIMUM AGE</Placeholder> years old
          and legally able to enter into a contract to hold an account. You must
          provide accurate registration information and keep it current. You are
          responsible for activity under your account and for the security of the
          credentials or third-party identity you sign in with.
        </p>
        <p>
          One account per person. Accounts may not be shared, resold or
          transferred. We may decline to open an account, or close one, where we
          reasonably believe these terms have been breached or where operating an
          account for you would be unlawful in your jurisdiction.
        </p>
      </LegalSection>

      <LegalSection id="service" title="3. What the service is">
        <p>
          PMVL publishes research and informational analysis about publicly
          traded prediction-market contracts. It is not investment, legal, tax or
          financial advice, it is not a solicitation or an offer to trade, and it
          is not personalised to you. PMVL is read-only: it holds no funds,
          custodies no assets, stores no exchange credentials and places no
          orders on any venue.
        </p>
        <p>
          Nothing in these terms creates an advisory or fiduciary relationship.
          Any position you take is your own decision, taken on a venue with which
          you have your own separate relationship.
        </p>
      </LegalSection>

      <LegalSection id="acceptable-use" title="4. Acceptable use">
        <p>You agree not to:</p>
        <ul className="list-disc space-y-1 pl-5">
          <li>
            present PMVL output as investment advice, as a recommendation, or as
            a claim about guaranteed or likely returns;
          </li>
          <li>
            resell, redistribute or republish the research as a signal service or
            as your own product;
          </li>
          <li>
            scrape, crawl or automate access in a way that degrades the service
            for others, or circumvent rate limits or access controls;
          </li>
          <li>
            attempt to gain unauthorised access to any account, system or data,
            or to interfere with the integrity of the service;
          </li>
          <li>
            use the service where doing so, or trading on the venues it describes,
            would be unlawful for you.
          </li>
        </ul>
      </LegalSection>

      <LegalSection id="billing" title="5. What is sold, and how it is paid for">
        <p>
          The only paid product is the Founding Research Pilot:{" "}
          <strong>USD {PILOT_PRICE_USD}, charged once</strong>, for{" "}
          {PILOT_DURATION_DAYS} days of emailed research digests counted from
          your first delivery, plus any tax that applies where you are. The first
          cohort is limited to {PILOT_MEMBER_CAP} members.
        </p>
        <p>
          <strong>This is not a subscription.</strong> There is no recurring
          charge, no automatic renewal and nothing to cancel. When the{" "}
          {PILOT_DURATION_DAYS} days end, deliveries stop and you are not billed
          again. Continuing afterwards would require a new, separate purchase.
        </p>
        <p>
          Payment is taken on a Stripe-hosted payment page. We do not receive or
          store your card details. Your use of Stripe&apos;s payment pages is
          additionally subject to Stripe&apos;s terms.
        </p>
        <p>
          Paying does not by itself start the service. Each payment is confirmed
          by a person against Stripe&apos;s record before delivery begins, and we
          contact you at the email address used at checkout. Public research on
          this site is never gated on payment.
        </p>
      </LegalSection>

      <LegalSection id="cancellation" title="6. Ending the service, and refunds">
        <p>
          There is nothing to cancel: the Founding Research Pilot is a one-time
          purchase that ends by itself after {PILOT_DURATION_DAYS} days. No
          further payment is taken, so no cancellation step is required of you.
        </p>
        <p>
          Refund policy: <Placeholder>REFUND POLICY</Placeholder>. Statutory
          cancellation and refund rights that apply where you live are not
          affected by anything in this section.
        </p>
      </LegalSection>

      <LegalSection id="ip" title="7. Intellectual property">
        <p>
          The software, models, methodology documentation, page designs and
          written analysis on this site belong to{" "}
          <Placeholder>LEGAL ENTITY NAME</Placeholder> or its licensors. Your
          subscription grants a personal, non-exclusive, non-transferable right to
          use the service for your own research. It does not transfer ownership
          of anything.
        </p>
        <p>
          Underlying market data originates from the public Kalshi and Polymarket
          APIs and remains subject to those venues&apos; own terms. You may quote
          short extracts of PMVL&apos;s published analysis with attribution and a
          link; you may not reproduce it wholesale or use it to train a competing
          model without written permission.
        </p>
      </LegalSection>

      <LegalSection id="limitations" title="8. Research limitations and no warranty">
        <p>
          The service is provided &ldquo;as is&rdquo;. We do not warrant that the
          research is accurate, complete, current or fit for any purpose, that the
          probability estimates are correct, that displayed prices are executable,
          or that the service will be uninterrupted or error-free.
        </p>
        <p>
          The hosted deployment serves a frozen snapshot, so data is stale by
          design and is labelled as such. Model coverage is partial. Backtests and
          demo datasets are simulations, not live performance. The{" "}
          <Link href="/risk-disclosure" className="underline underline-offset-2">
            risk disclosure
          </Link>{" "}
          forms part of these terms and describes these limitations in detail.
        </p>
        <p>
          To the maximum extent permitted by law, we are not liable for trading
          losses, lost profits, or indirect or consequential loss arising from use
          of the service. Where liability cannot lawfully be excluded, it is
          limited to <Placeholder>LIABILITY CAP</Placeholder>. Nothing here
          excludes liability for fraud or for anything else that cannot be
          excluded by law.
        </p>
      </LegalSection>

      <LegalSection id="availability" title="9. Service availability and changes">
        <p>
          We do not commit to an uptime level. The service may be unavailable for
          maintenance, for upstream data-source outages, or without notice.
          Research content, model coverage, pipeline cadence and feature
          availability may change; where a change removes something a paid
          subscription was sold on, we will give notice before it takes effect.
        </p>
      </LegalSection>

      <LegalSection id="termination" title="10. Termination">
        <p>
          You may stop using the service and close your account at any time.
          Cancelling a subscription is separate from closing an account and is
          described in section 6.
        </p>
        <p>
          We may suspend or terminate an account for breach of these terms, for
          conduct that endangers the service or other users, or where continuing
          to serve you would be unlawful. Where we terminate without cause, we
          will refund the unused portion of any period already paid for.
        </p>
      </LegalSection>

      <LegalSection id="law" title="11. Governing law and disputes">
        <p>
          These terms are governed by the law of{" "}
          <Placeholder>GOVERNING JURISDICTION</Placeholder>, and disputes will be
          resolved in the courts of{" "}
          <Placeholder>DISPUTE VENUE</Placeholder>, without prejudice to any
          mandatory consumer-protection rights available to you where you live.
        </p>
      </LegalSection>

      <LegalSection id="changes" title="12. Changes to these terms">
        <p>
          We may update these terms. Material changes will be notified to account
          holders at the address on file before they take effect, and the
          &ldquo;last updated&rdquo; date at the top of this page will change.
          Continuing to use the service after that date means accepting the
          updated terms.
        </p>
      </LegalSection>

      <LegalSection id="contact" title="13. Contact">
        <p>
          Questions about these terms:{" "}
          <a
            href={`mailto:${SUPPORT_EMAIL}`}
            className="underline underline-offset-2"
          >
            {SUPPORT_EMAIL}
          </a>
          . Legal notices: <Placeholder>LEGAL NOTICE ADDRESS</Placeholder>.
        </p>
        <p>
          The contact address above is confirmed and monitored. It is the only
          value on this page that has been resolved; every bracketed marker is
          still outstanding, and this document remains a draft that no lawyer has
          read.
        </p>
      </LegalSection>
    </LegalPage>
  );
}
