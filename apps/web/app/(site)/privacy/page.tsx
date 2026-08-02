import type { Metadata } from "next";
import Link from "next/link";

import { LegalPage, LegalSection, Placeholder } from "@/components/legal";
import { absoluteUrl, SUPPORT_EMAIL } from "@/lib/site";

export const metadata: Metadata = {
  title: "Privacy notice",
  description:
    "What PMVL collects: account identity from Clerk, session cookies, Stripe customer and subscription references, and operational logs. Nothing else, and no analytics.",
  alternates: { canonical: absoluteUrl("/privacy") },
};

export default function PrivacyPage() {
  return (
    <LegalPage
      title="Privacy notice"
      updated="2 August 2026"
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
          never create an account, the only data involved is the operational
          logging described below.
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
          <strong>Billing references, if you subscribe.</strong> Payments are
          processed by Stripe. PMVL stores, against your account: a Stripe
          customer ID, a subscription ID, the subscription status, the price ID,
          the current period end, whether a cancellation is scheduled, and the ID
          and timestamp of the last Stripe event applied — the last two solely so
          a repeated or out-of-order event cannot corrupt your entitlement.
        </p>
        <p>
          <strong>PMVL never receives or stores card numbers</strong>, bank
          details or any other payment instrument. Those go from your browser to
          Stripe directly.
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
            <strong>No analytics.</strong> There is no analytics package, no
            tracking pixel and no advertising script on this site. If analytics is
            added later, this notice will be updated before it ships.
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
          the account pages private to you. Billing references exist to determine
          what your subscription entitles you to and to let you manage it in
          Stripe&apos;s portal. Operational logs exist to keep the service running
          and to investigate faults. There is no other purpose, and none of it is
          used for advertising or sold to anyone.
        </p>
      </LegalSection>

      <LegalSection id="processors" title="Who else sees it">
        <ul className="list-disc space-y-1 pl-5">
          <li>
            <strong>Clerk</strong> — authentication and account storage. Sees your
            identity data and session activity.
          </li>
          <li>
            <strong>Stripe</strong> — payment processing and subscription
            management. Sees your payment details and billing address, which PMVL
            does not.
          </li>
          <li>
            <strong>Vercel</strong> — application hosting. Sees request metadata
            and server logs.
          </li>
          <li>
            <strong>Google</strong> — only if you choose to sign in with Google,
            and only to the extent of that sign-in.
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
          Account and billing records persist for as long as the account exists.
          Retention periods after account closure, and the retention period for
          server logs, are set by the hosting and authentication providers and by
          a policy the owner has not yet fixed:{" "}
          <Placeholder>DATA RETENTION POLICY</Placeholder>.
        </p>
        <p>
          This section deliberately does not promise a specific deletion schedule.
          No automated deletion or data-export process has been implemented in
          this release, and claiming one would be false.
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
          The identity of the data controller and the supervisory authority you
          may complain to depend on the legal entity and jurisdiction, which are
          not yet fixed: <Placeholder>DATA CONTROLLER AND JURISDICTION</Placeholder>.
        </p>
      </LegalSection>

      <LegalSection id="cookies" title="Cookies">
        <p>
          The only cookies this application sets are the authentication and
          session cookies Clerk requires, and they are set only once you sign in.
          A theme preference is stored in your browser&apos;s local storage, which
          never leaves your device and is not a cookie. There are no advertising,
          analytics or cross-site tracking cookies, which is why this site has no
          consent banner — there is nothing to consent to.
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
