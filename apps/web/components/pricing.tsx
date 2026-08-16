import Link from "next/link";

import { shouldShowCheckoutUi } from "@/lib/billing/config";
import { isAuthConfigured } from "@/lib/auth-server";
import type { Entitlement } from "@/lib/billing/entitlement";
import {
  FOUNDING_LIFETIME_INTEREST_MAILTO,
  FOUNDING_LIFETIME_PRICE_USD,
} from "@/lib/founding-lifetime";

/**
 * Plans.
 *
 * Three rules govern every word below.
 *
 * The first: nothing is sold that does not exist. Pro is presented as early
 * access to a product still being finished, with the benefits that are actually
 * built named and the ones that are not left unnamed. Listing alerts, exports or
 * an API here - none of which exist - would be the ordinary way this section
 * gets written and would be a lie.
 *
 * The second: the $29 founding card is a demand test, not a sale. Its only
 * outbound action is a pre-addressed email draft and every proposed capability
 * is labelled as proposed.
 *
 * The third: the checkout affordance follows the *server's* billing gate, not
 * the public flag. When billing is off, which is its state in production, the
 * page offers account creation and an early-access signal, and says plainly that
 * billing is not live. It never renders a button that would 503.
 */

interface Plan {
  id: "free" | "founding" | "pro";
  name: string;
  price: string;
  cadence: string;
  summary: string;
  includes: readonly string[];
  excludes?: readonly string[];
}

const FREE: Plan = {
  id: "free",
  name: "Free",
  price: "$0",
  cadence: "no card required",
  summary:
    "The research product as it stands today, in full — led by the execution-cost engine, which is the product rather than the consolation prize. Everything the hosted deployment can currently show, it shows to everyone.",
  includes: [
    // Listed first because it is the only surface that answers on every visit:
    // execution cost needs no probability estimate, so the independence rule
    // that empties the others cannot empty it. ADR 003 makes this the entry
    // point rather than the page a reader lands on when the list is empty.
    "Execution cost for any contract, at any size, with the break-even it implies",
    "The research briefing: what is actionable, what is not, and why",
    "Market discovery across both venues with executable prices and depth",
    "Actionable and Diagnostics arbitrage views, labelled separately",
    "Full methodology, including the formulas and the admission rules",
    "Track record and backtest transparency, winners and losers alike",
    "System page: snapshot timing, pipeline state and data-source list",
  ],
};

const PRO: Plan = {
  id: "pro",
  name: "Data access",
  price: "Not yet priced",
  cadence: "not open",
  summary:
    "If a paid tier ever opens it will sell data, not opinion: normalised cross-venue markets with cost-adjusted execution prices, over an API. Everything in it is checkable against the venues' own endpoints on the day you buy it, so none of it asks you to trust a forecast we have not validated.",
  includes: [
    "Normalised markets across both venues, with the settlement rules attached",
    "Executable ask ladders and depth, not midpoints or last trades",
    "The full cost stack per contract and size: fees, rounding, spread, transfer",
    "The same public research, unchanged and never moved behind the paywall",
  ],
  excludes: [
    // Named because the obvious thing to sell here is the thing we cannot
    // currently justify selling. Measured against settled markets, the
    // independent estimate scored slightly worse than the market's own price.
    "No signals, no ranked picks, no digest — the paid tier is not the forecast",
    "None of it is built or priced yet, and none of it is being sold today",
  ],
};

const FOUNDING: Plan = {
  id: "founding",
  name: "Founding Lifetime",
  price: `$${FOUNDING_LIFETIME_PRICE_USD}`,
  cadence: "proposed one-time founding price",
  summary:
    "A low-cost, local-first toolkit for people who repeatedly compare how order size changes entry cost. This page is testing demand before the plan is built or sold; expressing interest does not buy, reserve or unlock anything.",
  includes: [
    "Everything in Free, which stays free",
    "Proposed: save reusable cost assumptions and order templates locally",
    "Proposed: compare local cost history and export your own results",
    "Proposed: advanced local scenarios and local watchlist tools",
    "Proposed: future low-marginal-cost, local-first Pro updates",
  ],
  excludes: [
    "Not included: cloud sync, alerts, API access, AI usage, server-hosted history or human support",
    "Not built, not for sale, no card collected and no lifetime access promised today",
  ],
};

