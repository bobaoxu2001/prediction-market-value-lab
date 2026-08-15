/**
 * The entry-cost stack, ported from `pmvl_markets.pricing`.
 *
 * A second implementation of the fee maths is the thing this repository most has
 * to avoid: two copies are two numbers that can disagree, and here they would
 * disagree in front of someone about to place an order. It exists anyway because
 * the alternative — the extension calling our API — would make every overlay
 * depend on a hosted pipeline whose published snapshot was eleven days stale when
 * this was written, and staleness is the specific problem the extension solves.
 *
 * The port is therefore held to the Python case by case: `conformance.test.ts`
 * replays fixtures generated from the real `pmvl_markets.pricing` functions and
 * fails on any difference. Change either side and that suite is what should break.
 *
 * ## What is observed and what is assumed
 *
 * Kept apart here exactly as in `cost_truth.py`, because the separation is the
 * product's central honesty claim:
 *
 * - **Observed / rule-derived:** ask-ladder VWAP at the requested size, the venue's
 *   published fee schedule, and its rounding rule.
 * - **Disclosed assumptions:** the Polygon transfer allowance and the annual
 *   capital rate. Included in the headline, labelled as configuration.
 * - **Excluded from the headline:** the slippage pad. It is `tick × ticks`, a flat
 *   constant, and on a cheap contract it exceeds every observed component
 *   combined — folding it in would make the headline mostly a config default.
 */

import {
  type Dec,
  ONE,
  ZERO,
  add,
  ceilCent,
  dec,
  gt,
  gte,
  isZero,
  lt,
  lte,
  max,
  min,
  mul,
  quantizePolyFee,
  quantizeUsd,
  safeDiv,
  sub,
} from "./decimal";

export type Venue = "kalshi" | "polymarket";


export interface BookLevel {
  price: Dec;
  size: Dec;
}

export interface ContractTerms {
  venue: Venue;
  /** Already includes the series fee multiplier, as the Python `market.fee_rate` does. */
  feeRate: Dec;
  feeType: string;
  tickSize: Dec;
  minOrderSize: Dec;
  /** Years until resolution. Zero disables the capital charge. */
  yearsToResolution: Dec;
}

export interface Assumptions {
  /** `POLYMARKET_TRANSFER_COST_USD`: bridge and gas, amortised over the position. */
  polymarketTransferCostUsd: Dec;
  /** `CAPITAL_COST_ANNUAL_RATE`: simple interest on the stake until resolution. */
  capitalCostAnnualRate: Dec;
}

export const DEFAULT_ASSUMPTIONS: Assumptions = {
  polymarketTransferCostUsd: dec("0.50"),
  capitalCostAnnualRate: dec("0.05"),
};

export interface CostAtSize {
  size: Dec;
  filledSize: Dec;
  fullyFilled: boolean;
  belowMinOrderSize: boolean;
  levelsConsumed: number;
  nominalPrice: Dec;
  entryPrice: Dec;
  /** The part of the premium caused by walking the ladder, not by any fee. */
  depthImpact: Dec;
  platformFee: Dec;
  feeRounding: Dec;
  transferCost: Dec;
  capitalCost: Dec;
  /** Headline: entry + fee + rounding + transfer + capital. Excludes slippage. */
  measuredCost: Dec;
  measuredPremium: Dec;
  measuredPremiumRatio: Dec | null;
  /** A binary contract pays $1, so the cost per contract IS the break-even. */
  breakevenProbability: Dec | null;
}

/* ------------------------------------------------------------------- fees -- */

/** `fees.kalshi_taker_fee`: quadratic, ceiled to the cent on the whole order. */
export function kalshiTakerFee(contracts: Dec, price: Dec, rate: Dec): Dec {
  if (lte(contracts, ZERO) || lte(price, ZERO) || gte(price, ONE) || lte(rate, ZERO)) {
    return ZERO;
  }
  return ceilCent(mul(mul(mul(rate, contracts), price), sub(ONE, price)));
}

/**
 * `fees.polymarket_taker_fee`: 5dp, and a computed fee below the floor rounds to
 * zero rather than up to it — the docs are explicit that very small trades near
 * the extremes may incur no fee at all.
 */
export function polymarketTakerFee(contracts: Dec, price: Dec, rate: Dec): Dec {
  if (lte(contracts, ZERO) || lte(price, ZERO) || gte(price, ONE) || lte(rate, ZERO)) {
    return ZERO;
  }
  const rounded = quantizePolyFee(mul(mul(mul(contracts, rate), price), sub(ONE, price)));
  return gte(rounded, dec("0.00001")) ? rounded : ZERO;
}

export function takerFee(
  venue: Venue,
  contracts: Dec,
  price: Dec,
  rate: Dec,
  feeType: string,
): Dec {
  if (venue === "kalshi") {
    // Flat-fee series charge a fixed amount per contract; `rate` carries it.
    if (feeType === "flat") return ceilCent(mul(rate, contracts));
    return kalshiTakerFee(contracts, price, rate);
  }
  return polymarketTakerFee(contracts, price, rate);
}

/** The portion of the fee that exists only because of rounding, per contract. */
export function feeRoundingCost(
  venue: Venue,
  contracts: Dec,
  price: Dec,
  rate: Dec,
  feeType: string,
): Dec {
  if (lte(contracts, ZERO)) return ZERO;
  if (venue === "kalshi" && feeType === "flat") return ZERO;
  const exact =
    venue === "kalshi"
      ? mul(mul(mul(rate, contracts), price), sub(ONE, price))
      : mul(mul(mul(contracts, rate), price), sub(ONE, price));
  const charged = takerFee(venue, contracts, price, rate, feeType);
  return safeDiv(sub(charged, exact), contracts);
}

