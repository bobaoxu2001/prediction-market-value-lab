/**
 * Reading the order form, with the two venues' real markup as the fixtures.
 *
 * The first version of these tests asserted a reader that looked for a contract
 * count. Both venues were then loaded and neither has one — they are dollar
 * fields — so the tests were passing against a design that would have misread
 * every real order. The shapes below are copied from the live pages, observed on
 * 10 August 2026, and the important cases are the refusals.
 */

// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from "vitest";

import {
  contractsForDollars,
  readOrderInput,
  readSelectedSide,
} from "../src/order-form";

/** jsdom gives every element a zero box; the reader requires a visible field. */
function makeVisible(root: ParentNode): void {
  for (const input of Array.from(root.querySelectorAll("input"))) {
    input.getBoundingClientRect = () => ({ width: 120, height: 32 }) as DOMRect;
  }
}

function page(html: string): ParentNode {
  document.body.innerHTML = html;
  makeVisible(document);
  return document;
}

beforeEach(() => {
  document.body.innerHTML = "";
});

/* ------------------------------------------------------- the real venues -- */

/**
 * Kalshi's order ticket, reduced to what matters.
 *
 * The field itself says nothing — placeholder `0`, a generated React id, no
 * name, no aria-label. Only the panel around it names the unit, and it says
 * DOLLARS.
 */
const KALSHI_PANEL = `
  <div>
    <div>BUY SELL DOLLARS</div>
    <div>Boston vs Toronto</div>
    <div>Boston</div>
    <div>YES 61¢ NO 40¢</div>
    <div><span>Dollars</span><input type="text" id="_r_u_" placeholder="0" value="50"><span>$</span></div>
    <div>Odds 62% chance</div>
  </div>`;

/** Polymarket's, where the placeholder itself carries the dollar sign. */
const POLYMARKET_PANEL = `
  <div>
    <div>买入 卖出</div>
    <div>是 4.2¢ 否 95.9¢</div>
    <div>金额<input type="text" id="market-order-amount-input" placeholder="$0" value="25"></div>
  </div>`;

describe("the venues as they actually are", () => {
  it("reads Kalshi's amount as dollars, not as a contract count", () => {
    // The bug this replaced: 50 was taken as fifty contracts. It is fifty
    // dollars, which on a 61c contract is 81 contracts — not a rounding error.
    expect(readOrderInput(page(KALSHI_PANEL))).toEqual({ value: 50, unit: "dollars" });
  });

  it("reads Polymarket's amount as dollars from the placeholder alone", () => {
    expect(readOrderInput(page(POLYMARKET_PANEL))).toEqual({
      value: 25,
      unit: "dollars",
    });
  });

  it("ignores the site search box sitting on the same page", () => {
    // A number typed into search was previously a valid candidate, and with no
    // other numeric field on the page it became the trader's "order size".
    const withSearch = `<input type="text" name="search" placeholder="Trade on anything" value="100">${KALSHI_PANEL}`;
    expect(readOrderInput(page(withSearch))).toEqual({ value: 50, unit: "dollars" });
  });
});

/* ------------------------------------------------------------- the units -- */

describe("the unit must be established", () => {
  it("reads a contract count when the form says contracts", () => {
    expect(
      readOrderInput(
        page('<div><label for="q">Contracts</label><input id="q" value="37"></div>'),
      ),
    ).toEqual({ value: 37, unit: "contracts" });
  });

  it("refuses a bare number with nothing naming its unit", () => {
    // The whole point. A naked integer is more likely to be dollars than
    // contracts on both venues, so guessing would be wrong more often than right.
    expect(readOrderInput(page("<div><input value=\"50\"></div>"))).toBeNull();
  });

  it("treats an ambiguous panel as dollars, matching the venues' default", () => {
    const both = `<div><span>Dollars</span><span>Contracts</span><input value="20"></div>`;
    expect(readOrderInput(page(both))).toEqual({ value: 20, unit: "dollars" });
  });
});

describe("fields that are not an order amount", () => {
  it("ignores a limit price", () => {
    expect(
      readOrderInput(
        page('<div>Dollars<input aria-label="Limit price" value="37"></div>'),
      ),
    ).toBeNull();
  });

  it("ignores odds and percentage fields", () => {
    for (const label of ["Odds", "Percent"]) {
      expect(
        readOrderInput(page(`<div>Dollars<input aria-label="${label}" value="37"></div>`)),
      ).toBeNull();
    }
  });

  it("refuses when two amount fields both qualify", () => {
    const two = `<div>Dollars<input value="10"></div><div>Dollars<input value="99"></div>`;
    expect(readOrderInput(page(two))).toBeNull();
  });
});

