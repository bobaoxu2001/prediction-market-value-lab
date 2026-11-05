# Legal document values — what is resolved and what is not

`/terms`, `/privacy` and `/risk-disclosure` are **drafts written by an engineer
to describe what the software actually does.** They have not been reviewed or
approved by a lawyer, and each page says so at the top. That has not changed.

What has changed is the values. Every one was originally left as a visible
placeholder rather than invented — guessing an entity name, a jurisdiction or a
refund policy produces a document that reads as finished and is wrong, which is
worse than one that obviously is not. **The owner has since resolved them, and
this document spent several releases still describing them as outstanding.** It
now records the resolved state, and the one value that is deliberately absent.

**Status change, 7 August 2026.** These placeholders were an open risk while the
Founding Pilot was on sale: taking money against unreviewed terms with a blank
refund policy is a bad idea independent of the software. The pilot is withdrawn
([ADR 003](adr-003-withdraw-the-pilot.md)) and nothing on the site is purchasable,
so they stop being urgent and become a **precondition**: resolving them is on the
list of things that must be true before any paid tier opens, alongside the
evidential gate in that ADR. They are not resolved by the pilot going away.

## How placeholders rendered

`components/legal.tsx` exports `<Placeholder>`, which renders as a highlighted
`[VALUE — OWNER INPUT REQUIRED]`. **No legal page uses it any more.** The
component is kept for the next value that needs one; the assertion in
`apps/web/tests/pages.test.tsx` has been inverted accordingly and now requires
that no legal page contains `OWNER INPUT REQUIRED`.

This document described every row below as outstanding for several releases
after the owner resolved them. The values were resolved in
`apps/web/lib/seller.ts`, which is the single module the pages read.

## Resolved

| Value | Constant in `lib/seller.ts` | Note |
| --- | --- | --- |
| Support email | `SUPPORT_EMAIL` (in `lib/site.ts`) | Confirmed by the owner and approved for the public Beta |
| Legal entity name | `SELLER_LEGAL_NAME` + `SELLER_TYPE` | An **individual**, not a company. Nothing may imply an LLC, corporation or registered mark, and a test asserts it |
| Product/service name | `SERVICE_NAME` | A service name, not a registered mark |
| Minimum age | `MINIMUM_AGE` | 18 |
| Governing jurisdiction | `GOVERNING_JURISDICTION` | |
| Dispute venue | `DISPUTE_VENUE` | |
| Refund policy | `REFUND_POLICY` | Rendered only on the preserved historical pilot page; the pilot is withdrawn |
| Liability cap | `LIABILITY_CAP` | |
| Data retention policy | `DATA_RETENTION_POLICY` | |
| Data controller and jurisdiction | `DATA_CONTROLLER`, `CONTROLLER_LOCATION` | |
| Billing / monthly / annual price | — | Not applicable: `BILLING_MODE=disabled` and nothing is purchasable |

## Deliberately unresolved: the notices address

`BUSINESS_MAILING_ADDRESS` used to hold the owner's exact **private residential
address**, rendered on `/terms` twice and `/privacy` once. This repository is
public: the string was served, indexed, and is permanent in history.

It has been removed from the working tree and replaced with
`LEGAL_NOTICE_METHOD` / `LEGAL_NOTICE_METHOD_SHORT`, which state plainly that no
business postal address is published, that notices are accepted at the contact
address, and that a postal address for formal service is supplied on request.

This is a resolution, not a remaining placeholder. The value that would fill it
properly is a **business or registered-agent address**, and one does not exist
because there is no company. Two regression suites keep it that way:

- `apps/web/tests/legal-notice-address.test.tsx` — no legal page renders anything
  shaped like a street line, a dwelling unit, or a state-and-ZIP pair, and the
  notices clause still says something;
- `tests/test_no_private_postal_address.py` — no file git tracks contains a
  mailing address.

If a company or registered agent is ever established, its address goes in
`lib/seller.ts` and the pages need no change.

## Claims deliberately not made

The privacy notice does **not** promise:

- automated data export or deletion — neither is implemented;
- a specific retention period — none is enforced by code;
- GDPR, CCPA or SOC 2 compliance — no such process exists;
- analytics behaviour — there is no analytics package on the site.

Adding any of these sentences requires implementing the thing first.

## Before billing goes live

1. ~~Resolve every row in the table above.~~ **Done** — see Resolved, above.
2. Have a lawyer review all three pages. **Still outstanding**, and it is the one
   that matters: resolving the values did not review the documents, and
   `<ReviewNotice />` says so on every page.
3. Remove `<ReviewNotice />` from `components/legal.tsx`, *after* step 2. It is a
   code change on purpose: an unreviewed document must not be able to lose its
   warning by someone editing a string.
4. Work through the live-launch checklist in [saas-setup.md](saas-setup.md).
5. Decide what fills the notices clause. Today it says no postal address is
   published, which is accurate and adequate while nothing is sold. A paid tier
   in some jurisdictions needs a servable address, and it must not be a home.
6. Close the entitlement-write concurrency blocker, or accept it explicitly:
   [stripe-entitlement-concurrency.md](stripe-entitlement-concurrency.md).

None of this is urgent while `BILLING_MODE=disabled` and nothing is purchasable.
All of it is a precondition for that changing.
