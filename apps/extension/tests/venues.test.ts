/**
 * Venue adapters against payloads actually returned by the venues.
 *
 * The fixtures in `fixtures/kalshi-*.json` and `fixtures/polymarket-*.json` were
 * captured from the live public endpoints, not hand-written. Hand-written venue
 * payloads test that the parser matches the author's belief about the format,
 * which is the belief most likely to be wrong.
 */

import { describe, expect, it } from "vitest";

import kalshiMarket from "../fixtures/kalshi-market.json";
import kalshiOrderbook from "../fixtures/kalshi-orderbook.json";
import polymarketMarket from "../fixtures/polymarket-market.json";
import polymarketBookFixture from "../fixtures/polymarket-book.json";

import { cheapestPlaceable, costLadder } from "../src/cost";
import { dec, gt, lt, toNumber, toString, ZERO } from "../src/decimal";
import {
  identify,
  kalshiBook,
  kalshiCandidates,
  kalshiEventTicker,
  marketNamedInPanel,
  polymarketBook,
  polymarketSlug,
  resolveKalshi,
  resolvePolymarket,
  termsFromBook,
  yearsUntil,
} from "../src/venues";

/** Serves the recorded payloads, and fails loudly on an unexpected URL. */
function recorded(routes: Record<string, unknown>) {
  return async (url: string) => {
    for (const [fragment, payload] of Object.entries(routes)) {
      if (url.includes(fragment)) return payload;
    }
    throw new Error(`unexpected request: ${url}`);
  };
}

/* ------------------------------------------------------------ identification */

