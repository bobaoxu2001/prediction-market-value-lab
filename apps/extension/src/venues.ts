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
 * Kalshi's own site rate-limits automated access, so the DOM and URL shapes could
 * not be verified from the development environment. Rather than guess and hope,
 * discovery produces *candidates* and every candidate is checked against the
 * public API before anything is rendered. A ticker that does not resolve to a real
 * market produces no overlay at all.
 *
 * That ordering matters. A wrong guess that silently rendered would put a
 * confident, wrong cost next to somebody's order ticket, which is worse than the
 * extension appearing not to work.
 */

import { type BookLevel, type ContractTerms, type Venue } from "./cost";
import { type Dec, ONE, dec, gt, quantizeUsd, sub } from "./decimal";

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

export interface Contract {
  venue: Venue;
  /** The venue's own identifier: a Kalshi ticker or a Polymarket token id. */
  id: string;
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
export function kalshiCandidates(url: string, bodyText: string): string[] {
  const seen = new Set<string>();
  const push = (raw: string) => {
    const ticker = raw.toUpperCase();
    if (!seen.has(ticker)) seen.add(ticker);
  };

  for (const match of decodeURIComponent(url).matchAll(KALSHI_TICKER)) push(match[0]);
  for (const match of bodyText.matchAll(KALSHI_TICKER)) push(match[0]);
  // Bounded: each candidate costs a request, and a page that yields dozens is a
  // page this extension has not understood.
  return [...seen].slice(0, 8);
}

interface KalshiMarket {
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

  const multiplier =
    market.fee_multiplier === undefined || market.fee_multiplier === null
      ? dec("1")
      : dec(String(market.fee_multiplier));
  const resolution = market.expected_expiration_time ?? market.close_time;

  return {
    venue: "kalshi",
    id: market.ticker,
    title: [market.title, market.yes_sub_title].filter(Boolean).join(" — "),
    terms: {
      venue: "kalshi",
      feeRate: multiplyRate(KALSHI_BASE_RATE, multiplier),
      feeType: market.fee_type ?? "",
      tickSize: dec("0.01"),
      minOrderSize: dec(String(market.minimum_order_size ?? 1)),
      yearsToResolution: yearsUntil(resolution),
    },
  };
}

/**
 * Kalshi's book is bids-only, so YES asks are derived: a resting NO bid at X is a
 * YES ask at $1 − X, carrying the same size. Mirrors `KalshiProvider.parse_orderbook`.
 */
export async function kalshiBook(
  ticker: string,
  fetchJson: FetchJson,
): Promise<LiveBook | null> {
  const payload = await fetchJson(
    `${KALSHI_API}/markets/${encodeURIComponent(ticker)}/orderbook?depth=100`,
  );
  const book =
    (payload as Record<string, any> | null)?.orderbook_fp ??
    (payload as Record<string, any> | null)?.orderbook;
  if (!book) return null;

  const noBids: Array<[string, string]> = book.no_dollars ?? book.no ?? [];
  const levels: BookLevel[] = [];
  for (const entry of noBids) {
    if (!Array.isArray(entry) || entry.length < 2) continue;
    const askPrice = quantizeUsd(sub(ONE, dec(String(entry[0]))));
    if (!gt(askPrice, dec("0"))) continue;
    levels.push({ price: askPrice, size: dec(String(entry[1])) });
  }
  if (levels.length === 0) return null;
  return { levels, observedAt: Date.now() };
}

/* ------------------------------------------------------------- polymarket -- */

/** `polymarket.com/event/<event-slug>/<market-slug>` — the market slug is last. */
export function polymarketSlug(url: string): string | null {
  const match = /polymarket\.com\/(?:event|market)\/([^/?#]+)(?:\/([^/?#]+))?/.exec(url);
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
  let yesToken: string | null = null;
  try {
    const ids = JSON.parse(market.clobTokenIds) as string[];
    yesToken = ids[0] ?? null;
  } catch {
    yesToken = null;
  }
  if (!yesToken) return null;

  return {
    venue: "polymarket",
    id: yesToken,
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

function multiplyRate(base: Dec, multiplier: Dec): Dec {
  return { units: base.units * multiplier.units, scale: base.scale + multiplier.scale };
}

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

/** Identify the contract on the page, or return null and render nothing. */
export async function identify(
  url: string,
  bodyText: string,
  fetchJson: FetchJson,
): Promise<Contract | null> {
  if (/(^|\.)polymarket\.com/.test(new URL(url).hostname)) {
    const slug = polymarketSlug(url);
    return slug ? resolvePolymarket(slug, fetchJson) : null;
  }
  for (const ticker of kalshiCandidates(url, bodyText)) {
    const contract = await resolveKalshi(ticker, fetchJson);
    if (contract) return contract;
  }
  return null;
}

export async function loadBook(
  contract: Contract,
  fetchJson: FetchJson,
): Promise<LiveBook | null> {
  return contract.venue === "kalshi"
    ? kalshiBook(contract.id, fetchJson)
    : polymarketBook(contract.id, fetchJson);
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
