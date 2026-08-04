// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

/**
 * Page rendering.
 *
 * The pages are async server components, so they are awaited and the returned
 * element is rendered. That exercises the real data path - including the
 * degraded state, which is the one most likely to rot unnoticed because it only
 * appears when something is already broken.
 */

import {
  PILOT_DURATION_DAYS,
  PILOT_MEMBER_CAP,
  PILOT_PRICE_USD,
} from "@/lib/pilot";
import {
  BUSINESS_MAILING_ADDRESS,
  DATA_CONTROLLER,
  DATA_RETENTION_POLICY,
  DISPUTE_VENUE,
  GOVERNING_JURISDICTION,
  LIABILITY_CAP,
  MINIMUM_AGE,
  REFUND_POLICY_SENTENCES,
  SELLER_LEGAL_NAME,
} from "@/lib/seller";

/** The three documents a buyer is asked to accept. */
const LEGAL_PAGES = [
  "@/app/(site)/terms/page",
  "@/app/(site)/privacy/page",
  "@/app/(site)/risk-disclosure/page",
] as const;

/** Render a page (sync or async server component) and return its visible text. */
async function renderText(specifier: string): Promise<string> {
  const { default: Page } = await import(specifier);
  const element = specifier.includes("founding-pilot") ? await Page() : <Page />;
  const text = render(element).container.textContent ?? "";
  cleanup();
  return text;
}

/**
 * Occurrences of `pattern` that are asserted rather than denied.
 *
 * A word-presence check cannot tell "this is a subscription" from "this is not a
 * subscription", and the second is a sentence these pages *must* contain.
 *
 * The negation has to be checked next to the match, not anywhere in the
 * sentence. Dropping every sentence containing "not" would skip
 * "Your subscription renews monthly and cannot be cancelled" - a real violation
 * hidden by an unrelated "cannot" later in the same sentence. So only the words
 * immediately before each match are inspected.
 */
function unnegatedMatches(text: string, pattern: RegExp): string[] {
  const global = new RegExp(pattern.source, `${pattern.flags.replace("g", "")}g`);
  const found: string[] = [];
  for (const match of text.matchAll(global)) {
    const before = text.slice(Math.max(0, match.index - 44), match.index);
    // "no automatic renewal", "not a subscription", "never renews", and the
    // contrastive form - "a service rather than a registered trademark".
    const negation = /(\b(no|not|never|nothing|neither|nor|without)\b|\b(rather|other) than\b)[^.!?]*$/i;
    if (negation.test(before)) continue;
    found.push(text.slice(Math.max(0, match.index - 70), match.index + 40).trim());
  }
  return found;
}

const redirectCalls: string[] = [];
vi.mock("next/navigation", () => ({
  redirect: (url: string) => {
    redirectCalls.push(url);
    throw new Error(`NEXT_REDIRECT:${url}`);
  },
}));

vi.mock("next/image", () => ({
  default: ({ src, alt, ...rest }: { src: string; alt: string }) =>
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={alt} {...rest} />,
}));

/**
 * Whether an auth provider is configured, switchable per test.
 *
 * `vi.hoisted` because `vi.mock` factories are lifted above the imports: a plain
 * outer binding would not exist yet when the factory is evaluated.
 */
const authState = vi.hoisted(() => ({ configured: false }));

vi.mock("@/lib/auth-server", () => ({
  isAuthConfigured: () => authState.configured,
  getCurrentUser: vi.fn(async () => null),
  getCurrentUserId: vi.fn(async () => null),
  getPrivateMetadata: vi.fn(async () => ({})),
  mergePrivateMetadata: vi.fn(async () => true),
  userExists: vi.fn(async () => true),
}));

const SYSTEM = {
  data: {
    snapshot_mode: true,
    runtime_mode: "read_only_snapshot",
    model_version: "ensemble-v1.0.0",
    trading_execution_enabled: false,
    row_counts: { markets: 2057 },
    freshest_quote_observed_at: "2026-07-31T08:56:07.827120Z",
    jobs: [
      { job_name: "ingest", status: "success" },
      { job_name: "rank", status: "success" },
    ],
    pipeline: { public_serving_mode: "Read-only snapshot" },
    snapshot_timing: { freshest_quote_observed_at: "2026-07-31T08:56:07.827120Z" },
  },
};

function mockApi(handler: (path: string) => unknown | null) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = new URL(url).pathname + new URL(url).search;
      const body = handler(path);
      if (body === null) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => body };
    }),
  );
}

