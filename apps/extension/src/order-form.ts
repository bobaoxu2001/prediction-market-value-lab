/**
 * Reading what the trader has typed into the venue's order form.
 *
 * This is the difference between the extension and the website. A fixed ladder
 * teaches a general lesson once — that small orders pay more — and a reader who
 * has learned it does not need to be told again. A figure for *this* order is a
 * different thing: it is arithmetic on a number that changes every time, which is
 * why the value does not wear off.
 *
 * ## The unit is the whole problem
 *
 * The first version of this file looked for a contract count. Both venues were
 * then loaded and neither has one. On 10 August 2026:
 *
 * - **Kalshi** offers a `DOLLARS` / contracts switch and defaults to dollars. Its
 *   amount field has placeholder `0`, a generated React id, no name and no
 *   aria-label. Nothing about the element says what its number means.
 * - **Polymarket** is dollars only, with placeholder `$0` and id
 *   `market-order-amount-input`.
 *
 * So a bare integer in an order form is *more likely to be dollars than
 * contracts*, and the old reader would have taken Kalshi's `50` — fifty dollars —
 * and priced fifty contracts. On a 5¢ contract that is a tenfold error, printed
 * confidently beside a live order ticket. It only avoided doing so on Polymarket
 * by luck, because `$` happened to appear in the placeholder.
 *
 * The reader therefore returns a **value and a unit**, and returns nothing at all
 * when the unit cannot be established from the text around the field. Refusing is
 * cheap — the panel falls back to the ladder — and guessing is not.
 */

/** What the number in the order form means. */
export type OrderUnit = "contracts" | "dollars";

export interface OrderInput {
  value: number;
  unit: OrderUnit;
}

/** Values outside this range are almost certainly not an order. */
const MIN_PLAUSIBLE = 1;
const MAX_PLAUSIBLE = 1_000_000;

/** How far up the tree to look for text that names the unit. */
const CONTEXT_HOPS = 5;

/*
 * Anchored on a leading word boundary only, deliberately.
 *
 * Adjacent inline elements concatenate when read as text — a panel rendering
 * `<span>Dollars</span><span>Contracts</span>` yields `DollarsContracts` — and a
 * trailing `\b` then fails to match either word. Requiring the boundary at the
 * start alone survives that without matching arbitrary substrings.
 */
const SAYS_DOLLARS = /\bdollar|\busd\b|\$/i;
const SAYS_CONTRACTS = /\bcontract|\bshares?\b|\bqty\b|\bquantity\b/i;

/**
 * Text that marks a field as something other than an order amount entirely.
 *
 * A limit price and an order amount are both bare integers, and only the
 * surrounding words separate them.
 */
const NOT_AN_AMOUNT = /limit price|\bodds\b|percent|%|\bavg\b|average|\bsearch\b/i;

function describe(input: HTMLInputElement): string {
  const labels =
    input.labels && input.labels.length > 0
      ? Array.from(input.labels)
          .map((l) => l.textContent ?? "")
          .join(" ")
      : "";
  return [
    input.name,
    input.id,
    input.placeholder,
    input.getAttribute("aria-label") ?? "",
    input.getAttribute("data-testid") ?? "",
    labels,
  ].join(" ");
}

/** The text of the field's nearest few ancestors, where the unit label lives. */
function context(input: HTMLInputElement): string {
  let node: HTMLElement | null = input.parentElement;
  let text = "";
  for (let hop = 0; hop < CONTEXT_HOPS && node; hop += 1) {
    text = (node.innerText ?? node.textContent ?? "").replace(/\s+/g, " ");
    // Stop as soon as an ancestor says something about units; going further
    // eventually swallows the whole page and matches everything.
    if (SAYS_DOLLARS.test(text) || SAYS_CONTRACTS.test(text)) break;
    node = node.parentElement;
  }
  return text;
}

function isVisible(input: HTMLInputElement): boolean {
  if (input.disabled || input.readOnly || input.type === "hidden") return false;
  const rect = input.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function numericValue(input: HTMLInputElement): number | null {
  const raw = input.value.trim().replace(/[,$\s]/g, "");
  if (raw === "" || !/^\d+(?:\.\d+)?$/.test(raw)) return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < MIN_PLAUSIBLE || value > MAX_PLAUSIBLE) {
    return null;
  }
  return value;
}

/**
 * Establishes the unit, or gives up.
 *
 * Dollars is checked first and wins ties. Kalshi's panel prints both "Dollars"
 * and the contract's own name nearby, and its default is dollars — so on an
 * ambiguous panel, reading the number as dollars is the reading that matches
 * what the venue is actually doing.
 */
function unitOf(input: HTMLInputElement): OrderUnit | null {
  const own = describe(input);
  if (SAYS_DOLLARS.test(own)) return "dollars";
  if (SAYS_CONTRACTS.test(own)) return "contracts";

  const around = context(input);
  if (SAYS_DOLLARS.test(around)) return "dollars";
  if (SAYS_CONTRACTS.test(around)) return "contracts";
  return null;
}

/**
 * The order amount on the page and what it is denominated in, or null.
 *
 * Null whenever the page is ambiguous: no candidate, several candidates, or a
 * candidate whose unit cannot be read. The panel then shows its ladder, which is
 * always correct if less specific.
 */
