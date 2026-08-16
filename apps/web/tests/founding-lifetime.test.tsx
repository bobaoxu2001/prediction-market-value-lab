// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import FoundingLifetimePage from "@/app/(site)/founding-lifetime/page";
import {
  FOUNDING_LIFETIME_INTEREST_MAILTO,
  FOUNDING_LIFETIME_PRICE_USD,
} from "@/lib/founding-lifetime";

afterEach(cleanup);

describe("Founding Lifetime demand test", () => {
  it("presents the proposed price and a mail draft, never checkout", () => {
    const { container } = render(<FoundingLifetimePage />);

    expect(screen.getByRole("heading", { name: /worth \$29 once/i })).toBeTruthy();
    const intent = screen.getByRole("link", { name: /consider paying \$29/i });
    expect(intent.getAttribute("href")).toBe(FOUNDING_LIFETIME_INTEREST_MAILTO);
    expect(intent.getAttribute("data-pmvl-funnel")).toBe("founding_offer_intent");
    expect(intent.getAttribute("data-pmvl-placement")).toBe("pricing");
    expect(container.querySelector("form")).toBeNull();
    expect(container.querySelector('a[href*="buy.stripe.com"]')).toBeNull();
    expect(container.querySelector('a[href^="https://checkout.stripe.com"]')).toBeNull();
  });

  it("says an interest email creates no purchase, reservation or entitlement", () => {
    const { container } = render(<FoundingLifetimePage />);
    const text = (container.textContent ?? "").toLowerCase();

    expect(text).toContain("there is no checkout");
    expect(text).toContain("does not buy or reserve lifetime access");
    expect(text).toContain("no purchase or reservation is created");
    expect(text).toContain("not a promise of perpetual cloud service");
  });

  it("bounds lifetime to low-marginal-cost local capability", () => {
    const { container } = render(<FoundingLifetimePage />);
    const text = container.textContent ?? "";

    expect(text).toContain(`$${FOUNDING_LIFETIME_PRICE_USD}`);
    expect(text).toMatch(/saved locally/i);
    expect(text).toMatch(/local cost history/i);
    expect(text).toMatch(/would not include.*cloud sync/is);
    expect(text).toMatch(/API access, AI usage/i);
    expect(text).toMatch(/signals, trade recommendations/i);
  });

  it("is discoverable from the sitemap", async () => {
    const { default: sitemap } = await import("@/app/sitemap");
    expect(sitemap().some((entry) => entry.url.endsWith("/founding-lifetime"))).toBe(true);
  });
});