beforeEach(() => {
  redirectCalls.length = 0;
  // The free-Beta default: no Clerk credentials, so no account can be created.
  authState.configured = false;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("marketing homepage", () => {
  async function renderHome(query: Record<string, string> = {}) {
    const { default: HomePage } = await import("@/app/(site)/page");
    const element = await HomePage({ searchParams: Promise.resolve(query) });
    return render(element);
  }

  it("renders the snapshot state the API actually reports", async () => {
    mockApi((path) => {
      if (path.startsWith("/system")) return SYSTEM;
      if (path.startsWith("/opportunities/watchlist")) return { data: [{}, {}, {}] };
      if (path.startsWith("/opportunities")) return { data: [] };
      return null;
    });
    await renderHome();

    // The counts are the API's, not literals in the page.
    expect(screen.getByText("2,057")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getAllByText("2/2").length).toBeGreaterThan(0);
    // Zero actionable is reported as the normal result, not hidden.
    expect(screen.getByText(/nothing cleared every gate/i)).toBeTruthy();
    // The freshness caveat is present and names the frozen snapshot.
    expect(screen.getAllByText(/frozen research snapshot/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/trading execution disabled/i).length).toBeGreaterThan(0);
  });

  it("renders a calm degraded state when the API is unavailable", async () => {
    // The failure this guards: filling the section with a cached or plausible
    // number, which is exactly the dishonesty the product is built against.
    mockApi(() => null);
    await renderHome();

    expect(screen.getByText(/figures unavailable/i)).toBeTruthy();
    expect(screen.getByText(/could not be reached/i)).toBeTruthy();
    expect(screen.queryByText("2,057")).toBeNull();
    // The page still functions as a funnel: the hero, the Free plan card and
    // the closing call to action all still route to the research.
    expect(
      screen.getAllByRole("link", { name: /explore research/i }).length,
    ).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("link", { name: /check system status/i })).toBeTruthy();
  });

  it("distinguishes an endpoint that failed from one that answered zero", async () => {
    mockApi((path) => {
      if (path.startsWith("/system")) return SYSTEM;
      return null; // opportunities and watchlist both fail
    });
    const { container } = await renderHome();
    const text = container.textContent ?? "";
    // A failed count renders as an em dash, never as 0.
    expect(text).toContain("—");
  });

  it("forwards an old research deep link to /app with its query intact", async () => {
    mockApi((path) => (path.startsWith("/system") ? SYSTEM : { data: [] }));
    await expect(renderHome({ horizon: "7d", mode: "demo" })).rejects.toThrow(
      /NEXT_REDIRECT/,
    );
    expect(redirectCalls[0]).toBe("/app?horizon=7d&mode=demo");
  });

  it("does not forward a bare homepage request", async () => {
    mockApi((path) => (path.startsWith("/system") ? SYSTEM : { data: [] }));
    await renderHome();
    expect(redirectCalls).toEqual([]);
  });

  it("offers no account call-to-action while accounts are unavailable", async () => {
    // The free-Beta shape. Every "Create free account" / "Join Pro early access"
    // routed to /sign-up, which renders "Accounts are not enabled here" — a
    // primary call to action leading to a page that says it cannot be done.
    // A visitor must not be led to believe registration currently works.
    mockApi((path) => (path.startsWith("/system") ? SYSTEM : { data: [] }));
    const { container } = await renderHome();

    expect(container.querySelector('a[href="/sign-up"]')).toBeNull();
    expect(container.querySelector('a[href="/sign-in"]')).toBeNull();
    expect((container.textContent ?? "").toLowerCase()).not.toContain(
      "create free account",
    );

    // The research is still one click away — that is the whole proposition.
    expect(
      screen.getAllByRole("link", { name: /explore research/i }).length,
    ).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/accounts coming soon/i).length).toBeGreaterThan(0);
  });

  it("makes no forbidden performance claim", async () => {
    mockApi((path) => (path.startsWith("/system") ? SYSTEM : { data: [] }));
    const { container } = await renderHome();
    const text = (container.textContent ?? "").toLowerCase();
    for (const phrase of [
      "guaranteed return",
      "winning trade",
      "accurate profit",
      "risk-free",
      "sure opportunit",
      "institutional secret",
    ]) {
      expect(text).not.toContain(phrase);
    }
    // "beat the market" appears only inside a rejection of the claim.
    if (text.includes("beat the market")) {
      expect(text).toContain("does not claim");
    }
  });

  it("labels every product screenshot with descriptive alt text", async () => {
    mockApi((path) => (path.startsWith("/system") ? SYSTEM : { data: [] }));
    const { container } = await renderHome();
    const images = [...container.querySelectorAll("img")];
    expect(images.length).toBe(6);
    for (const image of images) {
      const alt = image.getAttribute("alt") ?? "";
      expect(alt.length).toBeGreaterThan(40);
    }
  });
});

