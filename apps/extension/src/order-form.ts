/**
 * Reading the size the trader has actually typed, out of the venue's order form.
 *
 * This is the difference between the extension and the website. A fixed ladder
 * teaches a general lesson once — that small orders pay more — and a reader who
 * has learned it does not need to be told again. A figure for *this* order is a
 * different thing: it is arithmetic on a number that changes every time, which is
 * why the value does not wear off.
 *
 * ## Why it is written so defensively
 *
 * Neither venue's DOM could be observed from the development environment
 * (kalshi.com answers 429 to automated access), so every assumption here is a
 * guess about someone else's markup. A wrong guess that reads the *limit price*
 * field and prices "37 contracts" would be a confident, wrong number beside a
 * live order form.
 *
 * So the reader is conservative in four ways, and returns null rather than
 * stretching any of them:
 *
 * - only `<input>` elements, only numeric ones;
 * - only whole numbers in a plausible contract range;
 * - never a field that looks like a price, a limit or a dollar amount, by name,
 *   placeholder, label or adjacent text;
 * - and when several candidates survive, none of them, because picking between
 *   two plausible fields is exactly the guess this must not make.
 */

/** Contract counts outside this range are almost certainly not contract counts. */
const MIN_PLAUSIBLE = 1;
const MAX_PLAUSIBLE = 1_000_000;

/**
 * Words that mark a field as something other than a quantity.
 *
 * A price field holding "37" is indistinguishable from a size field holding "37"
 * by value alone, so the name is the only signal available. Erring toward
 * rejection: a missed size field shows the ladder, a mistaken price field shows
 * a fabricated row.
 */
const NOT_A_SIZE = /price|limit|cost|usd|dollar|\$|cents?|odds|percent|%|avg|average/i;

/** Words that positively mark a field as a quantity. */
const LOOKS_LIKE_SIZE = /qty|quantity|amount|contracts?|shares?|size|count/i;

function describe(input: HTMLInputElement): string {
  const label =
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
    label,
  ]
    .join(" ")
    .trim();
}

function isVisible(input: HTMLInputElement): boolean {
  if (input.disabled || input.readOnly || input.type === "hidden") return false;
  const rect = input.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

/**
 * The order size on the page, or null when it cannot be told apart from
 * something else.
 */
export function readOrderSize(root: ParentNode = document): number | null {
  const inputs = Array.from(root.querySelectorAll("input")).filter((input) => {
    if (!isVisible(input)) return false;
    if (input.type !== "number" && input.type !== "text" && input.type !== "tel") {
      return false;
    }
    const raw = input.value.trim().replace(/,/g, "");
    if (raw === "" || !/^\d+$/.test(raw)) return false;
    const value = Number(raw);
    if (!Number.isInteger(value) || value < MIN_PLAUSIBLE || value > MAX_PLAUSIBLE) {
      return false;
    }
    return !NOT_A_SIZE.test(describe(input));
  });

  if (inputs.length === 0) return null;
  if (inputs.length === 1) return Number(inputs[0].value.trim().replace(/,/g, ""));

  // Several numeric fields. Only a positively-named one breaks the tie; anything
  // else is a coin flip between the trader's size and some other integer.
  const named = inputs.filter((input) => LOOKS_LIKE_SIZE.test(describe(input)));
  if (named.length === 1) return Number(named[0].value.trim().replace(/,/g, ""));
  return null;
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
