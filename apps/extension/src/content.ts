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
 * Cost per contract at the size in the order form when one can be read, plus a
 * few placeable sizes around it, the break-even that follows, and which size is
 * cheapest. No probability estimate, no edge, no recommendation: the retrodiction
 * over 22 segments found no place where this project's models beat the market's
 * own price, and the one segment that cleared significance did so in the wrong
 * direction.
 *
 * So the overlay says what the trade costs. It has no opinion on whether to make
 * it — except to point out when a different size costs less, which is arithmetic
 * rather than a forecast.
 */

import { type CostAtSize, DEFAULT_ASSUMPTIONS, costAtSize, costLadder } from "./cost";
import { dec, toNumber } from "./decimal";
import { contractsForDollars, readOrderInput, watchOrderForm } from "./order-form";
import { messageHtml, panelHtml } from "./panel";
import {
  type Contract,
  type LiveBook,
  type Side,
  identify,
  loadBook,
  termsFromBook,
} from "./venues";

const PANEL_ID = "pmvl-cost-overlay";
/** How often the book is re-read while the tab sits open on one contract. */
const REFRESH_MS = 20_000;

async function fetchJson(url: string): Promise<unknown> {
  const reply = await chrome.runtime.sendMessage({ type: "pmvl:fetch", url });
  if (!reply?.ok) throw new Error(reply?.error ?? "no reply from background");
  return reply.value;
}

/* ------------------------------------------------------------------- panel -- */

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

function removePanel(): void {
  document.getElementById(PANEL_ID)?.remove();
}

/* --------------------------------------------------------------------- run -- */

/**
 * Which side the page is showing.
 *
 * Read from the URL only. A DOM guess at the selected YES/NO toggle would be
 * wrong on a redesign and wrong silently, and the side is named in the panel
 * header either way, so a reader can always see which contract was priced.
 */
function detectSide(url: string): Side {
  return /[?&](side|outcome)=no\b/i.test(url) ? "no" : "yes";
}

/**
 * Text of the block containing the order form.
 *
 * Used to tell an event's two markets apart: Kalshi's URL names the *event*
 * (`kxmlbgame-26aug101907bostor`), which expands to both sides of the game, and
 * the order panel prints the outcome it is set to ("Boston"). Scoped to the
 * form's own container rather than the whole page, because both outcomes are
 * named elsewhere on the page and matching against all of it would be ambiguous
 * every time.
 */
function orderPanelText(): string {
  const input = Array.from(document.querySelectorAll("input")).find((node) => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && /^\$?0?$/.test(node.placeholder);
  });
  let node: HTMLElement | null = input?.parentElement ?? null;
  for (let hop = 0; hop < 6 && node?.parentElement; hop += 1) node = node.parentElement;
  return (node?.innerText ?? "").slice(0, 600);
}

/** The book and the contract behind whatever is currently on screen. */
interface Loaded {
  contract: Contract;
  book: LiveBook;
  side: Side;
  ladder: CostAtSize[];
}

let loaded: Loaded | null = null;
let refreshTimer: number | undefined;

function draw(): void {
  if (!loaded) return;
  const { contract, book, side, ladder } = loaded;
  const terms = termsFromBook(contract, book);

  // What the trader has typed, and in what unit. Both venues default to dollars,
  // so a bare integer is converted rather than taken as a contract count.
  const typed = readOrderInput();
  let contracts: number | null = null;
  let fromDollars: number | null = null;

  if (typed?.unit === "contracts") {
    contracts = typed.value;
  } else if (typed?.unit === "dollars") {
    // Converted at the top-of-book price, which is the figure the venue itself
    // uses to show what an amount buys. The resulting cost row is then priced
    // properly by walking the ladder, so only the *count* is an approximation.
    const top = ladder[0]?.nominalPrice;
    contracts = top ? contractsForDollars(typed.value, toNumber(top)) : null;
    fromDollars = contracts === null ? null : typed.value;
  }

  const yourRow =
    contracts === null
      ? null
      : costAtSize(book.levels, dec(String(contracts)), terms, DEFAULT_ASSUMPTIONS);

  panel().innerHTML = panelHtml({
    venue: contract.venue,
    side,
    ladder,
    observedAt: book.observedAt,
    yourRow,
    yourDollars: fromDollars,
  });
}

async function reload(): Promise<void> {
  const url = location.href;
  const side = detectSide(url);

  let contract: Contract | null = null;
  try {
    contract = await identify(
      url,
      // The page's markup, not its rendered text: on Kalshi the ticker appears
      // only in the HTML, so scanning innerText found nothing and the overlay
      // never appeared at all.
      document.documentElement?.innerHTML ?? "",
      fetchJson,
      orderPanelText(),
    );
  } catch {
    contract = null;
  }

  // No overlay at all when the contract cannot be confirmed against the venue's
  // own API. Silence is the correct output for "I do not know what you are
  // looking at"; a number would be a guess placed beside a live order form.
  if (!contract) {
    loaded = null;
    removePanel();
    return;
  }

  try {
    const book = await loadBook(contract, fetchJson, side);
    if (!book) {
      loaded = null;
      panel().innerHTML = messageHtml(
        `The venue returned no ${side.toUpperCase()} order book for this contract just now.`,
      );
      return;
    }
    const terms = termsFromBook(contract, book);
    loaded = {
      contract,
      book,
      side,
      ladder: costLadder(book.levels, terms, DEFAULT_ASSUMPTIONS),
    };
    draw();
  } catch (error) {
    loaded = null;
    panel().innerHTML = messageHtml(`Could not read the live book: ${String(error)}`);
  }
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
    void reload();
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

if (document.body) {
  void reload();
  refreshTimer = window.setInterval(reload, REFRESH_MS);
  watchNavigation();
  // Typing a new size redraws from the book already in hand. Deliberately not a
  // refetch: a trader adjusting a size field would otherwise fire a request per
  // keystroke at an endpoint neither venue promises to keep fast.
  watchOrderForm(draw);
}

export { draw, reload, refreshTimer };
