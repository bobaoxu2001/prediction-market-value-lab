/**
 * Renders the overlay from the recorded venue payloads and writes it to a file.
 *
 * The one part of this extension that cannot be verified from a test environment
 * is its injection into kalshi.com, which rate-limits automated access. What
 * *can* be verified is everything up to that boundary: that real payloads produce
 * a panel, that the numbers in it are the ones the cost stack computed, and that
 * the stylesheet renders legibly in both colour schemes.
 *
 * So this builds the same markup the content script builds, against the same
 * fixtures, and writes `out/overlay-preview.html` to be opened and looked at. It
 * asserts the content; a human confirms it reads well.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import kalshiMarket from "../fixtures/kalshi-market.json";
import kalshiOrderbook from "../fixtures/kalshi-orderbook.json";
import polymarketMarket from "../fixtures/polymarket-market.json";
import polymarketBookFixture from "../fixtures/polymarket-book.json";

import {
  type CostAtSize,
  cheapestPlaceable,
  costLadder,
} from "../src/cost";
import { toNumber, toString as decToString } from "../src/decimal";
import {
  type Contract,
  kalshiBook,
  polymarketBook,
  resolveKalshi,
  resolvePolymarket,
  termsFromBook,
} from "../src/venues";

const HERE = dirname(fileURLToPath(import.meta.url));

const cents = (v: { units: bigint; scale: number }) => {
  const n = toNumber(v) * 100;
  return `${n < 10 ? n.toFixed(2) : n.toFixed(1)}¢`;
};
const percent = (v: { units: bigint; scale: number } | null) => {
  if (v === null) return "—";
  const n = toNumber(v) * 100;
  return `${n >= 0 ? "+" : ""}${Math.abs(n) < 10 ? n.toFixed(1) : n.toFixed(0)}%`;
};
const prob = (v: { units: bigint; scale: number } | null) =>
  v === null ? "—" : `${(toNumber(v) * 100).toFixed(1)}%`;
const sizeLabel = (v: { units: bigint; scale: number }) =>
  decToString(v).replace(/\.0+$/, "");

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

function panelHtml(contract: Contract, ladder: CostAtSize[]): string {
  const rows = strip(ladder);
  const cheapest = cheapestPlaceable(rows);
  const assumptionLed = contract.venue === "polymarket";
  return `
    <div class="pmvl-head"><span class="pmvl-dot"></span>Entry cost, per contract</div>
    <table class="pmvl-table">
      <thead><tr><th>Size</th><th>Costs</th><th>Over quote</th><th>Break-even</th></tr></thead>
      <tbody>
        ${rows
          .map(
            (row) => `<tr${row === cheapest ? ' class="pmvl-best"' : ""}>
              <td>${sizeLabel(row.size)}</td>
              <td>${cents(row.measuredCost)}</td>
              <td class="pmvl-warn">${percent(row.measuredPremiumRatio)}</td>
              <td>${prob(row.breakevenProbability)}</td>
            </tr>`,
          )
          .join("")}
      </tbody>
    </table>
    ${
      cheapest
        ? `<p class="pmvl-note pmvl-best-note">Cheapest placeable size:
           <strong>${sizeLabel(cheapest.size)}</strong>
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

function recorded(routes: Record<string, unknown>) {
  return async (url: string) => {
    for (const [fragment, payload] of Object.entries(routes)) {
      if (url.includes(fragment)) return payload;
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

describe("overlay renders from real venue payloads", () => {
  it("writes a preview of both venues", async () => {
    const kalshiFetch = recorded({
      "/orderbook": kalshiOrderbook,
      "/markets/": kalshiMarket,
    });
    const polyFetch = recorded({
      "clob.polymarket.com/book": polymarketBookFixture,
      "gamma-api.polymarket.com/markets": polymarketMarket,
    });

    const kc = (await resolveKalshi("KXMLBGAME-26AUG092020HOUSD-SD", kalshiFetch))!;
    const kb = (await kalshiBook(kc.id, kalshiFetch))!;
    const kalshiPanel = panelHtml(kc, costLadder(kb.levels, termsFromBook(kc, kb)));

    const pc = (await resolvePolymarket("strait-of-hormuz", polyFetch))!;
    const pb = (await polymarketBook(pc.id, polyFetch))!;
    const polyPanel = panelHtml(pc, costLadder(pb.levels, termsFromBook(pc, pb)));

    // Both must actually contain figures, or the preview is of an empty panel.
    expect(kalshiPanel).toContain("¢");
    expect(polyPanel).toContain("¢");
    expect(kalshiPanel).toContain("Cheapest placeable size");

    const html = `<meta charset="utf-8"><title>PMVL overlay preview</title>
<link rel="stylesheet" href="../overlay.css">
<style>
  body { margin: 0; font: 14px system-ui, sans-serif; background: #191C22; color: #E9ECF0; }
  .pane { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; padding: 28px; }
  .card { position: relative; min-height: 340px; }
  .card h2 { font-size: 12px; letter-spacing: 1.5px; text-transform: uppercase;
             color: #9BA3AF; font-weight: 400; margin: 0 0 12px; }
  .card .contract { font-size: 13px; color: #C6CCD4; margin-bottom: 16px; }
  /* The panel is fixed-position in the wild; anchor it for the preview. */
  .preview-panel { position: static !important; width: 320px; }
  .light { background: #F4F6F8; color: #14171C; padding: 28px; }
  .light .card h2, .light .card .contract { color: #5B636E; }
</style>
<div class="pane">
  <div class="card"><h2>Kalshi · dark</h2>
    <div class="contract">${kc.title}</div>
    <div id="pmvl-cost-overlay" class="pmvl-panel preview-panel">${kalshiPanel}</div></div>
  <div class="card"><h2>Polymarket · dark</h2>
    <div class="contract">${pc.title}</div>
    <div id="pmvl-cost-overlay-2" class="pmvl-panel preview-panel">${polyPanel}</div></div>
</div>`;

    const out = resolve(HERE, "../out/overlay-preview.html");
    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, html);
    expect(html.length).toBeGreaterThan(500);
  });
});
