/**
 * Identifying the contract on screen, and pulling its live book from the venue.
 *
 * The book comes from the venue's own public, unauthenticated endpoint — the same
 * ones the research pipeline uses — rather than from our API. That is the whole
 * point of the extension: the hosted deployment serves a frozen snapshot whose
 * freshest quote was eleven days old when this was written, and "what does this
 * cost right now" cannot be answered from an eleven-day-old book.
 *
 * ## Identification fails closed
 *
 * Discovery produces *candidates* and every candidate is checked against the
 * public API before anything is rendered. A ticker that does not resolve to a real
 * market produces no overlay at all. A wrong guess that silently rendered would
 * put a confident, wrong cost next to somebody's order ticket, which is worse
 * than the extension appearing not to work.
 *
 * ## What the real pages turned out to look like
 *
 * The first version of this file was written without being able to load either
 * venue, and every assumption in it about their URLs and markup was wrong. Both
 * were then observed directly, on 10 August 2026:
 *
 * **Kalshi** puts no market ticker in the URL and none in the rendered text. The
 * last path segment is the *event* ticker in lower case
 * (`.../kxmlbgame-26aug101907bostor`), and the market tickers appear only inside
 * the HTML. Scanning `innerText` therefore found nothing at all and the overlay
 * never appeared. The event ticker is now taken from the path and expanded via
 * `/markets?event_ticker=`, which returns both sides of the contract.
 *
 * **Polymarket** serves localised paths — `polymarket.com/zh/event/<slug>/<slug>`
 * — and the old pattern required `event` to follow the hostname directly, so on
 * any non-English locale the slug came back null and, again, nothing rendered.
 */

import { type BookLevel, type ContractTerms, type Venue } from "./cost";
import { type Dec, ONE, dec, gt, mul, quantizeUsd, sub } from "./decimal";

export const KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2";
export const POLYMARKET_GAMMA = "https://gamma-api.polymarket.com";
export const POLYMARKET_CLOB = "https://clob.polymarket.com";

/**
 * Kalshi market tickers: a series, an event suffix, and the side.
 *
 * `KXMLBGAME-26AUG092020HOUSD-SD`. Deliberately anchored on the `KX` prefix and
 * the two-hyphen shape rather than accepting any uppercase run, because the page
 * is full of uppercase text and a loose pattern would produce a stream of
 * candidates that each cost an API call to reject.
 */
const KALSHI_TICKER = /\bKX[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9.]+\b/g;

/**
 * The event ticker Kalshi puts in the last path segment, upper-cased.
 *
 * `/markets/kxmlbgame/professional-baseball-game/kxmlbgame-26aug101907bostor`
 * carries two hyphen-separated parts rather than a market ticker's three, and it
 * is the only machine-readable identifier the page's URL contains.
 */