export function PricingPlans({
  entitlement,
  heading = true,
  headingLevel = "h2",
}: {
  entitlement: Entitlement;
  heading?: boolean;
  /**
   * `h1` when this block IS the page (/pricing), `h2` when it is a section of
   * one (the homepage). /pricing previously had no `h1` at all, which leaves a
   * screen-reader user navigating by heading with no page title on a primary
   * conversion page.
   */
  headingLevel?: "h1" | "h2";
}) {
  const Heading = headingLevel;
  // The plan titles sit one level under the block's own heading. Pinning them to
  // h3 while the block became an h1 skipped h2 entirely, which is its own
  // structural defect - a screen-reader user stepping through headings would be
  // told a level had been missed.
  const cardHeading = headingLevel === "h1" ? "h2" : "h3";
  const checkoutAvailable = shouldShowCheckoutUi();
  const accountsEnabled = isAuthConfigured();

  return (
    <div>
      {heading ? (
        <div className="max-w-2xl">
          <p className="t-label">Pricing</p>
          <Heading className="t-page-title mt-2">Free now. Test the $29 idea.</Heading>
          <p className="t-prose mt-3">
            The working product is free and stays free. Founding Lifetime is a
            proposed local-first plan, shown now to measure demand before it is
            built. The interest link opens an email draft; it does not take a
            payment, reserve a place or create lifetime access.
          </p>
        </div>
      ) : null}

      <div
        className={`mt-8 grid gap-px overflow-hidden rounded-[3px] border border-line bg-line ${
          checkoutAvailable ? "lg:grid-cols-3" : "lg:grid-cols-2"
        }`}
      >
        <PlanCard plan={FREE} entitlement={entitlement} headingLevel={cardHeading}>
          {entitlement.signedIn ? (
            <Link href="/app" className="btn-primary">
              Open research
            </Link>
          ) : accountsEnabled ? (
            <>
              <Link href="/sign-up" className="btn-primary">
                Create free account
              </Link>
              <Link href="/app" className="btn-quiet">
                Explore research
              </Link>
            </>
          ) : (
            // The free tier IS the public research, and it has never needed an
            // account. With registration unavailable, offering it as the primary
            // action on the free plan would be both broken and beside the point.
            <>
              <Link href="/app" className="btn-primary">
                Explore research
              </Link>
              <span className="chip bg-sunken text-ink-muted">
                No account needed
              </span>
            </>
          )}
        </PlanCard>

        <PlanCard plan={FOUNDING} entitlement={entitlement} headingLevel={cardHeading}>
          <a
            href={FOUNDING_LIFETIME_INTEREST_MAILTO}
            className="btn-primary"
            data-pmvl-funnel="founding_offer_intent"
            data-pmvl-placement="pricing"
          >
            I&apos;d consider the $29 plan
          </a>
          <Link href="/founding-lifetime" className="btn-quiet">
            See the proposed boundary
          </Link>
          <span className="chip bg-sunken text-ink-muted">
            Interest only · no payment
          </span>
        </PlanCard>

        {checkoutAvailable ? (
          <PlanCard plan={PRO} entitlement={entitlement} headingLevel={cardHeading}>
            {entitlement.hasLiveSubscription ? (
              // Already subscribed - including past-due, where a second checkout
              // would add an invoice rather than fix the failed one. The server
              // refuses this case too; showing the same answer here keeps the
              // page from offering something the handler will reject.
              <>
                <Link href="/account/billing" className="btn-primary">
                  Manage billing
                </Link>
                <span className="chip bg-sunken text-ink-muted">
                  Subscription already active
                </span>
              </>
            ) : (
              <CheckoutForms />
            )}
          </PlanCard>
        ) : null}
      </div>

      <p className="mt-6 t-meta">
        {checkoutAvailable ? (
          <>
            <strong className="text-ink-muted">
              Stripe test mode.
            </strong>{" "}
            This deployment is configured for test-mode checkout only. No real
            card is charged and no live Stripe product is touched. Use Stripe&apos;s
            published test cards.
          </>
        ) : (
          <>
            <strong className="text-ink-muted">Billing is disabled.</strong>{" "}
            The $29 link never enters billing: it only opens an email draft. If
            you send it, PMVL receives your email address and message for product
            research. It is not an order, reservation or promise that the plan
            will launch.
          </>
        )}{" "}
        Read the{" "}
        <Link href="/risk-disclosure" className="underline underline-offset-2">
          risk disclosure
        </Link>{" "}
        and the{" "}
        <Link href="/terms" className="underline underline-offset-2">
          terms
        </Link>{" "}
        before using the product or expressing interest. Prediction markets
        involve the risk of loss, and PMVL&apos;s estimates can be wrong.
      </p>
    </div>
  );
}