export function readOrderInput(root: ParentNode = document): OrderInput | null {
  const candidates: OrderInput[] = [];

  for (const input of Array.from(root.querySelectorAll("input"))) {
    if (!isVisible(input)) continue;
    if (input.type !== "number" && input.type !== "text" && input.type !== "tel") {
      continue;
    }
    if (NOT_AN_AMOUNT.test(describe(input))) continue;

    const value = numericValue(input);
    if (value === null) continue;

    const unit = unitOf(input);
    if (unit === null) continue;

    candidates.push({ value, unit });
  }

  return candidates.length === 1 ? candidates[0] : null;
}

/**
 * Contracts a dollar amount buys, at a price.
 *
 * Whole contracts only: both venues fill in whole units at these sizes, and
 * "you are buying 83.3 contracts" is not a thing a trader can act on. Floored
 * rather than rounded, because rounding up describes an order that costs more
 * than the amount typed.
 */
export function contractsForDollars(dollars: number, pricePerContract: number): number | null {
  if (!(dollars > 0) || !(pricePerContract > 0)) return null;
  const contracts = Math.floor(dollars / pricePerContract);
  return contracts >= 1 ? contracts : null;
}

/** Calls back when any input on the page changes, debounced. */
export function watchOrderForm(onChange: () => void, delayMs = 300): () => void {
  let timer: number | undefined;
  const handler = (event: Event) => {
    if (!(event.target instanceof HTMLInputElement)) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(onChange, delayMs);
  };
  document.addEventListener("input", handler, { capture: true, passive: true });
  document.addEventListener("change", handler, { capture: true, passive: true });
  return () => {
    window.clearTimeout(timer);
    document.removeEventListener("input", handler, { capture: true });
    document.removeEventListener("change", handler, { capture: true });
  };
}

/* ------------------------------------------------------- the order panel -- */

/** A YES/NO control that publishes whether it is the selected one. */
const SIDE_TOGGLE_SELECTOR =
  "[aria-pressed],[aria-selected],[aria-checked],[data-state]";

function isSideToggle(node: Element): boolean {
  return /^(yes|no)\b/i.test((node.textContent ?? "").trim());
}

/**
 * The block of the page containing the order form.
 *
 * Found from the amount field outward rather than by any class name, because
 * both venues ship generated class names that change between deploys.
 *
 * Climbing stops at the first ancestor that holds a side toggle as well as the
 * amount field — the smallest node that is recognisably the whole ticket. A
 * fixed number of hops was tried first and is wrong in both directions: six was
 * right for Kalshi's deep tree and swallowed the entire document on a shallow
 * one, which pulled in the market list below the ticket and its ten other
 * Yes/No buttons.
 *
 * The cap remains as a stop, so a page with no toggle at all yields a bounded
 * region rather than the body.
 */
export function orderPanelElement(root: Document = document): HTMLElement | null {
  const field = Array.from(root.querySelectorAll("input")).find((node) => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && /^\$?0?$/.test(node.placeholder);
  });
  if (!field) return null;

  let box: HTMLElement | null = field.parentElement;
  let widest: HTMLElement | null = box;
  for (let hop = 0; hop < 6 && box; hop += 1) {
    if (Array.from(box.querySelectorAll(SIDE_TOGGLE_SELECTOR)).some(isSideToggle)) {
      return box;
    }
    widest = box;
    box = box.parentElement;
  }
  return widest;
}

/** Text of the order panel, used to tell an event's two markets apart. */
export function orderPanelText(root: Document = document): string {
  return (orderPanelElement(root)?.innerText ?? "").slice(0, 600);
}

/** Attributes a toggle uses to say it is the selected one. */
const SELECTED_ATTRS = ["aria-pressed", "aria-selected", "aria-checked"];

/**
 * Which of YES / NO the order ticket is set to, read from the toggle's own
 * accessibility state.
 *
 * Kalshi's order panel marks its side buttons with `aria-pressed`, verified
 * live: YES reads `true` and NO reads `false`, and they swap on click. That is a
 * real state the venue publishes rather than a colour this code guesses at, so
 * it survives a restyle.
 *
 * Scoped to the order panel deliberately. The page below the ticket lists every
 * market in the event with its own Yes/No buttons — ten of them on a baseball
 * page — and none of those say anything about what the trader is about to buy.
 *
 * Returns null when the page does not say. Callers must fail closed — guessing
 * the side wrong prices the opposite contract.
 */
export function readSelectedSide(root: Document = document): "yes" | "no" | null {
  const box = orderPanelElement(root);
  if (!box) return null;

  const toggles = Array.from(box.querySelectorAll(SIDE_TOGGLE_SELECTOR)).filter(
    isSideToggle,
  );

  const selected = toggles.filter(
    (node) =>
      SELECTED_ATTRS.some((attr) => node.getAttribute(attr) === "true") ||
      node.getAttribute("data-state") === "active",
  );
  if (selected.length !== 1) return null;
  return /^no\b/i.test((selected[0].textContent ?? "").trim()) ? "no" : "yes";
}
