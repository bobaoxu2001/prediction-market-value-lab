// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * The Founding Pilot sales page.
 *
 * This is the only page in the product that asks for money, which makes it the
 * only page where a wrong word is a misrepresentation rather than a typo. The
 * tests below are therefore mostly about what the page must *not* say, and about
 * the one structural rule that matters: when no payment URL is configured there
 * is nothing to buy, so nothing may look like a purchase.
 */

const ORIGINAL_ENV = { ...process.env };

afterEach(() => {
  cleanup();
  process.env = { ...ORIGINAL_ENV };
});

async function renderPage() {
  // Imported per test because the module reads `process.env` at call time and
  // the page's payment branch is the thing under test.
  const { default: FoundingPilotPage } = await import(
    "@/app/(site)/founding-pilot/page"
  );
  render(<FoundingPilotPage />);
}

describe("founding pilot sales page", () => {
  beforeEach(() => {
    delete process.env.PILOT_PAYMENT_LINK;
    delete process.env.PILOT_SEATS_TAKEN;
  });

  describe("with no payment link configured", () => {
    it("offers an enquiry, not a purchase", async () => {
      await renderPage();

      const ctas = screen.getAllByRole("link", { name: /request a founding spot/i });
      expect(ctas.length).toBeGreaterThan(0);
      for (const cta of ctas) {
        expect(cta.getAttribute("href")).toBe(
          "mailto:ax2183@nyu.edu?subject=PMVL%20Founding%20Pilot%20interest",
        );
      }
    });

    it("shows no control that looks like a purchase", async () => {
      await renderPage();

      // A button reading "Join the pilot - $49" with nothing behind it is the
      // single most dishonest thing this page could render.
      expect(screen.queryByRole("link", { name: /join the pilot/i })).toBeNull();
      expect(screen.queryByRole("button", { name: /join|buy|pay|checkout/i })).toBeNull();
      for (const link of screen.getAllByRole("link")) {
        expect(link.getAttribute("href") ?? "").not.toContain("buy.stripe.com");
      }
    });

    it("says plainly that payment is not open", async () => {
      await renderPage();
      expect(
        screen.getAllByText(
          /payment is not open yet\. founding spots are being reviewed manually\./i,
        ).length,
      ).toBeGreaterThan(0);
    });

    it("claims no specific number of remaining seats when nobody is counting", async () => {
      await renderPage();
      // Unset seat count must not render as "20 still open" - an unverified
      // scarcity signal is a fabricated one.
      expect(screen.queryByText(/still open/i)).toBeNull();
    });
  });

  describe("with a payment link configured", () => {
    beforeEach(() => {
      process.env.PILOT_PAYMENT_LINK = "https://buy.stripe.com/test_founding";
    });

    it("offers the purchase and drops the enquiry CTA", async () => {
      await renderPage();

      const join = screen.getAllByRole("link", { name: /join the pilot/i });
      expect(join.length).toBeGreaterThan(0);
      for (const link of join) {
        expect(link.getAttribute("href")).toBe("https://buy.stripe.com/test_founding");
      }
      expect(
        screen.queryByRole("link", { name: /request a founding spot/i }),
      ).toBeNull();
      expect(screen.queryByText(/payment is not open yet/i)).toBeNull();
    });

    it("ignores a payment URL that is not a Stripe payment link", async () => {
      // A pasted mistake, or a substituted value, must degrade to "not open"
      // rather than turn the CTA into a redirect to somebody else's payment page.
      process.env.PILOT_PAYMENT_LINK = "https://evil.example/checkout";
      await renderPage();

      expect(screen.queryByRole("link", { name: /join the pilot/i })).toBeNull();
      expect(
        screen.getAllByRole("link", { name: /request a founding spot/i }).length,
      ).toBeGreaterThan(0);
    });
  });

  describe("what the page may never claim", () => {
    const FORBIDDEN: [string, RegExp][] = [
      ["guaranteed returns", /guarantee[sd]?\s+(returns?|profits?|income|winners?)/i],
      ["profit promises", /(guaranteed|assured|risk-?free)\s+profit/i],
      ["winning trades", /winning trades?/i],
      ["beating the market", /beat the market/i],
      ["sure opportunities", /sure (thing|opportunit)/i],
      ["institutional secrets", /institutional secrets?/i],
      ["daily opportunities promised", /(guaranteed|every ?day you (will|'ll) get an?) opportunit/i],
      ["real-time or live data", /(real-?time|live|continuous|streaming) (data|feed|alerts?|quotes?)/i],
      ["automated execution", /(automated|automatic) (execution|trading|orders?)/i],
      ["personalised advice", /(personali[sz]ed|tailored|custom) (advice|recommendations?|portfolio)/i],
      ["telling the reader what to buy", /\byou should (buy|sell|trade|bet)\b/i],
    ];

    it.each(FORBIDDEN)("does not claim %s", async (_label, pattern) => {
      await renderPage();
      const text = document.body.textContent ?? "";

      // Rejecting a claim is allowed - the page says what it is *not*. So a
      // match only fails if it is not inside a negating clause.
      const matches = [...text.matchAll(new RegExp(pattern.source, "gi"))];
      for (const match of matches) {
        const before = text.slice(Math.max(0, match.index! - 90), match.index!);
        expect(
          /\b(no|not|never|without|nothing|neither|nor|is not|does not|cannot)\b/i.test(before),
          `"${match[0]}" appears without a negation: ...${before.slice(-70)}[${match[0]}]`,
        ).toBe(true);
      }
    });

    it("states that it is research, not advice, and promises no return", async () => {
      await renderPage();
      const text = document.body.textContent ?? "";
      expect(text).toMatch(/not investment, legal, tax or financial advice/i);
      expect(text).toMatch(/no return is promised/i);
      expect(text).toMatch(/can settle worthless/i);
    });

    it("describes the samples as historical rather than current research", async () => {
      await renderPage();
      const text = document.body.textContent ?? "";
      expect(text).toMatch(/historical samples?, not current market research/i);
      expect(text).toMatch(/2026-07-31/);
    });

    it("is listed in the sitemap", async () => {
      // A sales page nobody can find is not a sales page. Asserted because the
      // route existing and the route being reachable are different facts, and
      // only the first one shows up in a build log.
      const { default: sitemap } = await import("@/app/sitemap");
      const urls = sitemap().map((entry) => entry.url);
      expect(urls.some((url) => url.endsWith("/founding-pilot"))).toBe(true);
    });

    it("states the terms consistently: 49 dollars, 30 days, 20 members", async () => {
      await renderPage();
      const text = document.body.textContent ?? "";
      expect(text).toMatch(/\$49|USD 49/);
      expect(text).toMatch(/30 days/);
      expect(text).toMatch(/20 (max|members)/);
      expect(text).toMatch(/one time, not a subscription/i);
    });
  });
});
