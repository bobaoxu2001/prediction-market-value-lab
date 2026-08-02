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

vi.mock("@/lib/auth-server", () => ({
  isAuthConfigured: () => false,
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

  it("offers early access rather than checkout while billing is disabled", async () => {
    await renderPricing();
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

  it("marks every unresolved owner decision visibly rather than inventing one", async () => {
    for (const specifier of ["@/app/(site)/terms/page", "@/app/(site)/privacy/page"]) {
      const { default: Page } = await import(specifier);
      const { container } = render(<Page />);
      expect(container.querySelectorAll("mark").length).toBeGreaterThan(0);
      expect(container.textContent).toContain("OWNER INPUT REQUIRED");
      cleanup();
    }
  });
});
