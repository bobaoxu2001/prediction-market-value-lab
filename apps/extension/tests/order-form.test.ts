/**
 * Reading the order size out of a form nobody here has seen.
 *
 * Neither venue's DOM could be observed from the development environment, so
 * this reader is a set of guesses about someone else's markup. These tests pin
 * the shape of those guesses — and, more importantly, pin the cases where it must
 * refuse: a price field misread as a quantity puts a confident, fabricated number
 * next to a live order form, which is far worse than showing no row at all.
 */

// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from "vitest";

import { readOrderSize } from "../src/order-form";

/**
 * jsdom gives every element a zero bounding box, and the reader requires a
 * visible field. Stubbing the measurement keeps the visibility rule under test
 * (see the hidden/disabled cases) without every fixture failing on layout.
 */
function makeVisible(root: ParentNode): void {
  for (const input of Array.from(root.querySelectorAll("input"))) {
    input.getBoundingClientRect = () =>
      ({ width: 120, height: 32 }) as DOMRect;
  }
}

function form(html: string): ParentNode {
  document.body.innerHTML = html;
  makeVisible(document);
  return document;
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("reads a size when it is unambiguous", () => {
  it("takes the only numeric field on the page", () => {
    expect(readOrderSize(form('<input type="number" value="37">'))).toBe(37);
  });

  it("accepts a text field holding a whole number", () => {
    expect(readOrderSize(form('<input type="text" name="qty" value="12">'))).toBe(12);
  });

  it("tolerates thousands separators", () => {
    expect(readOrderSize(form('<input name="quantity" value="1,500">'))).toBe(1500);
  });
});

describe("refuses rather than guessing", () => {
  it("ignores a field named like a price", () => {
    // The killer case: a limit price of "37" is indistinguishable from a size of
    // "37" by value, so the name is the only thing that separates them.
    expect(readOrderSize(form('<input type="number" name="limitPrice" value="37">')))
      .toBeNull();
  });

  it("ignores price fields by placeholder, aria-label and test id", () => {
    for (const attr of [
      'placeholder="Limit price"',
      'aria-label="Price per contract"',
      'data-testid="order-price-input"',
    ]) {
      expect(readOrderSize(form(`<input type="number" ${attr} value="42">`))).toBeNull();
    }
  });

  it("ignores a price field identified only by its label", () => {
    expect(
      readOrderSize(
        form('<label for="p">Price (¢)</label><input id="p" type="number" value="42">'),
      ),
    ).toBeNull();
  });

  it("returns nothing when two plausible fields both survive", () => {
    // Picking between them is exactly the coin flip this must not make.
    expect(
      readOrderSize(
        form('<input type="number" value="10"><input type="number" value="99">'),
      ),
    ).toBeNull();
  });

  it("breaks a tie only when one field is positively named a quantity", () => {
    expect(
      readOrderSize(
        form(
          '<input type="number" value="10">' +
            '<input type="number" name="contracts" value="99">',
        ),
      ),
    ).toBe(99);
  });

  it("still refuses when two fields both look like quantities", () => {
    expect(
      readOrderSize(
        form(
          '<input type="number" name="qty" value="10">' +
            '<input type="number" name="shareCount" value="99">',
        ),
      ),
    ).toBeNull();
  });
});

describe("what is not a contract count", () => {
  it("rejects fractional and negative values", () => {
    expect(readOrderSize(form('<input type="number" value="1.5">'))).toBeNull();
    expect(readOrderSize(form('<input type="number" value="-4">'))).toBeNull();
  });

  it("rejects zero and implausibly large counts", () => {
    expect(readOrderSize(form('<input type="number" value="0">'))).toBeNull();
    expect(readOrderSize(form('<input type="number" value="99999999">'))).toBeNull();
  });

  it("rejects an empty or non-numeric field", () => {
    expect(readOrderSize(form('<input type="number" value="">'))).toBeNull();
    expect(readOrderSize(form('<input type="text" value="all in">'))).toBeNull();
  });
});

describe("fields the trader cannot be using", () => {
  it("ignores hidden, disabled and read-only inputs", () => {
    expect(readOrderSize(form('<input type="hidden" value="7">'))).toBeNull();
    expect(readOrderSize(form('<input type="number" value="7" disabled>'))).toBeNull();
    expect(readOrderSize(form('<input type="number" value="7" readonly>'))).toBeNull();
  });

  it("ignores a field with no box on screen", () => {
    document.body.innerHTML = '<input type="number" value="7">';
    // Left at jsdom's zero-size default rather than made visible.
    expect(readOrderSize(document)).toBeNull();
  });

  it("returns nothing on a page with no inputs at all", () => {
    expect(readOrderSize(form("<div>no form here</div>"))).toBeNull();
  });
});
