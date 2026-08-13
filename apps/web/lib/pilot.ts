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

/**
 * Hard cap for the first cohort.
 *
 * Five, not twenty. The Stripe Payment Link that will sell this is capped at
 * five completed payments, and a page advertising more spots than the checkout
 * will accept is a contradiction the buyer discovers at the moment they try to
 * pay. The cap the site states and the cap the payment processor enforces have
 * to be the same number.
 *
 * Five is also what one person can genuinely review by hand for thirty
 * consecutive days without the review becoming a rubber stamp.
 */
export const PILOT_MEMBER_CAP = 5;

/**
 * The pilot is withdrawn. See `docs/adr-003-withdraw-the-pilot.md`.
 *
 * Not "unset", not "sold out" — withdrawn, because the reason is evidential
 * rather than operational. `pmvl retrodict` scores the independent estimate
 * against the venue's own price on already-settled markets, and on the first real
 * sample it came out 0.0025 Brier WORSE than the price. The pilot's premise is
 * that the digest contains information the price does not, and that premise is
 * currently unsupported by the only measurement that bears on it.
 *
 * Nobody paid, so there is nothing to unwind: the member ledger is still the
 * empty template.
 */
export const PILOT_WITHDRAWN = true;

/**
 * Why the page says the pilot is closed. Rendered verbatim, so it cannot drift
 * from the reason recorded in the ADR.
 */
export const PILOT_WITHDRAWN_REASON =
  "The pilot sold a daily research digest on the premise that our estimate " +
  "contains information the market price does not. Measured against settled " +
  "markets, it does not — our forecasts scored slightly worse than the price " +
  "itself. Selling it anyway would have been selling a premise we had just " +
  "failed to demonstrate.";

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
 * Now additionally closed at the top: `PILOT_WITHDRAWN` short-circuits before the
 * environment is read at all. Withdrawing by deleting the variable would leave
 * the decision one dashboard edit away from being reversed by someone who never
 * saw the measurement behind it; this way reopening sales is a code change with a
 * diff, which is the same reasoning ADR 002 applied to `BILLING_MODE`.
 */
export function pilotPaymentLink(): string | null {
  if (PILOT_WITHDRAWN) return null;

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
