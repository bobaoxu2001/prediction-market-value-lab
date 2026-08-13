import type { CostAtSize } from "@/lib/api";

/**
 * Choosing which rungs of a cost ladder to show when there is only room for a few.
 *
 * Shared by the landing hero and the per-contract share card so the two cannot
 * pick different rungs for the same contract and appear to disagree about what
 * entry costs.
 */

/**
 * Rungs the venue would actually accept and the book can actually fill.
 *
 * Both filters are load-bearing. A Polymarket contract with a five-contract
 * minimum will happily price one contract — the arithmetic is defined — and the
 * answer was "quoted 0.10¢, costs 50.1¢, +50010%", which is a real number for an
 * order that cannot be placed. A partially filled rung is the same kind of
 * mistake: a per-contract cost for a position you would not end up holding.
 */
export function fillableRungs(ladder: CostAtSize[]): CostAtSize[] {
  return ladder.filter(
    (rung) =>
      !rung.below_min_order_size &&
      rung.fully_filled &&
      rung.measured_cost !== null,
  );
}

/**
 * The smallest, cheapest and largest fillable rungs, in size order.
 *
 * Not three evenly spaced sizes. Cost per contract is U-shaped — the venue fee
 * and its rounding rule dominate a small order, book depth dominates a large one
 * — so the middle of the ladder is not the interesting point and the *minimum*
 * is. On a live Kalshi contract quoted at 1.00¢: one contract 2.00¢, ten
 * contracts 1.10¢, a thousand 2.12¢. Picking positionally showed the fifty-
 * contract rung and hid the cheapest size, which is the one figure here a reader
 * can act on.
 *
 * Fewer than three are returned when the ladder is short or when the cheapest
 * rung is already an endpoint; the caller renders what it gets.
 */
export function ladderStrip(ladder: CostAtSize[]): CostAtSize[] {
  const fillable = fillableRungs(ladder);
  if (fillable.length <= 3) return fillable;

  const cheapest = fillable.reduce((best, rung) =>
    Number(rung.measured_cost) < Number(best.measured_cost) ? rung : best,
  );

  // Deduplicated by cost, not only by size. Past the point where fee rounding
  // stops biting, a ladder is flat: on a live Kalshi contract the cheapest rung
  // and the largest both read "1.1¢, +7.0%", and showing them as two rows spends
  // a third of the space saying the same thing twice. The smallest size wins a
  // tie, because it is the cheaper one to actually put on.
  const picked = new Map<string, CostAtSize>();
  for (const rung of [fillable[0], cheapest, fillable[fillable.length - 1]]) {
    const key = Number(rung.measured_cost).toFixed(4);
    const held = picked.get(key);
    if (!held || Number(rung.size) < Number(held.size)) picked.set(key, rung);
  }
  return [...picked.values()].sort((a, b) => Number(a.size) - Number(b.size));
}

/**
 * The largest single component of an estimate, and whether it is an assumption.
 *
 * `transfer_cost` and `capital_cost` are disclosed configuration inputs, not
 * observations. When one of them is most of the estimate, any surface quoting
 * that estimate has to say so — otherwise a config default is wearing the
 * clothes of a measurement, which the README names as the one thing the product
 * must not do.
 */
export function dominantDriver(
  rung: CostAtSize,
): { label: string; assumed: boolean; share: number } | null {
  const components = rung.measured_components ?? {};
  const named: Array<{ label: string; assumed: boolean; raw: string | null }> = [
    { label: "order-book depth", assumed: false, raw: components.depth_impact },
    { label: "the venue fee", assumed: false, raw: components.platform_fee },
    { label: "fee rounding", assumed: false, raw: components.fee_rounding },
    {
      label: "the assumed transfer cost",
      assumed: true,
      raw: components.transfer_cost,
    },
    {
      label: "the assumed capital cost",
      assumed: true,
      raw: components.capital_cost,
    },
  ];

  const values = named
    .map((row) => ({ ...row, value: Math.abs(Number(row.raw ?? 0)) }))
    .filter((row) => Number.isFinite(row.value) && row.value > 0);
  if (!values.length) return null;

  const total = values.reduce((sum, row) => sum + row.value, 0);
  if (total <= 0) return null;
  const top = values.reduce((a, b) => (b.value > a.value ? b : a));
  return { label: top.label, assumed: top.assumed, share: top.value / total };
}

/** True when an assumption, rather than an observation, carries the estimate. */
export function isAssumptionDominated(rung: CostAtSize): boolean {
  const driver = dominantDriver(rung);
  return Boolean(driver?.assumed && driver.share > 0.5);
}
