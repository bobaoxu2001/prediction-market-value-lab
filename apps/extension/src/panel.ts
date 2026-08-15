/**
 * Building the overlay's markup, kept apart from the DOM plumbing that injects it.
 *
 * Separated so the preview harness renders exactly the markup the content script
 * renders. When the two were separate copies the harness drifted immediately, and
 * a preview of markup nobody ships is worth very little.
 */

import {
  type Assumptions,
  type CostAtSize,
  DEFAULT_ASSUMPTIONS,
  cheapestPlaceable,
} from "./cost";
import { type Dec, isZero, toNumber, toString as decToString } from "./decimal";
import { type Venue } from "./cost";
import { type Side } from "./venues";

/**
 * Escapes text before it reaches `innerHTML`.
 *
 * This runs inside a content script, so an injection here executes with the
 * extension's content-script privileges on the venue's page. Nothing currently
 * interpolated is attacker-controlled — but "currently" is doing a lot of work in
 * that sentence, and a market title or an error message containing markup is the
 * obvious way it stops being true.
 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function cents(value: Dec): string {
  const n = toNumber(value) * 100;
  return `${n < 10 ? n.toFixed(2) : n.toFixed(1)}¢`;
}

export function percent(value: Dec | null): string {
  if (value === null) return "—";
  const n = toNumber(value) * 100;
  return `${n >= 0 ? "+" : ""}${Math.abs(n) < 10 ? n.toFixed(1) : n.toFixed(0)}%`;
}

export function probability(value: Dec | null): string {
  if (value === null) return "—";
  return `${(toNumber(value) * 100).toFixed(1)}%`;
}

export function sizeLabel(value: Dec): string {
  return decToString(value).replace(/\.0+$/, "");
}

/** "just now", "20s ago", "3m ago" — the claim the extension exists to make. */
export function freshness(observedAt: number, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - observedAt) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 90) return `${seconds}s ago`;
  return `${Math.round(seconds / 60)}m ago`;
}

/** Smallest, cheapest and largest placeable rungs — the U-shape, not three points. */
export function strip(ladder: CostAtSize[]): CostAtSize[] {
  const usable = ladder.filter((r) => !r.belowMinOrderSize && r.fullyFilled);
  if (usable.length <= 3) return usable;
  const cheapest = cheapestPlaceable(usable)!;
  const picked = new Map<string, CostAtSize>();
  for (const row of [usable[0], cheapest, usable[usable.length - 1]]) {
    const key = decToString(row.measuredCost);
    const held = picked.get(key);
    if (!held || toNumber(row.size) < toNumber(held.size)) picked.set(key, row);
  }
  return [...picked.values()].sort((a, b) => toNumber(a.size) - toNumber(b.size));
}

export interface PanelInput {
  venue: Venue;
  side: Side;
  ladder: CostAtSize[];
  observedAt: number;
  /** The row for the size in the order form, when one could be read confidently. */
  yourRow: CostAtSize | null;
  /**
   * The dollar amount the contract count was derived from, when the order form
   * was denominated in dollars rather than contracts.
   *
   * Shown so the reader can see where the count came from. Both venues default
   * to dollars, so without this the panel would silently assert a contract count
   * the trader never typed.
   */
  yourDollars?: number | null;
  /** The scenario inputs used to compute the supplied rows. */
  assumptions?: Assumptions;
  now?: number;
}

function dollars(value: Dec): string {
  return `$${decToString(value)}`;
}

function annualRate(value: Dec): string {
  const percentage = (toNumber(value) * 100)
    .toFixed(2)
    .replace(/\.0+$/, "")
    .replace(/(\.\d*?)0+$/, "$1");
  return `${percentage}% annual capital-cost rate`;
}

function row(entry: CostAtSize, kind: "" | "best" | "yours"): string {
  const classes = [kind === "best" ? "pmvl-best" : "", kind === "yours" ? "pmvl-yours" : ""]
    .filter(Boolean)
    .join(" ");
  const label =
    kind === "yours"
      ? `${escapeHtml(sizeLabel(entry.size))} <span class="pmvl-tag">yours</span>`
      : escapeHtml(sizeLabel(entry.size));
  return `<tr${classes ? ` class="${classes}"` : ""}>
    <td>${label}</td>
    <td>${cents(entry.measuredCost)}</td>
    <td class="pmvl-warn">${percent(entry.measuredPremiumRatio)}</td>
    <td>${probability(entry.breakevenProbability)}</td>
  </tr>`;
}

/**
 * A caution when the size in the order form is not the cheapest way in.
 *
 * The only actionable sentence the overlay produces, so it is stated in money the
 * trader is actually about to spend rather than as a percentage. It appears only
 * when the gap is real: rounding noise dressed up as advice would train people to
 * ignore the panel.
 */
