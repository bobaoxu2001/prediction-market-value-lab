import type { Metadata } from "next";
import Link from "next/link";

import { LegalPage, LegalSection } from "@/components/legal";
import {
  BUSINESS_MAILING_ADDRESS,
  DATA_CONTROLLER,
} from "@/lib/seller";
import { absoluteUrl, SUPPORT_EMAIL } from "@/lib/site";

export const metadata: Metadata = {
  title: "Privacy notice",
  description:
    "What PMVL collects: optional account identity, privacy-minimised first-party funnel event counts, email you choose to send about the proposed $29 plan, and operational logs.",
  alternates: { canonical: absoluteUrl("/privacy") },
};

export default function PrivacyPage() {
  return (
    <LegalPage
      title="Privacy notice"
      updated="13 August 2026"
      summary="What this application actually collects, why, and who else sees it. It describes the implementation as built — not a policy template with categories the software does not use."
    >
      <LegalSection id="scope" title="Scope of this notice">
        <p>
          This notice covers the PMVL web application and its research API. It is
          written against the code that is deployed: where a section says nothing
          is collected, that is because there is no code path that collects it,
          not because collection is merely disallowed by policy.
        </p>
        <p>
          Reading the public research requires no account and no sign-in. If you
          never create an account and do not use an instrumented product action,
          the only data involved is the operational logging described below.
        </p>
      </LegalSection>

      <LegalSection id="collected" title="What is collected">
        <p>
          <strong>Account identity, if you create an account.</strong> Accounts
          are handled by Clerk, an authentication provider. Clerk holds your email
          address, any name you supply, and — if you sign in with Google — the
          identifier and basic profile Google returns to it. PMVL reads back only
          your user ID, primary email address, display name and account creation
          time, and uses them to identify your session and populate your account
          page.
        </p>
        <p>
          <strong>Session data.</strong> Clerk sets cookies necessary to keep you
          signed in and to protect the session. These are required for
          authentication to function; there is no version of a signed-in
          experience without them.
        </p>
        <p>
          <strong>Founding Lifetime interest email, only if you send it.</strong>{" "}
          The interest action opens a pre-addressed draft in your own email app.
          Nothing is sent to PMVL unless you choose to send it. If sent, PMVL
          receives the address, message and ordinary email metadata and uses them
          to evaluate the proposed plan and reply to you.
        </p>
        <p>
          Sending that message creates no purchase, reservation, entitlement or
          promise of launch. It is kept privately and is never committed to the
          public code repository. You can ask for it to be deleted through the
          contact address below.
        </p>
        <p>
          <strong>First-party funnel events.</strong> When you use a small set of
          named product actions, the browser sends a same-origin request to PMVL.
          The event contains only an allowlisted event name, the source
          (&ldquo;web&rdquo;) and, when applicable, an allowlisted placement. The
          server adds its time and deployment environment before writing a
          structured operational log entry.
        </p>
        <p>
          The funnel payload and PMVL&apos;s structured application log do not
          include a cookie, persistent identifier, page URL, referrer, IP address,
          user agent, market, order, account, email or payment data. As with every
          web request, Vercel may still process ordinary network metadata such as
          IP address and user agent in its platform access logs. PMVL cannot use
          the application event implementation to count unique users across
          sessions; it measures event totals and conversion intent only.
        </p>
        <p>
          <strong>PMVL never receives or stores card numbers</strong>, bank
          details or any other payment instrument. The $29 interest test contains
          no card field or payment processor.
        </p>
        <p>
          <strong>Operational logs.</strong> The hosting platform records ordinary
          server logs — request paths, status codes, timings and error traces.
          Application logging is written to exclude secrets and to record
          identifiers rather than payloads.
        </p>
      </LegalSection>

      <LegalSection id="not-collected" title="What is not collected">
        <ul className="list-disc space-y-1 pl-5">
          <li>
            <strong>No third-party or behavioural analytics.</strong> There is no
            analytics package, tracking pixel, advertising script, cross-site
            identifier or visitor profile. The privacy-minimised first-party event
            counts described above are the only product measurement.
          </li>
          <li>No card, bank or payment-instrument data, at any point.</li>
          <li>
            No trading account credentials, wallet keys or venue API keys — PMVL
            has no execution access and no field to put them in.
          </li>
          <li>No financial position, portfolio or net-worth information.</li>
          <li>
            No profile beyond what is listed above. The account page asks for
            nothing further.
          </li>
        </ul>
      </LegalSection>

      <LegalSection id="why" title="Why it is collected">
        <p>
          Account identity and session cookies exist to sign you in and to keep
          the account pages private to you. A Founding Lifetime interest email is
          used to evaluate that product idea and reply. First-party funnel event
          totals are used to understand whether key product actions are reached.
          Operational logs exist to keep the service running and investigate
          faults. None of this data is used for advertising or sold to anyone.
        </p>
      </LegalSection>

      <LegalSection id="processors" title="Who else sees it">
        <ul className="list-disc space-y-1 pl-5">
          <li>
            <strong>Clerk</strong> — authentication and account storage. Sees your
            identity data and session activity.
          </li>
          <li>
            <strong>Vercel</strong> — application hosting. Sees request metadata
            and server logs.
          </li>
          <li>
            <strong>Google</strong> — only if you choose to sign in with Google,
            and only to the extent of that sign-in.
          </li>
          <li>
            <strong>Email providers</strong> — only if you send the Founding
            Lifetime interest draft. Your provider and PMVL&apos;s provider process
            the address, message and ordinary delivery metadata.
          </li>
        </ul>
        <p>
          Each of these is an independent company with its own privacy policy. No
          other third party receives your data, and PMVL does not sell or rent
          personal information to anyone.
        </p>
      </LegalSection>

      <LegalSection id="retention" title="Retention">
        <p>
          Retention periods for server logs, including the non-identifying funnel
          event entries, are set by the hosting provider. Interest emails are
          handled as product-research correspondence and can be deleted on
          request. Deletion and export are carried out by hand: no automated
          deletion or data-export process has been implemented in this release.
        </p>
      </LegalSection>

      <LegalSection id="rights" title="Your rights">
        <p>
          Depending on where you live you may have rights to access, correct,
          export or delete personal data held about you, and to object to certain
          processing. To exercise them, write to{" "}
          <a
            href={`mailto:${SUPPORT_EMAIL}`}
            className="underline underline-offset-2"
          >
            {SUPPORT_EMAIL}
          </a>
          . Requests are handled manually; there is no self-service export or
          deletion in this release.
        </p>
        <p>
          The data controller is {DATA_CONTROLLER} Written notices may be sent to{" "}
          {BUSINESS_MAILING_ADDRESS}. Because the controller is an individual
          based in the United States rather than a company established in the
          EU or UK, there is no lead EU or UK supervisory authority for this
          service; where local law gives you a right to complain to a data
          protection authority, that right is unaffected.
        </p>
      </LegalSection>

      <LegalSection id="cookies" title="Cookies">
        <p>
          The only cookies this application sets are the authentication and
          session cookies Clerk requires, and they are set only once you sign in.
          A theme preference is stored in your browser&apos;s local storage, which
          never leaves your device and is not a cookie. There are no advertising,
          advertising or cross-site tracking cookies. First-party funnel events
          set no cookie and create no persistent visitor identifier.
        </p>
      </LegalSection>

      <LegalSection id="children" title="Children">
        <p>
          The service is not directed at children and accounts are limited to
          people old enough to contract, as stated in the{" "}
          <Link href="/terms" className="underline underline-offset-2">
            terms
          </Link>
          .
        </p>
      </LegalSection>

      <LegalSection id="changes" title="Changes to this notice">
        <p>
          If what the software collects changes, this page changes first or at the
          same time — not afterwards. The &ldquo;last updated&rdquo; date at the
          top reflects the most recent change.
        </p>
      </LegalSection>
    </LegalPage>
  );
}
