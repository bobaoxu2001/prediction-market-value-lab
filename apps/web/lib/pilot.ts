import "server-only";

/**
 * The Founding Research Pilot's terms, in one place.
 *
 * A price, a duration and a cap that appear in four sections of a sales page are
 * four chances to disagree with each other, and the one a buyer remembers is
 * whichever contradicts the receipt.
 *
 * The payment link is a *placeholder by design*. This task does not enable
 * billing: there is no Stripe API call anywhere in this feature, no secret key,
 * no webhook and no subscription. Selling the pilot means the owner creating a
 * Stripe Payment Link by hand and pasting its URL into one environment variable.
 * Until they do, the page says so and offers email instead of a broken button.
 */

/**
 * The enquiry link shown when no payment URL is configured.
 *
 * An enquiry, not a purchase: it opens the reader's mail client and charges
 * nothing. Kept next to the price so the two can never drift apart.
 */
export const PILOT_INTEREST_MAILTO =
  "mailto:ax2183@nyu.edu?subject=PMVL%20Founding%20Pilot%20interest";

/** USD, one time, not a subscription. */
export const PILOT_PRICE_USD = 49;

/** Days of daily digests, counted from first delivery rather than from payment. */
export const PILOT_DURATION_DAYS = 30;

/** Hard cap. Fulfilment is manual, and twenty is what one person can serve well. */
export const PILOT_MEMBER_CAP = 20;

/**
 * A Stripe Payment Link URL, supplied by the owner.
 *
 * Deliberately NOT a Stripe API integration:
 *
 *   - a Payment Link is created in the Stripe dashboard and is just a URL, so
 *     this application holds no secret key and makes no API call;
 *   - it therefore cannot charge anyone by accident, and nothing about it is
 *     reachable from the code paths that `BILLING_MODE` guards;
 *   - it is validated below rather than trusted, because an unset or malformed
 *     value must degrade to "not open yet" and never to a broken checkout.
 *
 * Set `PILOT_PAYMENT_LINK` to a `https://buy.stripe.com/...` URL to open sales.
 */
export function pilotPaymentLink(): string | null {
  const raw = process.env.PILOT_PAYMENT_LINK?.trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    // Only Stripe's own hosted payment-link host. A pasted mistake — or a
    // substituted value — must not turn this button into an open redirect to
    // an arbitrary payment page.
    if (url.protocol !== "https:") return null;
    if (url.hostname !== "buy.stripe.com") return null;
    return url.toString();
  } catch {
    return null;
  }
}

/** Whether the pilot can currently be bought. */
export function pilotSalesOpen(): boolean {
  return pilotPaymentLink() !== null;
}

/**
 * Seats already taken, when the owner has recorded it.
 *
 * Manual, because fulfilment is manual: there is no database of members in this
 * task. Unset means "not published", which the page renders as an honest absence
 * rather than as zero — claiming a specific number of remaining seats when
 * nobody is counting would be a fabricated scarcity signal.
 */
export function pilotSeatsTaken(): number | null {
  const raw = process.env.PILOT_SEATS_TAKEN?.trim();
  if (!raw) return null;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > PILOT_MEMBER_CAP) return null;
  return parsed;
}

export function pilotSeatsRemaining(): number | null {
  const taken = pilotSeatsTaken();
  return taken === null ? null : Math.max(0, PILOT_MEMBER_CAP - taken);
}