function savingNote(yours: CostAtSize, cheapest: CostAtSize): string | null {
  const perContract = toNumber(yours.measuredCost) - toNumber(cheapest.measuredCost);
  if (perContract <= 0) return null;
  const onYourOrder = perContract * toNumber(yours.size);
  // Under a cent on the whole order is not worth a sentence.
  if (onYourOrder < 0.01) return null;
  return `At ${escapeHtml(sizeLabel(yours.size))} you pay ${escapeHtml(cents(yours.measuredCost))} each.
    ${escapeHtml(sizeLabel(cheapest.size))} costs ${escapeHtml(cents(cheapest.measuredCost))} each —
    <strong>$${onYourOrder.toFixed(2)}</strong> less on an order this size.`;
}

export function panelHtml(input: PanelInput): string {
  const rows = strip(input.ladder);
  const sideLabel = escapeHtml(input.side.toUpperCase());
  const head = `<div class="pmvl-head"><span class="pmvl-dot"></span>Entry cost · ${sideLabel}</div>`;

  if (rows.length === 0 && input.yourRow === null) {
    return `${head}<p class="pmvl-note">No size on this contract could be filled at
      the depth currently on the book, so no cost is shown.</p>`;
  }

  const cheapest = cheapestPlaceable(rows);
  // The trader's own size is shown even when it is below the minimum or cannot be
  // filled, because that is precisely when they most need to know.
  const yours = input.yourRow;
  const shown = yours
    ? [...rows.filter((r) => decToString(r.size) !== decToString(yours.size)), yours].sort(
        (a, b) => toNumber(a.size) - toNumber(b.size),
      )
    : rows;

  const body = shown
    .map((entry) =>
      row(
        entry,
        yours && decToString(entry.size) === decToString(yours.size)
          ? "yours"
          : entry === cheapest
            ? "best"
            : "",
      ),
    )
    .join("");

  const warnings: string[] = [];
  if (yours?.belowMinOrderSize) {
    warnings.push(
      `This venue will not accept an order of ${sizeLabel(yours.size)} on this
       contract, so that figure is arithmetic rather than a trade you can place.`,
    );
  } else if (yours && !yours.fullyFilled) {
    warnings.push(
      `The book cannot fill ${sizeLabel(yours.size)} right now — only
       ${sizeLabel(yours.filledSize)} is available at any price on screen.`,
    );
  } else if (yours && cheapest) {
    const saving = savingNote(yours, cheapest);
    if (saving) warnings.push(saving);
  }
  if (yours && input.yourDollars) {
    warnings.push(
      `Your order form is in dollars: $${escapeHtml(String(input.yourDollars))} buys about
       ${escapeHtml(sizeLabel(yours.size))} contracts at the current top of book.`,
    );
  }
  if (!yours && cheapest) {
    warnings.push(
      `Cheapest placeable size: <strong>${escapeHtml(sizeLabel(cheapest.size))}</strong> at
       ${escapeHtml(cents(cheapest.measuredCost))} each.`,
    );
  }

  const assumptions = input.assumptions ?? DEFAULT_ASSUMPTIONS;
  const includedAssumptions: string[] = [];
  if (
    input.venue === "polymarket" &&
    shown.some((entry) => !isZero(entry.transferCost))
  ) {
    includedAssumptions.push(
      `an assumed ${dollars(assumptions.polymarketTransferCostUsd)} bridge/gas allowance amortised over the position`,
    );
  }
  if (shown.some((entry) => !isZero(entry.capitalCost))) {
    includedAssumptions.push(
      `an assumed ${annualRate(assumptions.capitalCostAnnualRate)} through resolution`,
    );
  }

  const assumptionDisclosure =
    includedAssumptions.length > 0
      ? ` Scenario inputs included in these figures: ${escapeHtml(includedAssumptions.join("; "))}.`
      : "";

  return `${head}
    <table class="pmvl-table">
      <thead><tr><th>Size</th><th>Costs</th><th>Over quote</th><th>Break-even</th></tr></thead>
      <tbody>${body}</tbody>
    </table>
    ${warnings.map((w) => `<p class="pmvl-note pmvl-best-note">${escapeHtml(w)}</p>`).join("")}
    <p class="pmvl-note">
      Book read ${escapeHtml(freshness(input.observedAt, input.now))}. Cost uses observed ask
      depth and published venue fee and rounding rules.${assumptionDisclosure}
      Slippage is not included. No forecast, no recommendation — research only.
    </p>`;
}

export function messageHtml(message: string): string {
  return `<div class="pmvl-head"><span class="pmvl-dot"></span>Entry cost</div>
    <p class="pmvl-note">${escapeHtml(message)}</p>`;
}
