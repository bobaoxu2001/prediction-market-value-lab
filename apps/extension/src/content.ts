/**
 * The overlay: what this order actually costs, next to the order ticket.
 *
 * Everything here is read-only. The extension never types into a field, never
 * changes an order size, and never submits anything — it puts a number beside
 * what the trader is already doing and leaves the decision alone. That is not
 * only a safety position: an extension that edited an order form would be a very
 * different thing to install, and the value on offer is the arithmetic.
 *
 * ## What it shows, and what it deliberately does not
 *
 * Cost per contract at a few real sizes, the break-even probability that follows
 * from it, and which placeable size is cheapest. No probability estimate, no
 * edge, no recommendation: the retrodiction over 22 segments found no place where
 * this project's models beat the market's own price, and the one segment that
 * cleared significance did so in the wrong direction.
 *
 * So the overlay says what the trade costs. It has no opinion on whether to make
 * it.
 */

import {
  type CostAtSize,
  DEFAULT_ASSUMPTIONS,
  cheapestPlaceable,
  costLadder,
} from "./cost";
import { toNumber, toString as decToString } from "./decimal";
import { type Contract, identify, loadBook, termsFromBook } from "./venues";

const PANEL_ID = "pmvl-cost-overlay";
const REFRESH_MS = 20_000;

async function fetchJson(url: string): Promise<unknown> {
  const reply = await chrome.runtime.sendMessage({ type: "pmvl:fetch", url });
  if (!reply?.ok) throw new Error(reply?.error ?? "no reply from background");
  return reply.value;
}

/* ------------------------------------------------------------------ render -- */

function cents(value: { units: bigint; scale: number }): string {
  const n = toNumber(value) * 100;
  return `${n < 10 ? n.toFixed(2) : n.toFixed(1)}¢`;
}

function percent(value: { units: bigint; scale: number } | null): string {
  if (value === null) return "—";
  const n = toNumber(value) * 100;
  return `${n >= 0 ? "+" : ""}${Math.abs(n) < 10 ? n.toFixed(1) : n.toFixed(0)}%`;
}

function probability(value: { units: bigint; scale: number } | null): string {
  if (value === null) return "—";
  return `${(toNumber(value) * 100).toFixed(1)}%`;
}

/** Smallest, cheapest and largest placeable rungs — the U-shape, not three points. */
function strip(ladder: CostAtSize[]): CostAtSize[] {
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

function panel(): HTMLElement {
  let node = document.getElementById(PANEL_ID);
  if (node) return node;
  node = document.createElement("div");
  node.id = PANEL_ID;
  node.className = "pmvl-panel";
  node.setAttribute("role", "complementary");
  node.setAttribute("aria-label", "Estimated entry cost");
  document.body.appendChild(node);
  return node;
}

function render(contract: Contract, ladder: CostAtSize[]): void {
  const rows = strip(ladder);
  const node = panel();

  if (rows.length === 0) {
    node.innerHTML = `
      <div class="pmvl-head"><span class="pmvl-dot"></span>Entry cost</div>
      <p class="pmvl-note">No size on this contract could be filled at the depth
      currently on the book, so no cost is shown.</p>`;
    return;
  }

  const cheapest = cheapestPlaceable(rows);
  const assumptionLed = contract.venue === "polymarket";

  node.innerHTML = `
    <div class="pmvl-head"><span class="pmvl-dot"></span>Entry cost, per contract</div>
    <table class="pmvl-table">
      <thead><tr><th>Size</th><th>Costs</th><th>Over quote</th><th>Break-even</th></tr></thead>
      <tbody>
        ${rows
          .map(
            (row) => `<tr${row === cheapest ? ' class="pmvl-best"' : ""}>
              <td>${decToString(row.size).replace(/\.0+$/, "")}</td>
              <td>${cents(row.measuredCost)}</td>
              <td class="pmvl-warn">${percent(row.measuredPremiumRatio)}</td>
              <td>${probability(row.breakevenProbability)}</td>
            </tr>`,
          )
          .join("")}
      </tbody>
    </table>
    ${
      cheapest
        ? `<p class="pmvl-note pmvl-best-note">Cheapest placeable size:
           <strong>${decToString(cheapest.size).replace(/\.0+$/, "")}</strong>
           at ${cents(cheapest.measuredCost)} each.</p>`
        : ""
    }
    <p class="pmvl-note">
      Observed ask depth and published venue fee and rounding rules${
        assumptionLed
          ? ", plus an assumed bridge/gas allowance amortised over the position"
          : ""
      }. Sizes below the venue minimum and sizes the book cannot fill are excluded.
      Slippage is not included. No forecast, no recommendation — research only.
    </p>`;
}

function renderError(message: string): void {
  panel().innerHTML = `
    <div class="pmvl-head"><span class="pmvl-dot"></span>Entry cost</div>
    <p class="pmvl-note">${message}</p>`;
}

/* -------------------------------------------------------------------- run -- */

let timer: number | undefined;

async function update(): Promise<void> {
  const url = location.href;
  let contract: Contract | null = null;
  try {
    contract = await identify(url, document.body?.innerText ?? "", fetchJson);
  } catch {
    contract = null;
  }

  // No overlay at all when the contract cannot be confirmed against the venue's
  // own API. Silence is the correct output for "I do not know what you are
  // looking at"; a number would be a guess placed beside a live order form.
  if (!contract) {
    document.getElementById(PANEL_ID)?.remove();
    return;
  }

  try {
    const book = await loadBook(contract, fetchJson);
    if (!book) {
      renderError("The venue returned no order book for this contract just now.");
      return;
    }
    const terms = termsFromBook(contract, book);
    render(contract, costLadder(book.levels, terms, DEFAULT_ASSUMPTIONS));
  } catch (error) {
    renderError(`Could not read the live book: ${String(error)}`);
  }
}

function schedule(): void {
  window.clearInterval(timer);
  timer = window.setInterval(update, REFRESH_MS);
}

/**
 * Both venues are single-page apps, so navigating between contracts never
 * reloads the document and a one-shot script would keep showing the first
 * contract's costs on every subsequent page. Watching the URL is cruder than
 * hooking the router and does not break when the router changes.
 */
function watchNavigation(): void {
  let lastUrl = location.href;
  const observer = new MutationObserver(() => {
    if (location.href === lastUrl) return;
    lastUrl = location.href;
    void update();
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

if (document.body) {
  void update();
  schedule();
  watchNavigation();
}

export { strip, update };