/* -------------------------------------------------------------- book walk -- */

export interface Fill {
  filledSize: Dec;
  averagePrice: Dec;
  levelsConsumed: number;
  fullyFilled: boolean;
}

/**
 * `orderbook.walk_book`: consume ask levels cheapest-first.
 *
 * A partial fill is returned as a partial fill. Treating one as if the requested
 * size were available is the mistake that turns "you cannot buy this much" into a
 * confident per-contract number.
 */
export function walkBook(levels: BookLevel[], targetSize: Dec): Fill | null {
  if (lte(targetSize, ZERO) || levels.length === 0) return null;

  const sorted = [...levels].sort((a, b) => (lt(a.price, b.price) ? -1 : 1));
  let remaining = targetSize;
  let notional = ZERO;
  let filled = ZERO;
  let consumed = 0;

  for (const level of sorted) {
    if (lte(remaining, ZERO)) break;
    const take = min(remaining, level.size);
    if (lte(take, ZERO)) continue;
    notional = add(notional, mul(take, level.price));
    filled = add(filled, take);
    remaining = sub(remaining, take);
    consumed += 1;
  }

  if (lte(filled, ZERO)) return null;
  return {
    filledSize: filled,
    averagePrice: quantizeUsd(safeDiv(notional, filled)),
    levelsConsumed: consumed,
    fullyFilled: lte(remaining, ZERO),
  };
}

/* -------------------------------------------------------------- the stack -- */

export function costAtSize(
  levels: BookLevel[],
  size: Dec,
  terms: ContractTerms,
  assumptions: Assumptions = DEFAULT_ASSUMPTIONS,
): CostAtSize | null {
  const fill = walkBook(levels, size);
  if (fill === null) return null;

  const filled = fill.filledSize;
  const entry = fill.averagePrice;
  const nominal = [...levels].sort((a, b) => (lt(a.price, b.price) ? -1 : 1))[0].price;

  const totalFee = takerFee(terms.venue, filled, entry, terms.feeRate, terms.feeType);
  const feePerContract = safeDiv(totalFee, filled);
  const rounding = feeRoundingCost(
    terms.venue,
    filled,
    entry,
    terms.feeRate,
    terms.feeType,
  );
  // `feePerContract` already contains the rounding component; report it separately
  // and subtract here so the total is not counted twice.
  const feeExRounding = max(ZERO, sub(feePerContract, rounding));

  const transfer =
    terms.venue === "polymarket" && gt(filled, ZERO)
      ? quantizeUsd(safeDiv(assumptions.polymarketTransferCostUsd, filled))
      : ZERO;

  const capital =
    gt(terms.yearsToResolution, ZERO) && gt(assumptions.capitalCostAnnualRate, ZERO)
      ? quantizeUsd(
          mul(mul(entry, assumptions.capitalCostAnnualRate), terms.yearsToResolution),
        )
      : ZERO;

  const measured = quantizeUsd(
    add(
      add(add(quantizeUsd(entry), quantizeUsd(feeExRounding)), quantizeUsd(rounding)),
      add(transfer, capital),
    ),
  );
  const premium = quantizeUsd(sub(measured, nominal));
  const ratio = lte(nominal, ZERO) ? null : safeDiv(premium, nominal);

  return {
    size,
    filledSize: filled,
    fullyFilled: fill.fullyFilled,
    belowMinOrderSize: lt(size, terms.minOrderSize),
    levelsConsumed: fill.levelsConsumed,
    nominalPrice: nominal,
    entryPrice: quantizeUsd(entry),
    depthImpact: quantizeUsd(sub(entry, nominal)),
    platformFee: quantizeUsd(feeExRounding),
    feeRounding: quantizeUsd(rounding),
    transferCost: transfer,
    capitalCost: capital,
    measuredCost: measured,
    measuredPremium: premium,
    measuredPremiumRatio: ratio,
    // A binary contract pays exactly $1, so a cost at or above $1 has no
    // break-even probability at all. Reporting 1.03 would be a number that
    // cannot exist, so the field goes empty instead.
    breakevenProbability: gt(measured, ZERO) && lt(measured, ONE) ? measured : null,
  };
}

/** Default rungs, matching the sizes the web ladder offers. */
export const LADDER_SIZES = ["1", "5", "10", "25", "50", "100", "250", "500", "1000"];

export function costLadder(
  levels: BookLevel[],
  terms: ContractTerms,
  assumptions: Assumptions = DEFAULT_ASSUMPTIONS,
  sizes: string[] = LADDER_SIZES,
): CostAtSize[] {
  const out: CostAtSize[] = [];
  for (const size of sizes) {
    const row = costAtSize(levels, dec(size), terms, assumptions);
    if (row !== null) out.push(row);
  }
  return out;
}

/**
 * The size that minimises cost per contract among rungs a trader could place.
 *
 * The one figure on the overlay that is directly actionable. Rungs below the venue
 * minimum are excluded because they are unplaceable rather than merely expensive,
 * and partial fills because their per-contract cost describes a position the
 * trader would not end up holding.
 */
export function cheapestPlaceable(ladder: CostAtSize[]): CostAtSize | null {
  const usable = ladder.filter((row) => !row.belowMinOrderSize && row.fullyFilled);
  if (usable.length === 0) return null;
  return usable.reduce((best, row) =>
    lt(row.measuredCost, best.measuredCost) ? row : best,
  );
}