describe("pricing", () => {
  async function renderPricing() {
    const { default: PricingPage } = await import("@/app/(site)/pricing/page");
    return render(await PricingPage());
  }

  it("offers nothing to click on Pro while accounts and billing are both off", async () => {
    // With neither Clerk nor Stripe configured — the free-Beta state — there is
    // no account to register and no tier to pay for. Deliberately not a
    // disabled-looking button either: a control that looks pressable still
    // implies the thing nearly works.
    const { container } = await renderPricing();
    expect(screen.getAllByText(/coming soon/i).length).toBeGreaterThan(0);
    expect(container.querySelector('a[href="/sign-up"]')).toBeNull();
    expect(screen.queryByRole("button", { name: /test checkout/i })).toBeNull();
    // The free tier is the public research, and it needs no account.
    expect(screen.getAllByRole("link", { name: /explore research/i }).length)
      .toBeGreaterThan(0);
    expect(screen.getAllByText(/no account needed/i).length).toBeGreaterThan(0);
  });

  it("still offers early access once accounts exist but billing does not", async () => {
    // The intended next state: Clerk configured, Stripe not. The early-access
    // path must come back on its own rather than needing another edit.
    authState.configured = true;
    const { PricingPlans } = await import("@/components/pricing");
    render(
      <PricingPlans
        entitlement={{
          state: "free",
          signedIn: false,
          user: null,
          isPro: false,
          plan: null,
          stripeCustomerId: null,
          stripeSubscriptionId: null,
          currentPeriodEnd: null,
          cancelAtPeriodEnd: false,
          billingDisabled: true,
          hasLiveSubscription: false,
        }}
      />,
    );
    expect(screen.getByRole("link", { name: /join pro early access/i })).toBeTruthy();
    expect(screen.getAllByText(/billing not yet live/i).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /test checkout/i })).toBeNull();
  });

  it("links the risk disclosure from the checkout entry point", async () => {
    // Requirement: risk disclosure is linked from pricing and checkout entry.
    const { container } = await renderPricing();
    const links = [...container.querySelectorAll('a[href="/risk-disclosure"]')];
    expect(links.length).toBeGreaterThan(0);
  });

  it.each([
    ["active", true],
    ["trialing", true],
    ["past_due", false],
  ])(
    "offers management rather than a second checkout to a %s subscriber",
    async (status, isPro) => {
      // The page must not offer what the handler will reject with a 409. The
      // past-due row is the one the earlier `!isPro` check got wrong: not Pro,
      // but a second checkout would bill them twice instead of repairing the
      // failed payment.
      process.env.BILLING_MODE = "test";
      process.env.NEXT_PUBLIC_BILLING_ENABLED = "true";
      process.env.STRIPE_SECRET_KEY = "sk_test_not_a_real_key";
      process.env.STRIPE_WEBHOOK_SECRET = "whsec_not_a_real_secret";
      process.env.STRIPE_PRO_MONTHLY_PRICE_ID = "price_monthly_test";
      process.env.STRIPE_PRO_ANNUAL_PRICE_ID = "price_annual_test";

      const { PricingPlans } = await import("@/components/pricing");
      const { isLiveSubscriptionStatus } = await import("@/lib/billing/entitlement");

      render(
        <PricingPlans
          entitlement={{
            state: isPro ? "pro_active" : "pro_past_due",
            signedIn: true,
            user: { id: "u", email: "a@example.com", name: null, createdAt: 1 },
            isPro,
            plan: "pro_monthly",
            stripeCustomerId: "cus_1",
            stripeSubscriptionId: "sub_1",
            currentPeriodEnd: null,
            cancelAtPeriodEnd: false,
            billingDisabled: false,
            hasLiveSubscription: isLiveSubscriptionStatus(status),
          }}
        />,
      );

      expect(screen.getByRole("link", { name: /manage billing/i })).toBeTruthy();
      expect(screen.queryByRole("button", { name: /test checkout/i })).toBeNull();
    },
  );

  it("promises no feature that does not exist", async () => {
    const { container } = await renderPricing();
    const text = (container.textContent ?? "").toLowerCase();
    // The Pro card names these only to say they do not exist.
    expect(text).toContain("no alerts, watchlists, exports or api exist today");
  });
});

