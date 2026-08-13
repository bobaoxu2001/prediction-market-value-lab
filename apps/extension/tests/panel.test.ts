/**
 * The panel's own logic: what it says, and what it refuses to say.
 *
 * Separate from the venue adapters because these are the decisions a reader
 * actually acts on — which row is theirs, whether a cheaper size is worth
 * mentioning, and whether the figure in front of them describes an order that
 * can be placed at all.
 */

import { describe, expect, it } from "vitest";

import { type CostAtSize } from "../src/cost";
import { dec } from "../src/decimal";
import {
  escapeHtml,
  freshness,
  messageHtml,
  panelHtml,
  strip,
} from "../src/panel";

function rung(
  size: string,
  cost: string,
  overrides: Partial<CostAtSize> = {},
): CostAtSize {
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
    ...overrides,
  };
}

const LADDER = [
  rung("1", "0.60"),
  rung("10", "0.55"),
  rung("100", "0.56"),
  rung("1000", "0.58"),
];

const BASE = {
  venue: "kalshi" as const,
  side: "yes" as const,
  ladder: LADDER,
  observedAt: 1_000_000,
  now: 1_000_000,
};

describe("freshness", () => {
  it("reads as recency, because that is the whole claim", () => {
    expect(freshness(1_000_000, 1_000_000)).toBe("just now");
    expect(freshness(1_000_000, 1_020_000)).toBe("20s ago");
    expect(freshness(1_000_000, 1_180_000)).toBe("3m ago");
  });

  it("never reports the future as an age", () => {
    expect(freshness(2_000_000, 1_000_000)).toBe("just now");
  });
});

describe("escaping", () => {
  it("neutralises markup before it reaches innerHTML", () => {
    expect(escapeHtml('<img src=x onerror="alert(1)">')).not.toContain("<img");
  });

  it("escapes the error path, which interpolates arbitrary text", () => {
    const html = messageHtml('boom <script>alert("x")</script>');
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});

describe("which rows are shown", () => {
  it("keeps the smallest, cheapest and largest placeable rungs", () => {
    const sizes = strip(LADDER).map((r) => r.size.units.toString());
    expect(sizes).toEqual(["1", "10", "1000"]);
  });

  it("drops rungs the venue would reject or the book cannot fill", () => {
    const kept = strip([
      rung("1", "0.90", { belowMinOrderSize: true }),
      rung("5", "0.55"),
      rung("1000", "0.58", { fullyFilled: false }),
    ]);
    expect(kept.map((r) => r.size.units.toString())).toEqual(["5"]);
  });
});

describe("the trader's own size", () => {
  it("is added, marked, and placed in size order", () => {
    const html = panelHtml({ ...BASE, yourRow: rung("250", "0.57") });
    expect(html).toContain("pmvl-yours");
    expect(html).toContain("250");
    // Between the 100 and 1000 rungs rather than appended at the end.
    expect(html.indexOf("250")).toBeGreaterThan(html.indexOf(">10<"));
  });

  it("replaces the ladder row when it lands on the same size", () => {
    const html = panelHtml({ ...BASE, yourRow: rung("10", "0.55") });
    // One row for size 10, not the ladder's and theirs side by side.
    expect(html.match(/>10 <span class="pmvl-tag">/g)?.length ?? 0).toBe(1);
    expect(html.match(/<td>10<\/td>/g)).toBeNull();
  });

  it("says in dollars what a cheaper size would save on this order", () => {
    // 250 at 57c against the cheapest at 55c is 2c x 250 = $5.00.
    const html = panelHtml({ ...BASE, yourRow: rung("250", "0.57") });
    expect(html).toContain("$5.00");
    expect(html).toContain("less on an order this size");
  });

  it("stays quiet when the saving rounds to nothing", () => {
    // A hundredth of a cent on ten contracts is not worth a sentence, and
    // dressing up rounding noise as advice teaches people to ignore the panel.
    const html = panelHtml({ ...BASE, yourRow: rung("10", "0.5500") });
    expect(html).not.toContain("less on an order this size");
  });

  it("warns instead of advising when the size is unplaceable", () => {
    const html = panelHtml({
      ...BASE,
      yourRow: rung("1", "9.99", { belowMinOrderSize: true }),
    });
    expect(html).toContain("will not accept an order of 1");
    expect(html).not.toContain("less on an order this size");
  });

  it("warns when the book cannot fill the size asked for", () => {
    const html = panelHtml({
      ...BASE,
      yourRow: rung("500", "0.57", { fullyFilled: false, filledSize: dec("120") }),
    });
    expect(html).toContain("cannot fill 500");
    expect(html).toContain("120");
  });

  it("falls back to naming the cheapest size when no size could be read", () => {
    const html = panelHtml({ ...BASE, yourRow: null });
    expect(html).toContain("Cheapest placeable size");
    expect(html).not.toContain("pmvl-yours");
  });
});

describe("the side is always stated", () => {
  it("names YES and NO in the header", () => {
    expect(panelHtml({ ...BASE, yourRow: null })).toContain("Entry cost · YES");
    expect(panelHtml({ ...BASE, side: "no", yourRow: null })).toContain(
      "Entry cost · NO",
    );
  });
});

describe("assumptions are disclosed per venue", () => {
  it("names the bridge allowance only when it contributes to Polymarket figures", () => {
    const withTransfer = [rung("5", "0.60", { transferCost: dec("0.10") })];
    const html = panelHtml({
      ...BASE,
      venue: "polymarket",
      ladder: withTransfer,
      yourRow: null,
    });
    expect(html).toContain("assumed $0.50 bridge/gas allowance");
    expect(panelHtml({ ...BASE, yourRow: null })).not.toContain("bridge/gas");
    expect(
      panelHtml({ ...BASE, venue: "polymarket", yourRow: null }),
    ).not.toContain("bridge/gas");
  });

  it("names the annual capital rate only when it contributes to the figures", () => {
    const withCapital = [rung("10", "0.56", { capitalCost: dec("0.01") })];
    const html = panelHtml({ ...BASE, ladder: withCapital, yourRow: null });
    expect(html).toContain("assumed 5% annual capital-cost rate through resolution");
    expect(panelHtml({ ...BASE, yourRow: null })).not.toContain(
      "annual capital-cost rate",
    );
  });

  it("discloses custom assumption values instead of silently naming defaults", () => {
    const html = panelHtml({
      ...BASE,
      venue: "polymarket",
      ladder: [
        rung("10", "0.57", {
          transferCost: dec("0.02"),
          capitalCost: dec("0.01"),
        }),
      ],
      yourRow: null,
      assumptions: {
        polymarketTransferCostUsd: dec("0.20"),
        capitalCostAnnualRate: dec("0.08"),
      },
    });
    expect(html).toContain("assumed $0.20 bridge/gas allowance");
    expect(html).toContain("assumed 8% annual capital-cost rate");
  });

  it("always says slippage is excluded and offers no recommendation", () => {
    const html = panelHtml({ ...BASE, yourRow: null });
    expect(html).toContain("Slippage is not included");
    expect(html).toContain("no recommendation");
  });
});

describe("an empty book", () => {
  it("says so rather than rendering an empty table", () => {
    const html = panelHtml({ ...BASE, ladder: [], yourRow: null });
    expect(html).toContain("No size on this contract could be filled");
    expect(html).not.toContain("<table");
  });
});
