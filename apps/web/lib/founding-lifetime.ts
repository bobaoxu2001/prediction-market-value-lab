/**
 * Public terms for the Founding Lifetime purchase-intent test.
 *
 * This is deliberately not billing configuration. The price is the proposition
 * being tested, not an amount the application can collect. Keeping the CTA as a
 * mailto link makes the boundary structural: there is no form endpoint, payment
 * session, reservation, account entitlement or promise of launch behind it.
 */

/** USD, one time, if and only if a real offer is opened later. */
export const FOUNDING_LIFETIME_PRICE_USD = 29;

/**
 * A pre-addressed email that expresses interest and does nothing else.
 *
 * No visitor data is sent to PMVL until the visitor chooses to send the draft in
 * their own mail client. The body restates the non-purchase boundary so a saved
 * copy of the email cannot be mistaken for an order or reservation.
 */
export const FOUNDING_LIFETIME_INTEREST_MAILTO =
  "mailto:ax2183@nyu.edu?subject=PMVL%20%2429%20Founding%20Lifetime%20interest" +
  "&body=I%27m%20interested%20in%20the%20proposed%20PMVL%20Founding%20Lifetime%20plan.%20I%20understand%20this%20email%20is%20not%20a%20purchase%2C%20reservation%2C%20or%20guarantee%20that%20the%20plan%20will%20launch.";
