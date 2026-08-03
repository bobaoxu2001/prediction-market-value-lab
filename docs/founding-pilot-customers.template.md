# Founding Pilot — member ledger (TEMPLATE)

**Copy this to `private/founding-pilot-customers.md` before entering anything.**
`private/` is gitignored. This template is the only copy that belongs in the
repository, and it must never acquire a real email address, payment identifier or
delivery note.

Customer records are personal data. Once committed, git history keeps them even
after deletion, which makes an erasure request impossible to honour.

---

## Rules

1. **Stripe is the only proof of payment.** A visit to the confirmation page is
   not payment. Open the Stripe Dashboard and confirm the payment shows as
   completed before recording anything or sending anything.
2. **One row per payment, not per person.** A second purchase by the same person
   is a second row with its own service window.
3. **Record before delivering.** The row exists first; the welcome email second.
4. **Never delete a row.** Corrections are appended to the correction log, so the
   history of what was sent stays auditable.
5. **Stop at five.** The Stripe Payment Link is capped at five completed
   payments. If a sixth payment somehow exists, refund it rather than serving it.

---

## Members

| # | Stripe payment ID | Customer email | Paid at (UTC) | Amount | Service start | Service end | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `pi_…` | | | USD 49.00 | | | |
| 2 | `pi_…` | | | USD 49.00 | | | |
| 3 | `pi_…` | | | USD 49.00 | | | |
| 4 | `pi_…` | | | USD 49.00 | | | |
| 5 | `pi_…` | | | USD 49.00 | | | |

`Status`: `awaiting welcome` → `active` → `completed`, or `refunded`.

Service start is the date of the **first delivery**, not the payment date, and
service end is 30 delivery days after it. Paying on a Friday night does not
consume a delivery day before anything has been delivered.

---

## Delivery log

One row per report actually sent. A generated report that a person did not
approve and send does not belong here.

| Date (UTC) | Member # | Kind | Snapshot ID | Gate result | Actionable | Watchlist | Sent at | Sent by |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | daily / weekly | | allowed / blocked | | | | |

A **blocked** report is a valid delivery and is logged the same way. So is a
report with zero actionable candidates. Both are the product behaving correctly.

---

## Correction log

Anything sent that was wrong, and what was done about it. A correction is sent to
every member who received the original, not only the one who noticed.

| Date (UTC) | Affects | What was wrong | Correction sent | Members notified |
| --- | --- | --- | --- | --- |
| | | | | |

---

## Refunds and extensions

| Date (UTC) | Member # | Refund or extension | Reason | Stripe refund ID | New service end |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

Extensions exist because the service can legitimately fail to deliver: if the
Snapshot is stale for a run of days, members paid for research days they did not
get. Extend the service end rather than refunding, unless the member asks.

---

## The manual workflow

```
Stripe Dashboard shows a COMPLETED payment
  └─> record the member privately (this file)
        └─> owner sends the welcome email
              └─> daily: fresh pipeline run produces a candidate report
                    └─> human reviews the report
                          └─> owner emails it
                                └─> weekly: outcome review
                                      └─> day 30: service completion note
```

No step is automated, and no step may be described to a member as automated.
There is no webhook, no scheduled mailer and no entitlement system. A scheduled
job produces a draft; a person decides whether it is sent.
