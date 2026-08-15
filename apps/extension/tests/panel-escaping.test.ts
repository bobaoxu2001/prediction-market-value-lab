/**
 * Every string that reaches panelHtml's innerHTML must render as text.
 *
 * The panel is injected into venue pages with content-script privileges, so an
 * unescaped interpolation is an execution sink. Today's inputs are internal
 * enums and Decimal-derived strings, but the discipline is: escape everything,
 * and prove it with hostile strings.
 */

import { describe, expect, it } from "vitest";

import { type CostAtSize } from "../src/cost";
import { dec } from "../src/decimal";
import { escapeHtml, messageHtml, panelHtml } from "../src/panel";
import type { Venue } from "../src/cost";
import type { Side } from "../src/venues";

const PAYLOAD = "<img src=x onerror=alert(1)>";

function rung(size: string, cost: string): CostAtSize {
  const measured = dec(cost);
  return {
    size: dec(size),
    filledSize: dec(size),
    fullyFilled: true,
    belowMinOrderSize: false,
    levelsConsumed: 1,
    nominalPrice: dec("0.50"),
    entryPrice: dec("0.50"),
    depthImpact: dec("0"),
    platformFee: dec("0"),
    feeRounding: dec("0"),
    transferCost: dec("0"),
    capitalCost: dec("0"),
    measuredCost: measured,
    measuredPremium: dec("0"),
    measuredPremiumRatio: dec("0.05"),
    breakevenProbability: measured,
  };
}

describe("escapeHtml", () => {
  it("escapes the five HTML-significant characters", () => {
    expect(escapeHtml('<img src="x" onerror=\'alert(1)\'>&')).toBe(
      "&lt;img src=&quot;x&quot; onerror=&#39;alert(1)&#39;&gt;&amp;",
    );
  });

  it("leaves plain text alone", () => {
    expect(escapeHtml("Entry cost · YES — 60.0¢")).toBe("Entry cost · YES — 60.0¢");
  });
});

describe("panelHtml renders hostile strings as text", () => {
  const ladder = [rung("1", "0.60"), rung("10", "0.55")];

  it("escapes a hostile side label in the header", () => {
    const html = panelHtml({
      venue: "kalshi",
      side: PAYLOAD as unknown as Side,
      ladder,
      observedAt: 1_000,
      yourRow: null,
      now: 1_000,
    });
    // The side is uppercased before escaping.
    expect(html).toContain("&lt;IMG SRC=X ONERROR=ALERT(1)&gt;");
    expect(html).not.toContain("<img src=x onerror=alert(1)>");
  });

  it("escapes a hostile venue string in the assumption disclosure", () => {
    const hostileVenue = PAYLOAD as unknown as Venue;
    const html = panelHtml({
      venue: hostileVenue,
      side: "yes",
      ladder: [rung("1", "0.60")],
      observedAt: 1_000,
      yourRow: null,
      now: 1_000,
    });
    // The venue name only feeds the assumption text; whatever it carries, the
    // payload must never appear as markup.
    expect(html).not.toContain("<img");
  });

  it("renders your-order warnings with intentional markup intact and no markup injection", () => {
    const html = panelHtml({
      venue: "kalshi",
      side: "yes",
      ladder,
      observedAt: 1_000,
      yourRow: rung("1", "0.60"),
      yourDollars: 5,
      now: 1_000,
    });
    // The dollars warning renders, and no hostile markup can arrive via it.
    expect(html).toContain("$5 buys about");
    expect(html).not.toContain("<img");
    // The intentional emphasis in the saving note survives escaping.
    expect(html).toContain("<strong>$");
    expect(html).not.toContain("&lt;strong&gt;");
  });
});

describe("messageHtml", () => {
  it("escapes error text", () => {
    const html = messageHtml("bad <script>alert(1)</script> response");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });
});