describe("values that cannot be an order", () => {
  it("rejects zero, negatives and implausible magnitudes", () => {
    for (const value of ["0", "-4", "99999999"]) {
      expect(
        readOrderInput(page(`<div>Dollars<input value="${value}"></div>`)),
      ).toBeNull();
    }
  });

  it("accepts a fractional dollar amount, which is a real thing to type", () => {
    expect(readOrderInput(page('<div>Dollars<input value="12.50"></div>'))).toEqual({
      value: 12.5,
      unit: "dollars",
    });
  });

  it("rejects empty and non-numeric fields", () => {
    expect(readOrderInput(page('<div>Dollars<input value=""></div>'))).toBeNull();
    expect(readOrderInput(page('<div>Dollars<input value="all in"></div>'))).toBeNull();
  });

  it("ignores hidden, disabled and read-only fields", () => {
    expect(
      readOrderInput(page('<div>Dollars<input type="hidden" value="7"></div>')),
    ).toBeNull();
    expect(
      readOrderInput(page('<div>Dollars<input value="7" disabled></div>')),
    ).toBeNull();
    expect(
      readOrderInput(page('<div>Dollars<input value="7" readonly></div>')),
    ).toBeNull();
  });

  it("ignores a field with no box on screen", () => {
    document.body.innerHTML = '<div>Dollars<input value="7"></div>';
    expect(readOrderInput(document)).toBeNull();
  });
});

/* --------------------------------------------------------- the conversion -- */

describe("dollars into contracts", () => {
  it("floors, because a rounded-up count costs more than was typed", () => {
    // $50 at 61c is 81.9 contracts; 82 would exceed the amount.
    expect(contractsForDollars(50, 0.61)).toBe(81);
  });

  it("returns nothing when the amount cannot buy even one contract", () => {
    expect(contractsForDollars(0.5, 0.61)).toBeNull();
  });

  it("refuses a zero or negative price rather than dividing by it", () => {
    expect(contractsForDollars(50, 0)).toBeNull();
    expect(contractsForDollars(0, 0.61)).toBeNull();
  });
});

/* --------------------------------------------------------------- the side -- */

/**
 * The order panel's YES/NO toggle, as Kalshi actually publishes it.
 *
 * `aria-pressed` was read off the live page: the selected button carries
 * `"true"` and swaps on click. Nothing else on the element says which is on —
 * both are transparent-backgrounded and share a class string.
 */
const SIDE_PANEL = (yesPressed: boolean) => `
  <div>
    <div>BUY SELL DOLLARS</div>
    <button aria-pressed="${yesPressed}">YES 62¢</button>
    <button aria-pressed="${!yesPressed}">NO 39¢</button>
    <div><span>Dollars</span><input placeholder="0" value="50"></div>
  </div>`;

describe("which side the ticket is set to", () => {
  it("reads YES and NO off the toggle's pressed state", () => {
    expect(readSelectedSide(page(SIDE_PANEL(true)) as Document)).toBe("yes");
    expect(readSelectedSide(page(SIDE_PANEL(false)) as Document)).toBe("no");
  });

  it("ignores the market list's own Yes/No buttons below the ticket", () => {
    // A baseball page carries ten of these, none of which say anything about
    // what the trader is about to buy.
    const withList = `${SIDE_PANEL(true)}
      <div><button aria-pressed="true">Yes 49¢</button><button aria-pressed="false">No 52¢</button></div>`;
    expect(readSelectedSide(page(withList) as Document)).toBe("yes");
  });

  it("returns null when nothing is marked selected", () => {
    const none = `<div>Dollars<button>YES 62¢</button><button>NO 39¢</button><input placeholder="0" value="5"></div>`;
    expect(readSelectedSide(page(none) as Document)).toBeNull();
  });

  it("returns null when both claim to be selected", () => {
    const both = `<div>Dollars<button aria-pressed="true">YES</button><button aria-pressed="true">NO</button><input placeholder="0" value="5"></div>`;
    expect(readSelectedSide(page(both) as Document)).toBeNull();
  });

  it("returns null when the selected control is not a YES/NO toggle", () => {
    const garbled = `<div>Dollars<button aria-pressed="true">Buy</button><button aria-pressed="false">Sell</button><input placeholder="0" value="5"></div>`;
    expect(readSelectedSide(page(garbled) as Document)).toBeNull();
  });
});

/* ------------------------------------------------------- a hidden document -- */

describe("a backgrounded tab", () => {
  it("finds no order panel, because layout is not computed", () => {
    // Documented rather than worked around. A hidden tab reports zero-sized
    // rects for everything, so the readers correctly find nothing — and the
    // content script must therefore skip its refresh instead of concluding the
    // page is unrecognised and removing the panel.
    document.body.innerHTML = KALSHI_PANEL; // no makeVisible(): zero-sized rects
    expect(readOrderInput(document)).toBeNull();
    expect(readSelectedSide(document)).toBeNull();
  });
});