describe("finding the contract on the page", () => {
  it("reads a Kalshi ticker out of the URL", () => {
    const found = kalshiCandidates(
      "https://kalshi.com/markets/kxmlbgame/x?ticker=KXMLBGAME-26AUG092020HOUSD-SD",
      "",
    );
    expect(found).toContain("KXMLBGAME-26AUG092020HOUSD-SD");
  });

  it("prefers the URL's ticker over one mentioned in the body", () => {
    // A sidebar naming other contracts must not outrank the page being viewed.
    const found = kalshiCandidates(
      "https://kalshi.com/x?ticker=KXMLBGAME-26AUG092020HOUSD-SD",
      "see also KXMLBGAME-26AUG091610TBSEA-TB",
    );
    expect(found[0]).toBe("KXMLBGAME-26AUG092020HOUSD-SD");
  });

  it("ignores uppercase prose that is not a ticker", () => {
    expect(kalshiCandidates("https://kalshi.com/", "BUY YES NOW - LIMITED TIME")).toEqual(
      [],
    );
  });

  it("bounds how many candidates one page can produce", () => {
    const body = Array.from(
      { length: 40 },
      (_, i) => `KXTEST-26AUG${String(i).padStart(2, "0")}AAA-YES`,
    ).join(" ");
    expect(kalshiCandidates("https://kalshi.com/", body).length).toBeLessThanOrEqual(8);
  });

  it("survives a locale segment in the Polymarket path", () => {
    // The live site serves `/zh/event/...`. The old pattern required `event` to
    // follow the hostname directly, so on any localised page the slug came back
    // null and the overlay never rendered at all.
    expect(
      polymarketSlug("https://polymarket.com/zh/event/strait-of-hormuz/strait-of-hormuz"),
    ).toBe("strait-of-hormuz");
    expect(polymarketSlug("https://polymarket.com/pt-br/event/a/b")).toBe("b");
  });

  it("reads the event ticker out of a Kalshi path", () => {
    // Kalshi puts no market ticker in the URL. The last segment is the *event*
    // ticker, lower-cased, and it is the only identifier the address carries.
    expect(
      kalshiEventTicker(
        "https://kalshi.com/markets/kxmlbgame/professional-baseball-game/kxmlbgame-26aug101907bostor",
      ),
    ).toBe("KXMLBGAME-26AUG101907BOSTOR");
    expect(kalshiEventTicker("https://kalshi.com/markets")).toBeNull();
  });

  it("finds tickers in markup that never reach the rendered text", () => {
    // On the live page `document.body.innerText` contains no ticker at all; they
    // exist only in the HTML. Scanning the text found nothing and the overlay
    // was absent on every Kalshi page.
    const html = '<div data-ticker="KXMLBGAME-26AUG101907BOSTOR-BOS"></div>';
    expect(kalshiCandidates("https://kalshi.com/markets/x", html)).toContain(
      "KXMLBGAME-26AUG101907BOSTOR-BOS",
    );
  });

  it("picks the selected outcome by how often the panel names it", () => {
    // The real panel reads "Boston vs Toronto / Boston / YES 62c NO 39c": both
    // outcomes appear, so a presence test matched both and refused every time.
    // The selected one is named once more.
    const markets = [
      { ticker: "…-BOS", yes_sub_title: "Boston" },
      { ticker: "…-TOR", yes_sub_title: "Toronto" },
    ];
    const panel = "BUY SELL DOLLARS Boston vs Toronto Boston YES 62¢ NO 39¢";
    expect(marketNamedInPanel(markets, panel)?.ticker).toBe("…-BOS");
  });

  it("refuses when both outcomes are named equally often", () => {
    const markets = [
      { ticker: "…-BOS", yes_sub_title: "Boston" },
      { ticker: "…-TOR", yes_sub_title: "Toronto" },
    ];
    expect(marketNamedInPanel(markets, "Boston vs Toronto")).toBeNull();
  });

  it("renders nothing when the event is known but the outcome is not", async () => {
    // A Bitcoin threshold board carries 80 strikes in one event and shows no
    // order ticket until a strike is picked, so the panel names nothing. The
    // markup scan happily returned the first ticker in the HTML — a $64,000
    // strike nobody had selected — and priced it as confidently as if it were
    // the trader's. Knowing the event and not the outcome must render nothing.
    const strikes = Array.from({ length: 5 }, (_, i) => ({
      ticker: `KXBTCD-26AUG1117-T${63000 + i * 250}.99`,
      yes_sub_title: `$${63000 + i * 250} or above`,
    }));
    const fetchJson = async (url: string) => {
      if (url.includes("event_ticker=")) return { markets: strikes };
      throw new Error(`should not have fallen back to a per-ticker lookup: ${url}`);
    };

    const contract = await identify(
      "https://kalshi.com/markets/kxbtcd/bitcoin-price-abovebelow/kxbtcd-26aug1117",
      strikes.map((s) => s.ticker).join(" "),
      fetchJson,
      "", // no order panel on screen yet
    );
    expect(contract).toBeNull();
  });

  it("still resolves when the panel does name the outcome", async () => {
    const strikes = [
      { ticker: "KXBTCD-26AUG1117-T63999.99", yes_sub_title: "$64,000 or above" },
      { ticker: "KXBTCD-26AUG1117-T64249.99", yes_sub_title: "$64,250 or above" },
    ];
    const fetchJson = async (url: string) => {
      if (url.includes("event_ticker=")) return { markets: strikes };
      throw new Error("unexpected");
    };
    const contract = await identify(
      "https://kalshi.com/markets/kxbtcd/x/kxbtcd-26aug1117",
      "",
      fetchJson,
      "Buy $64,250 or above · YES 31¢ · $64,250 or above",
    );
    expect(contract?.ids.yes).toBe("KXBTCD-26AUG1117-T64249.99");
  });

  it("takes the market slug from a Polymarket event URL", () => {
    expect(
      polymarketSlug("https://polymarket.com/event/some-event/the-market-slug?x=1"),
    ).toBe("the-market-slug");
    expect(polymarketSlug("https://polymarket.com/event/lone-slug")).toBe("lone-slug");
    expect(polymarketSlug("https://example.com/event/x")).toBeNull();
  });
});

/* -------------------------------------------------------------------- kalshi */

