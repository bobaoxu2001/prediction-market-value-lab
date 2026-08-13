/**
 * The TypeScript cost port against the Python, case for case.
 *
 * The extension exists because the overlay has to work on the venue's live book
 * rather than our published snapshot, and that requires the fee maths in
 * TypeScript. Two implementations are two numbers that can disagree — so this
 * suite is the price of having a second one, and a failure here means the
 * extension is about to tell somebody the wrong cost while they place an order.
 *
 * Fixtures come from `scripts/generate_cost_conformance_fixtures.py`, which calls
 * the real `pmvl_markets.pricing` functions rather than restating them. If the
 * Python changes, regenerate and read the diff: it is a change in what the
 * product says an order costs.
 */

import { describe, expect, it } from "vitest";

import fixtures from "../fixtures/cost-conformance.json";
import {
  type Venue,
  feeRoundingCost,
  takerFee,
  walkBook,
} from "../src/cost";
import { dec, safeDiv, toString, ZERO } from "../src/decimal";

interface FeeCase {
  venue: string;
  contracts: string;
  price: string;
  rate: string;
  fee_type: string;
  total_fee: string;
  fee_per_contract: string;
  fee_rounding: string;
}

interface WalkCase {
  book: string;
  levels: Array<{ price: string; size: string }>;
  size: string;
  result: {
    filled_size: string;
    average_price: string;
    levels_consumed: number;
    fully_filled: boolean;
  } | null;
}

const feeCases = fixtures.fee_cases as FeeCase[];
const walkCases = fixtures.walk_cases as WalkCase[];

/**
 * Compares two decimal strings by value, not by spelling.
 *
 * Python prints `Decimal("0.070")` with its trailing zero and the port does not
 * carry that trailing-zero exponent, so a string comparison would fail on cases
 * where the two agree perfectly. Value equality is the property that matters.
 */
function expectSameValue(actual: string, expected: string, label: string): void {
  const a = dec(actual);
  const b = dec(expected);
  const scale = Math.max(a.scale, b.scale);
  const lhs = a.units * 10n ** BigInt(scale - a.scale);
  const rhs = b.units * 10n ** BigInt(scale - b.scale);
  expect(lhs, `${label}: got ${actual}, python said ${expected}`).toBe(rhs);
}

describe("fee maths agrees with the Python implementation", () => {
  it("has a non-trivial number of cases", () => {
    // A silently empty fixture file would make this whole suite pass vacuously,
    // which is the one way a conformance test can be worse than no test at all.
    expect(feeCases.length).toBeGreaterThan(500);
    expect(walkCases.length).toBeGreaterThan(20);
  });

  it("reproduces every total taker fee", () => {
    for (const c of feeCases) {
      const got = takerFee(
        c.venue as Venue,
        dec(c.contracts),
        dec(c.price),
        dec(c.rate),
        c.fee_type,
      );
      expectSameValue(
        toString(got),
        c.total_fee,
        `total_fee ${c.venue} ${c.contracts}@${c.price} rate=${c.rate} type=${c.fee_type || "-"}`,
      );
    }
  });

  it("reproduces every per-contract fee", () => {
    for (const c of feeCases) {
      const total = takerFee(
        c.venue as Venue,
        dec(c.contracts),
        dec(c.price),
        dec(c.rate),
        c.fee_type,
      );
      const got = safeDiv(total, dec(c.contracts));
      expectSameValue(
        toString(got),
        c.fee_per_contract,
        `fee_per_contract ${c.venue} ${c.contracts}@${c.price} rate=${c.rate}`,
      );
    }
  });

  it("reproduces every rounding component", () => {
    for (const c of feeCases) {
      const got = feeRoundingCost(
        c.venue as Venue,
        dec(c.contracts),
        dec(c.price),
        dec(c.rate),
        c.fee_type,
      );
      expectSameValue(
        toString(got),
        c.fee_rounding,
        `fee_rounding ${c.venue} ${c.contracts}@${c.price} rate=${c.rate}`,
      );
    }
  });
});

describe("book walking agrees with the Python implementation", () => {
  it("reproduces every fill, including the partial ones", () => {
    for (const c of walkCases) {
      const levels = c.levels.map((l) => ({ price: dec(l.price), size: dec(l.size) }));
      const got = walkBook(levels, dec(c.size));

      if (c.result === null) {
        expect(got, `${c.book} @ ${c.size} should not fill`).toBeNull();
        continue;
      }
      expect(got, `${c.book} @ ${c.size} should fill`).not.toBeNull();
      const fill = got!;
      expectSameValue(
        toString(fill.filledSize),
        c.result.filled_size,
        `filled_size ${c.book} @ ${c.size}`,
      );
      expectSameValue(
        toString(fill.averagePrice),
        c.result.average_price,
        `average_price ${c.book} @ ${c.size}`,
      );
      expect(fill.levelsConsumed, `levels_consumed ${c.book} @ ${c.size}`).toBe(
        c.result.levels_consumed,
      );
      expect(fill.fullyFilled, `fully_filled ${c.book} @ ${c.size}`).toBe(
        c.result.fully_filled,
      );
    }
  });

  it("reports a partial fill as partial", () => {
    // The `thin` book holds 7 contracts in total, so 100 cannot be filled and the
    // per-contract cost of the part that would fill must not be presented as the
    // cost of the order that was asked for.
    const partial = walkCases.find(
      (c) => c.book === "thin" && c.size === "100",
    );
    expect(partial?.result?.fully_filled).toBe(false);

    const levels = partial!.levels.map((l) => ({
      price: dec(l.price),
      size: dec(l.size),
    }));
    const got = walkBook(levels, dec("100"));
    expect(got!.fullyFilled).toBe(false);
  });
});

describe("the Kalshi cent ceiling, which is the product's headline claim", () => {
  it("charges a full cent of fee on a one-lot", () => {
    // The README's central example: a 1c contract carries a 1c fee on a single
    // contract, doubling the cost. If this ever stops being true the landing page
    // is wrong, so it is asserted here and not only in prose.
    const fee = takerFee("kalshi", dec("1"), dec("0.01"), dec("0.07"), "");
    expect(toString(fee)).toBe("0.01");
  });

  it("costs far less per contract at a hundred", () => {
    const one = safeDiv(
      takerFee("kalshi", dec("1"), dec("0.01"), dec("0.07"), ""),
      dec("1"),
    );
    const hundred = safeDiv(
      takerFee("kalshi", dec("100"), dec("0.01"), dec("0.07"), ""),
      dec("100"),
    );
    expect(Number(toString(one))).toBeGreaterThan(Number(toString(hundred)) * 10);
  });

  it("never returns a fee for a degenerate price", () => {
    for (const price of ["0", "1"]) {
      expect(takerFee("kalshi", dec("10"), dec(price), dec("0.07"), "")).toEqual(ZERO);
      expect(takerFee("polymarket", dec("10"), dec(price), dec("0.07"), "")).toEqual(
        ZERO,
      );
    }
  });
});
