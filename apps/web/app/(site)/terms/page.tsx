import type { Metadata } from "next";
import Link from "next/link";

import { LegalPage, LegalSection } from "@/components/legal";
// The former pilot terms come from the same constants its preserved historical
// page renders, so this document cannot describe a different withdrawn offer.
import {
  PILOT_DURATION_DAYS,
  PILOT_MEMBER_CAP,
  PILOT_PRICE_USD,
} from "@/lib/pilot";
import {
  BUSINESS_MAILING_ADDRESS,
  DISPUTE_VENUE,
  GOVERNING_JURISDICTION,
  MINIMUM_AGE,
  SELLER_DESCRIPTION,
  SELLER_LEGAL_NAME,
} from "@/lib/seller";
import { absoluteUrl, SUPPORT_EMAIL } from "@/lib/site";

export const metadata: Metadata = {
  title: "Terms of service",
  description:
    "Draft terms for PMVL: free public research, the non-charging $29 Founding Lifetime demand test, the withdrawn Founding Research Pilot, acceptable use and research limitations.",
  alternates: { canonical: absoluteUrl("/terms") },
};

export default function TermsPage() {
  return (
    <LegalPage
      title="Terms of service"
      updated="13 August 2026"
      summary="The agreement governing use of PMVL. It also records that the $29 Founding Lifetime page is product research rather than an offer for sale, and that the earlier Founding Research Pilot remains withdrawn. This document has not been reviewed by a lawyer."
    >
      <LegalSection id="parties" title="1. Who these terms are between">
        <p>
          These terms are between you and {SELLER_LEGAL_NAME}, {SELLER_DESCRIPTION}{" "}
          offering the Prediction Market Value Lab service (&ldquo;PMVL&rdquo;,
          &ldquo;we&rdquo;, &ldquo;the service&rdquo;).
          Business mailing address: {BUSINESS_MAILING_ADDRESS}.
        </p>
        <p>
          PMVL is operated by an individual, not by a company. There is no
          corporation, limited liability company or partnership behind it, and
          &ldquo;PMVL&rdquo; is the name of a service rather than a registered
          trademark or a fictitious business name.
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
          You must be at least {MINIMUM_AGE} years old
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
          <strong>Nothing is currently sold.</strong> Public research and the
          current cost tools are free. The proposed USD 29 Founding Lifetime
          plan is a demand test, not an offer for sale: there is no checkout,
          payment, contract, reservation, account entitlement or delivery
          commitment attached to its interest link.
        </p>
        <p>
          Sending the pre-addressed interest email does not hold a price or place
          and does not guarantee launch. PMVL may change the proposed scope,
          price, availability or decide not to launch after evaluating demand,
          ongoing cost and delivery feasibility.
        </p>
        <p>
          If a Founding Lifetime product is offered later, it will have separate
          final terms defining what &ldquo;lifetime&rdquo; means, included and
          excluded capabilities, price, tax, refund rights, restoration and
          termination. Expressing interest now is not acceptance of those future
          terms.
        </p>
        <p>
          The earlier Founding Research Pilot remains withdrawn and was never
          sold. Its recorded terms were USD {PILOT_PRICE_USD} once for{" "}
          {PILOT_DURATION_DAYS} days, limited to {PILOT_MEMBER_CAP} members. They
          are retained for transparency on the closed pilot page and do not
          describe an available product.
        </p>
      </LegalSection>

      <LegalSection id="cancellation" title="6. Ending the service, and refunds">
        <p>
          There is currently no purchase to cancel and no payment to refund. An
          interest email may be withdrawn at any time by contacting us; withdrawal
          does not require deleting aggregate, non-identifying event counts that
          cannot be tied back to the sender.
        </p>
        <p>
          If a paid offer opens later, its final cancellation and refund terms
          will be shown before payment. Statutory rights that apply where you live
          will not be affected.
        </p>
      </LegalSection>

      <LegalSection id="ip" title="7. Intellectual property">
        <p>
          The software, models, methodology documentation, page designs and
          written analysis on this site belong to {SELLER_LEGAL_NAME} or its
          licensors. Free access grants no ownership or right to resell the
          service. Any future purchase terms would define the personal,
          non-exclusive and non-transferable licence it grants.
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
          of the service.
        </p>
        <p>
          Because nothing is currently sold, these terms do not set a paid-plan
          liability cap. Any future paid offer will state its applicable limit
          before purchase. Nothing here limits liability that cannot legally be
          limited.
        </p>
        <p>
          Nothing here excludes liability for fraud or for anything else that
          cannot be excluded by law.
        </p>
      </LegalSection>

      <LegalSection id="availability" title="9. Service availability and changes">
        <p>
          We do not commit to an uptime level. The service may be unavailable for
          maintenance, for upstream data-source outages, or without notice.
          Research content, model coverage, pipeline cadence and feature
          availability may change; where a change removes something a paid
          product was sold on, we will give notice before it takes effect.
        </p>
      </LegalSection>

      <LegalSection id="termination" title="10. Termination">
        <p>
          You may stop using the service and close your account at any time. There
          is currently no paid plan or recurring billing to cancel.
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
          These terms are governed by the law of {GOVERNING_JURISDICTION}, and
          disputes will be resolved in {DISPUTE_VENUE}, without prejudice to any
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
          . Legal notices: {SELLER_LEGAL_NAME}, {BUSINESS_MAILING_ADDRESS}.
        </p>
        <p>
          The contact address above is confirmed and monitored. This document
          remains a draft that no lawyer has reviewed; no paid offer may rely on
          it until that review and the product-specific commercial terms are
          complete.
        </p>
      </LegalSection>
    </LegalPage>
  );
}
