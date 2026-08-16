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
import {
  contractsForDollars,
  orderPanelText,
  readOrderInput,
  readSelectedSide,
  watchOrderForm,
} from "./order-form";
import { Latest } from "./latest";
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
 * Which side the order ticket is set to.
 *
 * The toggle's own `aria-pressed` state first, then an explicit URL parameter,
 * then NOTHING: it fails closed. An earlier version read the URL alone and
 * defaulted to YES, so a trader on NO was shown YES numbers on every page. The
 * null result must be respected by every caller - pricing the opposite contract
 * from a guess is the worst output this extension can produce.
 */
export function detectSide(url: string): Side | null {
  const fromDom = readSelectedSide();
  if (fromDom) return fromDom;
  if (/[?&](side|outcome)=no\b/i.test(url)) return "no";
  if (/[?&](side|outcome)=yes\b/i.test(url)) return "yes";
  return null;
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

/**
 * Monotonic generation for reload(). Reloads overlap - the timer, the URL
 * watcher and the side-toggle watcher can all fire while an earlier reload is
 * still awaiting the venue - and without a guard the slowest response wins and
 * paints a stale contract beside a fresh order ticket. Every mutation after an
 * await is therefore dropped unless this reload is still the newest one.
 */
const latest = new Latest();

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
  // A hidden tab computes no layout: every `getBoundingClientRect` returns zero
  // and `innerText` comes back empty, so the order panel cannot be found and the
  // contract cannot be identified. Left alone, the refresh timer would then treat
  // a backgrounded tab as "I do not know what this page is" and delete the panel,
  // so switching away and back showed an empty corner until the next refresh.
  //
  // Nothing needs recomputing while nobody is looking. `visibilitychange` picks
  // it up again.
  if (document.hidden) return;

  const generation = latest.begin();
  const url = location.href;
  const side = detectSide(url);
  if (side === null) {
    // Fail closed: without a confident side there are no side-specific numbers
    // to show. A stale YES/NO panel must not survive, but silence is also
    // wrong — the trader needs to see that the side could not be read.
    loaded = null;
    panel().innerHTML = messageHtml(
      "Cannot determine whether this ticket is YES or NO. No side-specific cost is shown.",
    );
    return;
  }

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
  if (!latest.isCurrent(generation)) return; // a newer reload owns the panel now

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
    if (!latest.isCurrent(generation)) return; // a newer reload owns the panel now
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
    if (!latest.isCurrent(generation)) return;
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

/**
 * Switching YES/NO changes which contract is being bought, so it needs a new
 * book rather than a redraw.
 *
 * The toggle is a button, not an input, so the order-form watcher never sees it
 * and the panel would have kept showing the other side's costs until the next
 * twenty-second refresh. Compared against the side actually loaded rather than
 * fired on every click, so clicking around the ticket costs nothing.
 */
function watchSideToggle(): void {
  document.addEventListener(
    "click",
    () => {
      // After the venue's own handler has run and updated the toggle.
      window.setTimeout(() => {
        const detected = detectSide(location.href);
        if (loaded && detected !== null && detected !== loaded.side) void reload();
      }, 120);
    },
    { capture: true, passive: true },
  );
}

/** Recompute as soon as the tab is looked at again. */
function watchVisibility(): void {
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void reload();
  });
}

if (document.body) {
  void reload();
  refreshTimer = window.setInterval(reload, REFRESH_MS);
  watchSideToggle();
  watchVisibility();
  watchNavigation();
  // Typing a new size redraws from the book already in hand. Deliberately not a
  // refetch: a trader adjusting a size field would otherwise fire a request per
  // keystroke at an endpoint neither venue promises to keep fast.
  watchOrderForm(draw);
}

export { draw, reload, refreshTimer };
