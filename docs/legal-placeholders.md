# Legal placeholders — owner input required

`/terms`, `/privacy` and `/risk-disclosure` are **drafts written by an engineer
to describe what the software actually does.** They have not been reviewed or
approved by a lawyer, and each page says so at the top.

Every value below was left as a visible placeholder rather than invented.
Guessing a legal entity name, a jurisdiction or a refund policy would produce a
document that reads as finished and is wrong — which is worse than one that
obviously is not.

## How placeholders render

`components/legal.tsx` exports `<Placeholder>`, which renders as a highlighted
`[VALUE — OWNER INPUT REQUIRED]`. `apps/web/tests/pages.test.tsx` asserts the
legal pages still contain at least one, so a partially filled document cannot
quietly look complete.

## Outstanding decisions

| Placeholder | Where | What is needed |
| --- | --- | --- |
| `LEGAL ENTITY NAME` | Terms §1, §7 | The registered company or sole trader operating the service |
| `REGISTERED ADDRESS` | Terms §1 | Registered office address |
| `PRODUCT AND SERVICE NAME AS REGISTERED` | Terms §1 | Final product name, if it differs from "PMVL" |
| `MINIMUM AGE` | Terms §2 | Contracting age in the target jurisdictions |
| `BILLING CURRENCY` | Terms §5 | Currency subscriptions are sold in |
| `MONTHLY PRICE` | Terms §5, Stripe | Monthly amount |
| `ANNUAL PRICE` | Terms §5, Stripe | Annual amount |
| `REFUND POLICY` | Terms §6 | Refund terms, including statutory rights where they apply |
| `LIABILITY CAP` | Terms §8 | Limitation of liability, where lawful |
| `GOVERNING JURISDICTION` | Terms §11 | Governing law |
| `DISPUTE VENUE` | Terms §11 | Courts for disputes |
| `SUPPORT EMAIL` | Terms §13, Privacy, footer | A monitored address |
| `LEGAL NOTICE ADDRESS` | Terms §13 | Address for formal notices |
| `DATA RETENTION POLICY` | Privacy | How long account, billing and log data are kept |
| `DATA CONTROLLER AND JURISDICTION` | Privacy | Controller identity and supervisory authority |

## Claims deliberately not made

The privacy notice does **not** promise:

- automated data export or deletion — neither is implemented;
- a specific retention period — none is enforced by code;
- GDPR, CCPA or SOC 2 compliance — no such process exists;
- analytics behaviour — there is no analytics package on the site.

Adding any of these sentences requires implementing the thing first.

## Before billing goes live

1. Resolve every row in the table above.
2. Have a lawyer review all three pages.
3. Remove `<ReviewNotice />` from `components/legal.tsx`. It is a code change on
   purpose: an unreviewed document must not be able to lose its warning by
   someone editing a string.
4. Work through the live-launch checklist in `docs/saas-setup.md`.