function PlanCard({
  plan,
  entitlement,
  headingLevel,
  children,
}: {
  plan: Plan;
  entitlement: Entitlement;
  headingLevel: "h2" | "h3";
  children: React.ReactNode;
}) {
  const CardHeading = headingLevel;
  const current =
    (plan.id === "free" && entitlement.signedIn && !entitlement.isPro) ||
    (plan.id === "pro" && entitlement.isPro);

  return (
    <div className="flex flex-col bg-base p-6 sm:p-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <CardHeading className="t-section-title text-[1.25rem]">
          {plan.name}
        </CardHeading>
        {current ? (
          <span className="chip bg-edge/15 text-edge">Current plan</span>
        ) : null}
      </div>
      <p className="num mt-3 text-2xl font-semibold">{plan.price}</p>
      <p className="t-meta mt-1">{plan.cadence}</p>
      <p className="t-prose mt-4">{plan.summary}</p>

      <ul className="mt-5 flex-1 space-y-2">
        {plan.includes.map((item) => (
          <li key={item} className="flex gap-2 text-sm text-ink-muted">
            <span aria-hidden="true" className="mt-[0.45rem] h-px w-3 shrink-0 bg-line-strong" />
            <span>{item}</span>
          </li>
        ))}
        {plan.excludes?.map((item) => (
          <li key={item} className="flex gap-2 text-sm text-ink-faint">
            <span aria-hidden="true" className="mt-[0.45rem] h-px w-3 shrink-0 bg-risk/50" />
            <span>{item}</span>
          </li>
        ))}
      </ul>

      <div className="mt-6 flex flex-wrap items-center gap-3">{children}</div>
    </div>
  );
}

/**
 * Test-mode checkout entry points.
 *
 * Plain forms that POST to the server. No client JavaScript decides anything:
 * the plan is a fixed, server-allowlisted identifier in a hidden field, the
 * server ignores anything else it is sent, and the response is a 303 to Stripe.
 * A form also gets keyboard operation, focus handling and the disabled state
 * from the platform rather than from a handler that has to remember them.
 */
function CheckoutForms() {
  return (
    <>
      {(
        [
          { plan: "pro_monthly", label: "Start monthly test checkout" },
          { plan: "pro_annual", label: "Start annual test checkout" },
        ] as const
      ).map(({ plan, label }, index) => (
        <form key={plan} action="/api/billing/checkout" method="post">
          <input type="hidden" name="plan" value={plan} />
          <input type="hidden" name="returnTo" value="/account/billing" />
          <button type="submit" className={index === 0 ? "btn-primary" : "btn-quiet"}>
            {label}
          </button>
        </form>
      ))}
    </>
  );
}