describe("kalshi", () => {
  const fetchJson = recorded({
    "/orderbook": kalshiOrderbook,
    "/markets/": kalshiMarket,
  });

  it("resolves the contract from the real market payload", async () => {
    const contract = await resolveKalshi("KXMLBGAME-26AUG092020HOUSD-SD", fetchJson);
    expect(contract).not.toBeNull();
    expect(contract!.venue).toBe("kalshi");
    expect(contract!.title).toContain("San Diego");
    // Null fee_multiplier in the payload must fall back to the published rate,
    // not to zero, which would silently price every Kalshi order as fee-free.
    expect(toString(contract!.terms.feeRate)).toBe("0.07");
  });

  it("derives YES asks from resting NO bids", async () => {
    const book = await kalshiBook("KXMLBGAME-26AUG092020HOUSD-SD", fetchJson);
    expect(book).not.toBeNull();
    expect(book!.levels.length).toBeGreaterThan(10);

    // A NO bid at X is a YES ask at 1 - X, so every derived ask sits inside the
    // unit interval. Getting the derivation backwards would put them outside it.
    for (const level of book!.levels) {
      expect(gt(level.price, ZERO)).toBe(true);
      expect(lt(level.price, dec("1"))).toBe(true);
      expect(gt(level.size, ZERO)).toBe(true);
    }
  });

  it("prices a ladder whose cheapest size is not the smallest", async () => {
    const contract = await resolveKalshi("KXMLBGAME-26AUG092020HOUSD-SD", fetchJson);
    const book = await kalshiBook(contract!.ids.yes, fetchJson);
    const ladder = costLadder(book!.levels, termsFromBook(contract!, book!));

    expect(ladder.length).toBeGreaterThan(3);
    const cheapest = cheapestPlaceable(ladder)!;
    const one = ladder.find((r) => toNumber(r.size) === 1)!;
    // The whole premise of the overlay: a one-lot is not the cheapest way in.
    expect(toNumber(cheapest.measuredCost)).toBeLessThanOrEqual(
      toNumber(one.measuredCost),
    );
  });

  it("never reports a break-even above certainty", async () => {
    const contract = await resolveKalshi("KXMLBGAME-26AUG092020HOUSD-SD", fetchJson);
    const book = await kalshiBook(contract!.ids.yes, fetchJson);
    for (const row of costLadder(book!.levels, contract!.terms)) {
      if (row.breakevenProbability === null) continue;
      expect(toNumber(row.breakevenProbability)).toBeLessThan(1);
      expect(toNumber(row.breakevenProbability)).toBeGreaterThan(0);
    }
  });
});

/* ---------------------------------------------------------------- polymarket */

describe("polymarket", () => {
  const fetchJson = recorded({
    "clob.polymarket.com/book": polymarketBookFixture,
    "gamma-api.polymarket.com/markets": polymarketMarket,
  });

  it("resolves the YES token from the gamma payload", async () => {
    const contract = await resolvePolymarket("strait-of-hormuz", fetchJson);
    expect(contract).not.toBeNull();
    expect(contract!.venue).toBe("polymarket");
    // clobTokenIds arrives as a JSON-encoded string, YES first.
    expect(contract!.ids.yes).toMatch(/^\d{20,}$/);
  });

  it("reads depth, and the venue terms the book reports with it", async () => {
    const book = await polymarketBook("token", fetchJson);
    expect(book).not.toBeNull();
    expect(book!.levels.length).toBeGreaterThan(10);
    expect(toString(book!.minOrderSize!)).toBe("5");
  });

  it("marks sizes below the venue minimum as unplaceable", async () => {
    const contract = await resolvePolymarket("strait-of-hormuz", fetchJson);
    const book = await polymarketBook(contract!.ids.yes, fetchJson);
    const ladder = costLadder(book!.levels, termsFromBook(contract!, book!));

    const one = ladder.find((r) => toNumber(r.size) === 1);
    // A 5-contract minimum makes a one-lot arithmetic, not an order. The
    // amortised bridge cost over one contract is what produced a +50010%
    // premium on a share card before this flag was respected.
    expect(one?.belowMinOrderSize).toBe(true);
    expect(cheapestPlaceable(ladder)!.belowMinOrderSize).toBe(false);
  });

  it("sorts a book the venue may return worst-price-first", async () => {
    const book = await polymarketBook("token", fetchJson);
    const ladder = costLadder(book!.levels, {
      venue: "polymarket",
      feeRate: dec("0"),
      feeType: "",
      tickSize: dec("0.01"),
      minOrderSize: dec("5"),
      yearsToResolution: dec("0"),
    });
    // Walking cheapest-first means entry price rises with size, never falls.
    const prices = ladder.map((r) => toNumber(r.entryPrice));
    for (let i = 1; i < prices.length; i += 1) {
      expect(prices[i]).toBeGreaterThanOrEqual(prices[i - 1]);
    }
  });
});

/* -------------------------------------------------------------------- shared */

describe("time to resolution", () => {
  const now = Date.parse("2026-08-09T00:00:00Z");

  it("is zero for a missing or past date, disabling the capital charge", () => {
    expect(yearsUntil(undefined, now)).toEqual(dec("0"));
    expect(yearsUntil("2026-08-08T00:00:00Z", now)).toEqual(dec("0"));
  });

  it("is about a year out for a date a year away", () => {
    expect(toNumber(yearsUntil("2027-08-09T00:00:00Z", now))).toBeCloseTo(1, 2);
  });
});
