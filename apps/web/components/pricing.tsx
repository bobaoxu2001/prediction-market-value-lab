import Link from "next/link";

import { shouldShowCheckoutUi } from "@/lib/billing/config";
import type { Entitlement } from "@/lib/billing/entitlement";

/**
 * Plans.
 *
 * Two rules govern every word below.
 *
 * The first: nothing is sold that does not exist. Pro is presented as early
 * access to a product still being finished, with the benefits that are actually
 * built named and the ones that are not left unnamed. Listing alerts, exports or
 * an API here - none of which exist - would be the ordinary way this section
 * gets written and would be a lie.
 *
 * The second: the checkout affordance follows the *server's* billing gate, not
 * the public flag. When billing is off, which is its state in production, the
 * page offers account creation and an early-access signal, and says plainly that
 * billing is not live. It never renders a button that would 503.
 */

interface Plan {
  id: "free" | "pro";
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
    "The research product as it stands today, in full. Everything the hosted deployment can currently show, it shows to everyone.",
  includes: [
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
  name: "Founding Pro",
  price: "Not yet priced",
  cadence: "early access",
  summary:
    "A paid tier is being prepared. It is not finished, so it is not being sold. What it will include is being decided against what the pipeline can actually support, not against what would be easy to advertise.",
  includes: [
    "Early access to account-tier features as they are completed",
    "A say in what gets built first, before the tier is priced",
    "The same public research, unchanged and never moved behind the paywall",
  ],
  excludes: [
    "No alerts, watchlists, exports or API exist today, and none are promised",
    "No signal service, no recommendations to act on, no execution",
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

  return (
    <div>
      {heading ? (
        <div className="max-w-2xl">
          <p className="t-label">Pricing</p>
          <Heading className="t-page-title mt-2">
            Two plans, one of them not for sale yet
          </Heading>
          <p className="t-prose mt-3">
            The public research product is free and stays free. The paid tier is
            in preparation; until it is finished and reviewed, this deployment
            cannot take a payment at all.
          </p>
        </div>
      ) : null}

      <div className="mt-8 grid gap-px overflow-hidden rounded-[3px] border border-line bg-line lg:grid-cols-2">
        <PlanCard plan={FREE} entitlement={entitlement} headingLevel={cardHeading}>
          {entitlement.signedIn ? (
            <Link href="/app" className="btn-primary">
              Open research
            </Link>
          ) : (
            <>
              <Link href="/sign-up" className="btn-primary">
                Create free account
              </Link>
              <Link href="/app" className="btn-quiet">
                Explore research
              </Link>
            </>
          )}
        </PlanCard>

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
          ) : checkoutAvailable ? (
            <CheckoutForms />
          ) : (
            <>
              <Link href="/sign-up" className="btn-primary">
                Join Pro early access
              </Link>
              <span className="chip bg-sunken text-ink-muted">
                Billing not yet live
              </span>
            </>
          )}
        </PlanCard>
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
            The server rejects checkout requests on this deployment regardless of
            what the interface offers. Activating billing requires a server-side
            billing mode, valid test credentials, allowlisted price IDs,
            completed legal review and the owner&apos;s approval.
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
        before subscribing. Prediction markets involve the risk of loss, and
        PMVL&apos;s estimates can be wrong.
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