const KALSHI_EVENT_IN_PATH = /\/(kx[a-z0-9]+-[a-z0-9]+)(?:[/?#]|$)/i;

export function kalshiEventTicker(url: string): string | null {
  const match = KALSHI_EVENT_IN_PATH.exec(url);
  return match ? match[1].toUpperCase() : null;
}

/** Which side of the contract is being bought. */
export type Side = "yes" | "no";

export interface Contract {
  venue: Venue;
  /**
   * The venue's own identifier for each side.
   *
   * Kalshi uses one ticker for both — the side selects which set of resting bids
   * to invert into asks. Polymarket issues a separate ERC-1155 token per side
   * with an independent book, so `no` is a different id and may be absent.
   */
  ids: { yes: string; no: string | null };
  title: string;
  terms: ContractTerms;
}

export interface LiveBook {
  levels: BookLevel[];
  observedAt: number;
  /**
   * Venue terms carried on the book response itself, when it supplies them.
   *
   * Polymarket's CLOB returns `min_order_size` and `tick_size` alongside the
   * depth. Those are better than anything derived elsewhere because they arrived
   * with the same snapshot of the same market, and the minimum in particular
   * decides whether a rung is merely expensive or actually unplaceable.
   */
  minOrderSize?: Dec;
  tickSize?: Dec;
}

/* ------------------------------------------------------------------ kalshi -- */

/**
 * Every plausible Kalshi ticker on the page, best guess first.
 *
 * The URL is tried before the body: a market page's address names the contract
 * being looked at, whereas the body may also mention related contracts in a
 * sidebar, and showing costs for the wrong one is the failure to avoid.
 */
export function kalshiCandidates(url: string, pageSource: string): string[] {
  const seen = new Set<string>();
  const push = (raw: string) => seen.add(raw.toUpperCase());

  for (const match of decodeURIComponent(url).matchAll(KALSHI_TICKER)) push(match[0]);
  // `pageSource` is the page's HTML, not its rendered text. On the live site the
  // ticker appears only in the markup - reading `innerText` returned nothing and
  // the overlay never appeared on any Kalshi page.
  for (const match of pageSource.matchAll(KALSHI_TICKER)) push(match[0]);
  // Bounded: each candidate costs a request, and a page that yields dozens is a
  // page this extension has not understood.
  return [...seen].slice(0, 8);
}

export interface KalshiMarket {
  ticker: string;
  title?: string;
  yes_sub_title?: string;
  fee_type?: string;
  fee_multiplier?: number | string;
  minimum_order_size?: number | string;
  close_time?: string;
  expected_expiration_time?: string;
}

/** Published Kalshi taker rate at multiplier 1. */
const KALSHI_BASE_RATE = dec("0.07");

export async function resolveKalshi(
  ticker: string,
  fetchJson: FetchJson,
): Promise<Contract | null> {
  const payload = await fetchJson(`${KALSHI_API}/markets/${encodeURIComponent(ticker)}`);
  const market = (payload as { market?: KalshiMarket } | null)?.market;
  if (!market?.ticker) return null;

  return contractFromKalshiMarket(market);
}

export function contractFromKalshiMarket(market: KalshiMarket): Contract {
  const multiplier =
    market.fee_multiplier === undefined || market.fee_multiplier === null
      ? dec("1")
      : dec(String(market.fee_multiplier));
  const resolution = market.expected_expiration_time ?? market.close_time;

  return {
    venue: "kalshi",
    ids: { yes: market.ticker, no: market.ticker },
    title: [market.title, market.yes_sub_title].filter(Boolean).join(" — "),
    terms: {
      venue: "kalshi",
      feeRate: mul(KALSHI_BASE_RATE, multiplier),
      feeType: market.fee_type ?? "",
      tickSize: dec("0.01"),
      minOrderSize: dec(String(market.minimum_order_size ?? 1)),
      yearsToResolution: yearsUntil(resolution),
    },
  };
}

/**
 * Kalshi's book is bids-only, so asks are derived: a resting bid on one side at X
 * is an ask on the other at $1 − X, carrying the same size. Mirrors
 * `KalshiProvider.parse_orderbook`.
 *
 * Both sides come back in one payload, so pricing NO costs no extra request. That
 * matters because a trader on the NO side reading YES numbers is not looking at a
 * slightly different figure, they are looking at the other contract.
 */
export async function kalshiBook(
  ticker: string,
  fetchJson: FetchJson,
  side: Side = "yes",
): Promise<LiveBook | null> {
  const payload = await fetchJson(
    `${KALSHI_API}/markets/${encodeURIComponent(ticker)}/orderbook?depth=100`,
  );
  const book =
    (payload as Record<string, any> | null)?.orderbook_fp ??
    (payload as Record<string, any> | null)?.orderbook;
  if (!book) return null;

  // YES asks come from NO bids, and NO asks from YES bids.
  const opposing: Array<[string, string]> =
    side === "yes" ? (book.no_dollars ?? book.no ?? []) : (book.yes_dollars ?? book.yes ?? []);

  const levels: BookLevel[] = [];
  for (const entry of opposing) {
    if (!Array.isArray(entry) || entry.length < 2) continue;
    const askPrice = quantizeUsd(sub(ONE, dec(String(entry[0]))));
    if (!gt(askPrice, dec("0"))) continue;
    levels.push({ price: askPrice, size: dec(String(entry[1])) });
  }
  if (levels.length === 0) return null;
  return { levels, observedAt: Date.now() };
}

/* ------------------------------------------------------------- polymarket -- */

/**
 * `polymarket.com/event/<event-slug>/<market-slug>` — the market slug is last.
 *
 * The optional locale segment is load-bearing. The live site serves
 * `polymarket.com/zh/event/...`, and a pattern anchored directly on the hostname
 * matched nothing there, so the overlay was absent on every localised page.
 */
export function polymarketSlug(url: string): string | null {
  const match =
    /polymarket\.com\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?(?:event|market)\/([^/?#]+)(?:\/([^/?#]+))?/i.exec(
      url,
    );
  if (!match) return null;
  return match[2] ?? match[1];
}

interface GammaMarket {
  question?: string;
  clobTokenIds?: string;
  conditionId?: string;
  orderPriceMinTickSize?: number | string;
  endDate?: string;
  fee?: number | string;
}

export async function resolvePolymarket(
  slug: string,
  fetchJson: FetchJson,
): Promise<Contract | null> {
  const payload = await fetchJson(
    `${POLYMARKET_GAMMA}/markets?slug=${encodeURIComponent(slug)}`,
  );
  const market = Array.isArray(payload) ? (payload[0] as GammaMarket) : null;
  if (!market?.clobTokenIds) return null;

  // Gamma returns the token ids as a JSON-encoded string, YES first.
  let tokens: string[] = [];
  try {
    tokens = JSON.parse(market.clobTokenIds) as string[];
  } catch {
    tokens = [];
  }
  const yesToken = tokens[0] ?? null;
  if (!yesToken) return null;

  return {
    venue: "polymarket",
    ids: { yes: yesToken, no: tokens[1] ?? null },
    title: market.question ?? slug,
    terms: {
      venue: "polymarket",
      feeRate: dec(String(market.fee ?? "0")),
      feeType: "",
      tickSize: dec(String(market.orderPriceMinTickSize ?? "0.001")),
      // Published minimum. Amortising the bridge cost below it produces a real
      // number for an order the venue will not accept.
      minOrderSize: dec("5"),
      yearsToResolution: yearsUntil(market.endDate),
    },
  };
}

export async function polymarketBook(
  tokenId: string,
  fetchJson: FetchJson,
): Promise<LiveBook | null> {
  const payload = (await fetchJson(
    `${POLYMARKET_CLOB}/book?token_id=${encodeURIComponent(tokenId)}`,
  )) as {
    asks?: Array<{ price: string; size: string }>;
    min_order_size?: string | number;
    tick_size?: string | number;
  } | null;
  const asks = payload?.asks ?? [];
  // Not sorted here: `walkBook` orders levels cheapest-first itself, and the
  // CLOB does not document which way round it returns them.
  const levels = asks
    .filter((a) => a?.price !== undefined && a?.size !== undefined)
    .map((a) => ({ price: dec(String(a.price)), size: dec(String(a.size)) }));
  if (levels.length === 0) return null;
  return {
    levels,
    observedAt: Date.now(),
    minOrderSize:
      payload?.min_order_size === undefined
        ? undefined
        : dec(String(payload.min_order_size)),
    tickSize:
      payload?.tick_size === undefined ? undefined : dec(String(payload.tick_size)),
  };
}

/* ------------------------------------------------------------------ shared -- */

export type FetchJson = (url: string) => Promise<unknown>;

/** Years until a timestamp, or zero. Zero disables the capital-cost component. */
export function yearsUntil(iso: string | undefined | null, now = Date.now()): Dec {
  if (!iso) return dec("0");
  const target = Date.parse(iso);
  if (Number.isNaN(target) || target <= now) return dec("0");
  const years = (target - now) / (365.25 * 24 * 3600 * 1000);
  // Six decimal places is about thirty seconds, far finer than the capital
  // charge can meaningfully resolve.
  return dec(years.toFixed(6));
}

/**
 * Every market on a Kalshi event, from the event ticker in the page's path.
 *
 * The path is the only identifier a Kalshi market page's URL carries, and it
 * names the event rather than either side of it, so a game returns two markets.
 */
export async function kalshiEventMarkets(
  eventTicker: string,
  fetchJson: FetchJson,
): Promise<KalshiMarket[]> {
  const payload = await fetchJson(
    `${KALSHI_API}/markets?event_ticker=${encodeURIComponent(eventTicker)}&limit=100`,
  );
  const markets = (payload as { markets?: KalshiMarket[] } | null)?.markets;
  return Array.isArray(markets) ? markets.filter((m) => m?.ticker) : [];
}

/**
 * Which of an event's markets the order ticket is actually on.
 *
 * Each market's `yes_sub_title` is the outcome's name — "Boston", "Toronto" —
 * and the order panel prints the one it is set to. Matching on that is how the
 * two sides of a game are told apart without hard-coding either venue's markup.
 *
 * Selected by **occurrence count**, not by presence. The panel carries the
 * matchup title as well as the selection, so on the live page it reads
 * "Boston vs Toronto / Boston / YES 62¢ NO 39¢": both outcomes are present, and
 * a presence test matched both and refused every time. The selected one is named
 * once more than the other.
 *
 * A tie returns null. Two outcomes named equally often is a panel that has not
 * said which is selected, and picking would be a coin flip over which contract
 * to price.
 */
export function marketNamedInPanel(
  markets: KalshiMarket[],
  panelText: string,
): KalshiMarket | null {
  const haystack = panelText.toLowerCase();
  const scored = markets
    .map((market) => {
      const label = (market.yes_sub_title ?? "").trim().toLowerCase();
      if (label.length < 2) return { market, mentions: 0 };
      return { market, mentions: haystack.split(label).length - 1 };
    })
    .filter((row) => row.mentions > 0)
    .sort((a, b) => b.mentions - a.mentions);

  if (scored.length === 0) return null;
  if (scored.length === 1) return scored[0].market;
  return scored[0].mentions > scored[1].mentions ? scored[0].market : null;
}

/** Identify the contract on the page, or return null and render nothing. */
export async function identify(
  url: string,
  pageSource: string,
  fetchJson: FetchJson,
  panelText = "",
): Promise<Contract | null> {
  if (/(^|\.)polymarket\.com/.test(new URL(url).hostname)) {
    const slug = polymarketSlug(url);
    return slug ? resolvePolymarket(slug, fetchJson) : null;
  }

  // The event ticker in the path is the reliable identifier; the scan of the
  // markup is a fallback for pages whose URL does not carry one.
  const eventTicker = kalshiEventTicker(url);
  if (eventTicker) {
    const markets = await kalshiEventMarkets(eventTicker, fetchJson);
    if (markets.length > 0) {
      const chosen =
        markets.length === 1 ? markets[0] : marketNamedInPanel(markets, panelText);
      // Deliberately no fallback from here.
      //
      // Once the event is known, the markup scan is not a second opinion - it is
      // a worse one. A Bitcoin threshold board carries 80 strikes in one event
      // and shows no order ticket until the trader picks one, so the panel names
      // nothing and the scan happily returned the first ticker in the HTML:
      // `KXBTCD-26AUG1117-T63999.99`, a $64,000 strike nobody had selected,
      // priced as confidently as if it were theirs.
      //
      // Knowing the event and not the outcome is precisely the state in which
      // this must render nothing.
      return chosen ? contractFromKalshiMarket(chosen) : null;
    }
  }

  for (const ticker of kalshiCandidates(url, pageSource)) {
    const contract = await resolveKalshi(ticker, fetchJson);
    if (contract) return contract;
  }
  return null;
}

export async function loadBook(
  contract: Contract,
  fetchJson: FetchJson,
  side: Side = "yes",
): Promise<LiveBook | null> {
  if (contract.venue === "kalshi") {
    return kalshiBook(contract.ids.yes, fetchJson, side);
  }
  const token = side === "yes" ? contract.ids.yes : contract.ids.no;
  // A Polymarket market with no NO token has no NO book to price. Returning null
  // shows the "no book" state rather than quietly pricing the other side.
  return token ? polymarketBook(token, fetchJson) : null;
}

/**
 * Terms with anything the book itself reported taking precedence.
 *
 * The minimum order size decides whether a rung is unplaceable rather than
 * merely expensive, so when the depth response states it, that is the number to
 * believe — it came from the same snapshot of the same market as the depth.
 */
export function termsFromBook(contract: Contract, book: LiveBook): ContractTerms {
  return {
    ...contract.terms,
    minOrderSize: book.minOrderSize ?? contract.terms.minOrderSize,
    tickSize: book.tickSize ?? contract.terms.tickSize,
  };
}