describe("legal pages", () => {
  it.each([
    ["terms", "@/app/(site)/terms/page"],
    ["privacy", "@/app/(site)/privacy/page"],
    ["risk disclosure", "@/app/(site)/risk-disclosure/page"],
  ])("renders %s with its unreviewed-draft notice", async (_name, specifier) => {
    const { default: Page } = await import(specifier);
    const { container } = render(<Page />);
    expect(within(container).getByText(/not reviewed by counsel/i)).toBeTruthy();
    expect((container.textContent ?? "").length).toBeGreaterThan(1500);
  });

  it("states the risk disclosure's required facts", async () => {
    const { default: Page } = await import("@/app/(site)/risk-disclosure/page");
    const { container } = render(<Page />);
    const text = (container.textContent ?? "").toLowerCase();
    for (const claim of [
      "not investment advice, legal advice, tax advice or financial advice",
      "prediction markets involve the risk of loss",
      "estimates are model output",
      "prices and quotes may be stale",
      "liquidity may be insufficient",
      "settlement rules",
      "past results do not guarantee future results",
      "backtest results, demo datasets and guided walkthroughs on this site are simulations",
      "places no orders",
    ]) {
      expect(text, `risk disclosure must state: ${claim}`).toContain(claim);
    }
  });

  it("shows the confirmed support address as a usable mailto link", async () => {
    // The one legal-page value the owner has resolved. It is a single constant
    // rather than three literals: a contact address that is wrong in one of
    // three places is worse than one that is missing, because a reader cannot
    // tell which copy is current.
    const { SUPPORT_EMAIL } = await import("@/lib/site");
    for (const specifier of [
      "@/app/(site)/terms/page",
      "@/app/(site)/privacy/page",
      "@/app/(site)/risk-disclosure/page",
    ]) {
      const { default: Page } = await import(specifier);
      const { container } = render(<Page />);
      const link = container.querySelector(`a[href="mailto:${SUPPORT_EMAIL}"]`);
      expect(link, `${specifier} must link the support address`).toBeTruthy();
      expect(container.textContent).toContain(SUPPORT_EMAIL);
      cleanup();
    }
  });

  it("the claim detector catches assertions and ignores denials", () => {
    // The two checks below are only worth anything if this helper actually
    // fires. A negation rule loose enough to excuse everything would let the
    // suite pass while the pages said whatever they liked.
    const pattern = /\bsubscription\b/i;
    expect(unnegatedMatches("This is not a subscription.", pattern)).toEqual([]);
    expect(unnegatedMatches("One time, never a subscription.", pattern)).toEqual([]);
    expect(
      unnegatedMatches("A service rather than a subscription.", pattern),
    ).toEqual([]);
    expect(unnegatedMatches("Your subscription renews.", pattern)).toHaveLength(1);
    // The case a whole-sentence filter would have missed: a real claim sharing a
    // sentence with an unrelated negation.
    expect(
      unnegatedMatches("Your subscription renews and cannot be cancelled.", pattern),
    ).toHaveLength(1);
    // A negation belonging to the previous sentence must not excuse this one.
    expect(
      unnegatedMatches("Nothing renews. Your subscription is billed again.", pattern),
    ).toHaveLength(1);
  });

  it("has no unresolved owner placeholder left anywhere", async () => {
    // The owner supplied every outstanding value, so the inverse of the old
    // assertion now holds. This is the check that has to survive: a document
    // going to a paying customer must not contain a bracketed blank, and the
    // previous version of this test would have passed happily while one did.
    for (const specifier of LEGAL_PAGES) {
      const { default: Page } = await import(specifier);
      const { container } = render(<Page />);
      expect(container.querySelectorAll("mark").length, specifier).toBe(0);
      expect(container.textContent, specifier).not.toContain("OWNER INPUT REQUIRED");
      cleanup();
    }
  });

  it("keeps the counsel-review warning", async () => {
    // Resolving the values is not legal review, and nothing in this change made
    // it so. The banner is the only thing telling a reader that, so it outranks
    // every value that was just filled in.
    for (const specifier of LEGAL_PAGES) {
      const { default: Page } = await import(specifier);
      const { container } = render(<Page />);
      expect(container.textContent, specifier).toMatch(/not been reviewed .{0,40}by a lawyer/i);
      cleanup();
    }
  });

  it("names an individual seller and claims no company", async () => {
    // The seller is a person. A stray "LLC", "Inc" or registered-trademark claim
    // would misrepresent who the buyer is contracting with, and is the sort of
    // thing a template edit reintroduces silently.
    //
    // Denials are not claims. "There is no corporation, limited liability
    // company or partnership behind it" contains the words and is precisely the
    // sentence that should be there, so negated sentences are excluded before
    // the check rather than the words being allowed everywhere.
    const forbidden =
      /\b(LLC|L\.L\.C\.|Inc\.?|Incorporated|Corporation|Corp\.?|GmbH|Ltd\.?|limited liability|registered trademark)\b|®/i;
    for (const specifier of [...LEGAL_PAGES, "@/app/(site)/founding-pilot/page"]) {
      const claims = unnegatedMatches(await renderText(specifier), forbidden);
      expect(claims, `${specifier} claims a company exists`).toEqual([]);
    }
  });

  it("renders the resolved seller facts on the pages that need them", async () => {
    const { default: Terms } = await import("@/app/(site)/terms/page");
    const terms = render(<Terms />).container.textContent ?? "";
    expect(terms).toContain(SELLER_LEGAL_NAME);
    expect(terms).toContain(BUSINESS_MAILING_ADDRESS);
    expect(terms).toContain(GOVERNING_JURISDICTION);
    expect(terms).toContain(DISPUTE_VENUE);
    expect(terms).toContain(LIABILITY_CAP);
    expect(terms).toContain(String(MINIMUM_AGE));
    for (const sentence of REFUND_POLICY_SENTENCES) {
      expect(terms, "the refund policy must appear in full").toContain(sentence);
    }
    cleanup();

    const { default: Privacy } = await import("@/app/(site)/privacy/page");
    const privacy = render(<Privacy />).container.textContent ?? "";
    expect(privacy).toContain(DATA_RETENTION_POLICY);
    expect(privacy).toContain(DATA_CONTROLLER);
    cleanup();
  });

  it("shows the refund policy on the sales page before the payment CTA", async () => {
    // Order matters, not just presence. A refund policy a buyer only meets after
    // paying is a policy written for the seller.
    const { default: Page } = await import("@/app/(site)/founding-pilot/page");
    const { container } = render(await Page());
    const text = container.textContent ?? "";
    for (const sentence of REFUND_POLICY_SENTENCES) {
      expect(text).toContain(sentence);
    }
    const cta = container.querySelector("a.btn-primary");
    expect(cta).toBeTruthy();
    const policyNode = [...container.querySelectorAll("li")].find((li) =>
      (li.textContent ?? "").includes(REFUND_POLICY_SENTENCES[0]),
    );
    expect(policyNode).toBeTruthy();
    // DOCUMENT_POSITION_FOLLOWING means the CTA comes after the policy.
    expect(
      policyNode!.compareDocumentPosition(cta!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    cleanup();
  });

  it("states the commercial terms identically on the sales page and the terms", async () => {
    const { default: Pilot } = await import("@/app/(site)/founding-pilot/page");
    const pilot = (render(await Pilot()).container.textContent ?? "");
    cleanup();
    const { default: Terms } = await import("@/app/(site)/terms/page");
    const terms = render(<Terms />).container.textContent ?? "";
    cleanup();
    for (const page of [pilot, terms]) {
      expect(page).toMatch(new RegExp(`\\$?${PILOT_PRICE_USD}`));
      expect(page).toContain(`${PILOT_DURATION_DAYS} days`);
      expect(page).toContain(String(PILOT_MEMBER_CAP));
    }
  });

  it("has no subscription or recurring-billing language on any of them", async () => {
    // Same rule as above: "no automatic renewal" and "one time, not a
    // subscription" are denials and must survive. What must not survive is a
    // sentence describing a recurring charge as something that happens.
    const forbidden = [
      /\brenews?\b/i,
      /\brenewal\b/i,
      /\brecurring\b/i,
      /\bper month\b/i,
      /\bper year\b/i,
      /\bmonthly\b/i,
      /\bannual(ly)?\b/i,
      /\bbilling period\b/i,
      /\bcustomer portal\b/i,
      /\byour subscription\b/i,
    ];
    for (const specifier of [...LEGAL_PAGES, "@/app/(site)/founding-pilot/page"]) {
      const text = await renderText(specifier);
      for (const pattern of forbidden) {
        const claims = unnegatedMatches(text, pattern);
        expect(claims, `${specifier} asserts ${pattern}`).toEqual([]);
      }
    }
  });
});
