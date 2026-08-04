/**
 * Who is selling, on what terms, as supplied and approved by the owner.
 *
 * These were `<Placeholder>` markers until the owner resolved them. They live in
 * one module rather than as literals across four pages for the same reason
 * `SUPPORT_EMAIL` does: a jurisdiction or a refund policy that is right on
 * `/terms` and stale on `/founding-pilot` is worse than one that is missing,
 * because a reader cannot tell which copy governs.
 *
 * **The seller is an individual, not a company.** Nothing here may imply a
 * corporation, an LLC, a registered trademark or a fictitious-business
 * registration, because none exists. "PMVL Founding Research Pilot" is the name
 * of a service an individual offers, not a registered mark.
 *
 * These are owner-supplied facts. Do not edit them to read better, and do not
 * infer neighbouring values from them - an invented address or jurisdiction in a
 * document that reads as finished is worse than a visible blank.
 *
 * Resolving these does **not** mean the documents were reviewed by a lawyer.
 * They were not. `<LegalDraftBanner>` says so and must stay.
 */

/** Individual operator. Not a company: see the module note above. */
export const SELLER_TYPE = "individual" as const;

/** The owner's legal name, matching the identity used with the payment processor. */
export const SELLER_LEGAL_NAME = "Ao Xu";

/** How the seller is described wherever a company name would otherwise appear. */
export const SELLER_DESCRIPTION = "an individual operator";

/** The service being sold. A service name, not a registered mark. */
export const SERVICE_NAME = "PMVL Founding Research Pilot";

/**
 * The public mailing address, also the address for formal legal notices.
 *
 * One address for both, because two addresses in one agreement is an invitation
 * to serve notice at the wrong one.
 */
export const BUSINESS_MAILING_ADDRESS =
  "Apt 1201, 444 Washington Blvd, Jersey City, NJ 07310";

/** Contracting age. */
export const MINIMUM_AGE = 18;

/** Governing law. */
export const GOVERNING_JURISDICTION = "the State of New Jersey, United States";

/** Where disputes are heard. */
export const DISPUTE_VENUE =
  "the state and federal courts located in Hudson County, Jersey City, New Jersey";

/** Where the data controller is located, for the privacy notice. */
export const CONTROLLER_LOCATION = "New Jersey, United States";

/**
 * Refund and extension terms, verbatim as approved.
 *
 * Split into sentences so each page can lead with the part its reader needs -
 * the sales page wants the pre-delivery refund first - without any page
 * paraphrasing the policy into something subtly different.
 */
export const REFUND_POLICY = {
  /** The unconditional part, and the one a buyer looks for before paying. */
  beforeDelivery:
    "A customer may request a full refund before the first research digest is delivered.",
  /** What happens when PMVL fails to deliver. */
  afterDelivery:
    "After delivery begins, if PMVL fails for reasons within its control to deliver more than three scheduled business-day reports during the 30-day service term, the customer may choose either an extension equal to the missed delivery days or a prorated refund for the undelivered days.",
  /** What is explicitly not refundable. Stated plainly rather than buried. */
  notCovered:
    "No refund is provided solely because a report contains no actionable candidate, because market prices move, or because the customer experiences a trading loss.",
} as const;

/** The three sentences in reading order, for pages that print the whole policy. */
export const REFUND_POLICY_SENTENCES = [
  REFUND_POLICY.beforeDelivery,
  REFUND_POLICY.afterDelivery,
  REFUND_POLICY.notCovered,
] as const;

/** Limitation of liability, verbatim as approved. */
export const LIABILITY_CAP =
  "To the maximum extent permitted by applicable law, total liability is limited to the total amount actually paid by the customer for the Founding Pilot, currently USD 49. This limitation does not apply where liability cannot legally be limited.";

/** Retention, verbatim as approved. */
export const DATA_RETENTION_POLICY =
  "PMVL retains customer fulfilment, support, correction and dispute records for 24 months after the service term ends. Records required for tax, payment-dispute, fraud-prevention or other legal obligations may be retained for the legally required period. Records are then deleted or anonymised where reasonably possible. Stripe separately processes and retains payment information under its own policies.";

/** Controller identity for the privacy notice. */
export const DATA_CONTROLLER = `${SELLER_LEGAL_NAME}, ${SELLER_DESCRIPTION} located in ${CONTROLLER_LOCATION}.`;
