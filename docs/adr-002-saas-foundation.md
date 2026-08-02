# ADR 002 — Accounts, entitlements and billing for the SaaS layer

**Status:** accepted for the foundation; live charging explicitly deferred.
**Date:** 2 August 2026.
**Supersedes:** nothing. **Related:** `docs/saas-setup.md`, `docs/legal-placeholders.md`.

## Context

PMVL is a working, public prediction-market research product with no accounts,
no payments and no marketing entry point: `/` was the research briefing and the
only visitor it served well was one who already knew what the project was.

This ADR records the decisions behind the first product layer on top of it — a
public site, accounts, an entitlement model and Stripe test-mode billing — and
in particular the decision *not* to charge anyone yet.

## Decision 1 — Clerk for authentication

**Chosen:** Clerk, via its official Next.js App Router integration.

**Rejected:** a custom email/password database; NextAuth/Auth.js; Supabase Auth.

Reasons:

- A custom password database is a liability with no upside here. Password
  hashing, reset flows, session rotation, breach response, MFA and bot
  protection are all things that have to be right and none of them are this
  product's problem.
- Google sign-in was a requirement, and delegating OAuth to a provider that
  maintains the integrations is cheaper than maintaining them.
- Clerk resolves the session **server-side** in React Server Components. That is
  what lets the header render the correct navigation in the first HTML response
  instead of flashing "Create free account" at someone already signed in — and,
  in the other direction, never sketching the shape of the account navigation
  for someone who is not.
- Private metadata gives a server-only key-value store attached to a user,
  which is exactly the shape the entitlement cache needs, without introducing a
  database to a repository that currently has one read-only SQLite artifact and
  no application database.

Cost: a hard dependency on a vendor for sign-in. Accepted because the blast
radius is bounded — public research does not require an account and is
unaffected if Clerk is unavailable — and because the entitlement layer sits
behind an interface (`getCurrentEntitlement`) that does not name Clerk.

## Decision 2 — Stripe-hosted Checkout, not an embedded card form

**Chosen:** Stripe Checkout (hosted) plus the Stripe Customer Portal.

**Rejected:** Stripe Elements embedded in a PMVL page; a custom cancellation and
payment-method UI.

Reasons:

- No card data ever touches this application, which collapses the PCI surface
  to essentially nothing and makes the privacy notice short and true.
- Hosted Checkout handles 3-D Secure, wallets, local payment methods, tax and
  currency without this codebase learning any of it.
- The Customer Portal removes the need to build cancellation, proration,
  payment-method updates and invoice history — four surfaces that are easy to
  get subtly wrong and that directly affect someone's money.
- Fewer endpoints means fewer places to get authorisation wrong. This layer has
  three: create checkout, open portal, receive webhook.

Cost: less control over the checkout look. Irrelevant for a product whose
positioning is restraint.

## Decision 3 — Stripe is the source of truth; Clerk metadata is a cache

Entitlement state is written **only** by the signed webhook, and only after
re-reading the subscription from Stripe. The browser's return from Checkout
updates nothing.

Reasons:

- A success-URL redirect proves that a browser was sent to a URL. It can be
  forged by typing it, replayed, or never happen at all when someone closes the
  tab after paying. Treating it as payment confirmation is the classic way a
  subscription product ends up granting access to people who did not pay and
  denying it to people who did.
- Re-reading the subscription rather than trusting the event payload is the
  strongest available ordering protection: a late-delivered event then writes
  *current* state. Explicit `event.id` and `event.created` guards handle the
  rest — a redelivery is a no-op, and an event older than the last applied is
  dropped.
- Caching in Clerk private metadata keeps page rendering off Stripe's API on
  every request without making the cache authoritative. If it is ever wrong, the
  next webhook corrects it, and the states it can be wrong *in* all fail closed.

## Decision 4 — Billing is test-only, and live is a code change

`BILLING_MODE` accepts `disabled` and `test`. There is no `live`. A
`sk_live_` key is rejected rather than honoured, so shipping the wrong secret
disables billing instead of enabling real payments.

Reasons:

- The product being sold does not exist yet. The Pro tier is early access to
  work in progress, and the pricing page says so rather than listing alerts,
  exports or an API that have not been built.
- The legal documents have not been reviewed by a lawyer and contain unresolved
  placeholders — the legal entity, the jurisdiction, the refund policy, the
  retention policy. Taking money against unreviewed terms with a blank refund
  policy is a bad idea independent of the software.
- An environment variable is too easy to set. Making "go live" require a
  reviewed code change puts a pull request between an idea at 2am and a real
  charge.

The corollary, stated as an implementation rule and asserted by tests:
**setting a public UI flag alone must never activate billing.**

## Decision 5 — Public research stays public

No research route consults the entitlement layer. Not "is currently ungated" —
*does not call it at all*, which is why a billing outage, a Clerk outage or a
missing metadata field cannot take the research away.

Reasons:

- It is the product's credibility. The argument PMVL makes is that it will tell
  you when there is no trade; a paywall in front of the funnel that shows zero
  actionable opportunities would undermine the one thing it is trying to
  demonstrate.
- The free tier is what a prospective subscriber evaluates. Hiding it optimises
  a signup rate at the cost of the people who would have stayed.
- A subscription product that gates its existing free content on the day it
  introduces billing teaches its users to expect that.

If a paid tier ever gates something, it will gate *new* capability, and it will
be announced before it happens rather than discovered by a reader hitting a
paywall on a page that used to be open.

## Decision 6 — Route groups rather than moving the research product

The research terminal moved into a `(research)` route group, which contributes
nothing to the URL. Every research path is byte-identical; only the briefing
moved, from `/` to `/app`, and `/` forwards any request carrying a briefing
query parameter so existing deep links keep working.

The alternative — a shared shell component invoked from each page — would have
duplicated the layout's data fetch on every route and left two places to change
the header. The alternative of leaving `/` as the briefing would have meant no
marketing entry point at all.

## Consequences

- Two vendors are now on the critical path for *accounts*, and neither is on the
  critical path for *research*.
- Entitlement state lives in two systems. Stripe is authoritative; the cache is
  reconstructible by replaying webhooks or by a one-off backfill from Stripe.
- Nobody can be charged from this pull request, and making that possible
  requires code review, legal review and owner approval.
- The pricing page currently sells early access to unfinished work. That is
  honest but it is not a business; the live-launch checklist in
  `docs/saas-setup.md` names what has to be true before it is one.
