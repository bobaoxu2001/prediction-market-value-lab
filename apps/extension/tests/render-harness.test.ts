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

import { costAtSize, costLadder } from "../src/cost";
import { dec } from "../src/decimal";
import { panelHtml } from "../src/panel";
import {
  kalshiBook,
  polymarketBook,
  resolveKalshi,
  resolvePolymarket,
  termsFromBook,
} from "../src/venues";

const HERE = dirname(fileURLToPath(import.meta.url));

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

    // Fixed so the preview is byte-stable between runs; the panel prints a
    // relative age, which would otherwise drift with the wall clock.
    const NOW = 1_760_000_000_000;

    const kc = (await resolveKalshi("KXMLBGAME-26AUG092020HOUSD-SD", kalshiFetch))!;
    const kb = (await kalshiBook(kc.ids.yes, kalshiFetch))!;
    const kTerms = termsFromBook(kc, kb);
    // A one-lot on a deeply liquid contract. The size is theirs and is shown as
    // such, but the book is flat past the smallest rungs so there is nothing to
    // save and the panel must say nothing rather than manufacture advice.
    const kalshiPanel = panelHtml({
      venue: kc.venue,
      side: "yes",
      ladder: costLadder(kb.levels, kTerms),
      observedAt: NOW - 8_000,
      yourRow: costAtSize(kb.levels, dec("1"), kTerms),
      now: NOW,
    });

    const pc = (await resolvePolymarket("strait-of-hormuz", polyFetch))!;
    const pb = (await polymarketBook(pc.ids.yes, polyFetch))!;
    const pTerms = termsFromBook(pc, pb);
    // Ten contracts on Polymarket, where the fixed bridge allowance amortised over
    // a small position dominates: the case where the overlay has real money to
    // point at.
    const polyPanel = panelHtml({
      venue: pc.venue,
      side: "yes",
      ladder: costLadder(pb.levels, pTerms),
      observedAt: NOW - 45_000,
      yourRow: costAtSize(pb.levels, dec("10"), pTerms),
      now: NOW,
    });

    // Both must actually contain figures, or the preview is of an empty panel.
    expect(kalshiPanel).toContain("¢");
    expect(polyPanel).toContain("¢");
    // The trader's own size is present and marked as theirs.
    expect(kalshiPanel).toContain("pmvl-yours");
    // Freshness is stated, since it is the whole reason this is not the website.
    expect(kalshiPanel).toContain("8s ago");
    // The side is named, so a NO panel can never be read as a YES one.
    expect(kalshiPanel).toContain("Entry cost · YES");
    // On the flat, liquid book there is nothing to save and nothing is claimed.
    expect(kalshiPanel).not.toContain("less on an order this size");
    // On the thin one there is, and it is stated in dollars.
    expect(polyPanel).toContain("less on an order this size");

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
  <div class="card"><h2>Kalshi · liquid book, nothing to save</h2>
    <div class="contract">${kc.title}</div>
    <div id="pmvl-cost-overlay" class="pmvl-panel preview-panel">${kalshiPanel}</div></div>
  <div class="card"><h2>Polymarket · a cheaper size exists</h2>
    <div class="contract">${pc.title}</div>
    <div id="pmvl-cost-overlay-2" class="pmvl-panel preview-panel">${polyPanel}</div></div>
</div>`;

    const out = resolve(HERE, "../out/overlay-preview.html");
    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, html);
    expect(html.length).toBeGreaterThan(500);
  });
});
