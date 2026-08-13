/**
 * Exact decimal arithmetic on BigInt, mirroring the subset of Python's `Decimal`
 * that the cost stack uses.
 *
 * Not a convenience. The entire product is a claim about cents — that a contract
 * quoted at 1¢ costs 2¢ to buy one of — and IEEE doubles cannot represent 0.07 or
 * 0.01 exactly. `0.07 * 3` in JavaScript is 0.21000000000000002, which is below a
 * cent boundary that `ceil_cent` would otherwise have rounded up, so a float port
 * of this file would disagree with the Python at exactly the boundaries the
 * product exists to describe.
 *
 * A value is `{ units, scale }` meaning `units × 10⁻ˢᶜᵃˡᵉ`. Addition, subtraction
 * and multiplication are **exact**: mantissas are integers, so their products are
 * integers and the scale is just bookkeeping. Only division and the explicit
 * quantise steps round, and each names the mode it uses.
 *
 * The Python side runs at `prec = 28, ROUND_HALF_EVEN`, and division here matches
 * both. Whether that is sufficient is not asserted here — it is verified case by
 * case against the real Python implementation in the conformance suite.
 */

export interface Dec {
  readonly units: bigint;
  readonly scale: number;
}

/**
 * Division precision, in **significant digits**, matching Python's `prec = 28`.
 *
 * Significant digits and not decimal places, which is a distinction that cost a
 * failing conformance run to notice. A fixed 30 decimal places looks like more
 * precision and is less: a per-contract rounding component of 8.33e-6 gets 30
 * digits after the point but only 25 significant ones, so it disagreed with
 * Python from the 26th digit on. Fees are small numbers, so this is the case that
 * matters rather than an edge case.
 */
const DIVISION_SIGNIFICANT_DIGITS = 28;

export function dec(value: string | number | bigint): Dec {
  if (typeof value === "bigint") return { units: value, scale: 0 };
  const text = typeof value === "number" ? numberToDecimalString(value) : value.trim();
  if (!/^[+-]?\d*\.?\d+(?:[eE][+-]?\d+)?$/.test(text)) {
    throw new Error(`not a decimal literal: ${JSON.stringify(text)}`);
  }
  const [mantissa, exponentText] = text.split(/[eE]/);
  const exponent = exponentText ? Number(exponentText) : 0;
  const negative = mantissa.startsWith("-");
  const digits = mantissa.replace(/^[+-]/, "");
  const [whole, fraction = ""] = digits.split(".");
  const scale = fraction.length - exponent;
  const units = BigInt(`${whole}${fraction}` || "0") * (negative ? -1n : 1n);
  return scale < 0
    ? { units: units * pow10(-scale), scale: 0 }
    : { units, scale };
}

/**
 * A JS number reaches this only from venue payloads, which arrive as strings and
 * should stay strings. Going through the shortest round-trip representation keeps
 * `0.07` as `0.07` rather than its binary expansion, and anything that cannot be
 * written exactly is a bug in the caller rather than something to paper over.
 */
function numberToDecimalString(value: number): string {
  if (!Number.isFinite(value)) throw new Error(`not a finite number: ${value}`);
  return String(value);
}

const POW10: bigint[] = [1n];
function pow10(n: number): bigint {
  if (n < 0) throw new Error(`negative power: ${n}`);
  while (POW10.length <= n) POW10.push(POW10[POW10.length - 1] * 10n);
  return POW10[n];
}

export const ZERO = dec("0");
export const ONE = dec("1");

function align(a: Dec, b: Dec): [bigint, bigint, number] {
  const scale = Math.max(a.scale, b.scale);
  return [
    a.units * pow10(scale - a.scale),
    b.units * pow10(scale - b.scale),
    scale,
  ];
}

export function add(a: Dec, b: Dec): Dec {
  const [x, y, scale] = align(a, b);
  return { units: x + y, scale };
}

export function sub(a: Dec, b: Dec): Dec {
  const [x, y, scale] = align(a, b);
  return { units: x - y, scale };
}

/** Exact: integer mantissas multiply, scales add. Nothing is discarded. */
export function mul(a: Dec, b: Dec): Dec {
  return { units: a.units * b.units, scale: a.scale + b.scale };
}

export function cmp(a: Dec, b: Dec): number {
  const [x, y] = align(a, b);
  return x < y ? -1 : x > y ? 1 : 0;
}

export const lt = (a: Dec, b: Dec) => cmp(a, b) < 0;
export const lte = (a: Dec, b: Dec) => cmp(a, b) <= 0;
export const gt = (a: Dec, b: Dec) => cmp(a, b) > 0;
export const gte = (a: Dec, b: Dec) => cmp(a, b) >= 0;
export const isZero = (a: Dec) => a.units === 0n;
export const isNegative = (a: Dec) => a.units < 0n;
export const max = (a: Dec, b: Dec) => (gte(a, b) ? a : b);
export const min = (a: Dec, b: Dec) => (lte(a, b) ? a : b);

/** Mirrors `pmvl_shared.money.safe_div`: zero denominator yields the default. */
export function safeDiv(a: Dec, b: Dec, fallback: Dec = ZERO): Dec {
  if (isZero(b)) return fallback;
  if (isZero(a)) return ZERO;

  // The exact rational a/b, as a ratio of integers.
  const numerator = a.units * pow10(b.scale);
  const denominator = b.units * pow10(a.scale);

  // Where the leading significant digit sits, so the result can be rounded to a
  // digit count rather than to a fixed number of places.
  const exponent = leadingExponent(absBig(numerator), absBig(denominator));
  const scale = DIVISION_SIGNIFICANT_DIGITS - 1 - exponent;
  if (scale <= 0) {
    // A value large enough that 28 significant digits reach past the decimal
    // point. Nothing in this cost stack gets there, but truncating silently
    // would be the wrong way to find out.
    return { units: divideRoundHalfEven(numerator, denominator), scale: 0 };
  }
  return {
    units: divideRoundHalfEven(numerator * pow10(scale), denominator),
    scale,
  };
}

const absBig = (value: bigint): bigint => (value < 0n ? -value : value);

/** `floor(log10(n / d))` for positive integers, without touching a float. */
function leadingExponent(n: bigint, d: bigint): number {
  // The digit-count difference is within one of the answer; walk it in from there.
  let exponent = n.toString().length - d.toString().length;
  while (!atLeastPow10(n, d, exponent)) exponent -= 1;
  while (atLeastPow10(n, d, exponent + 1)) exponent += 1;
  return exponent;
}

/**
 * Whether `n / d >= 10 ** exponent`, by cross-multiplication.
 *
 * Negative exponents scale the numerator instead of dividing, which keeps every
 * comparison in exact integers. Fees are small, so the negative branch is the
 * common one rather than an afterthought.
 */
function atLeastPow10(n: bigint, d: bigint, exponent: number): boolean {
  return exponent >= 0 ? n >= d * pow10(exponent) : n * pow10(-exponent) >= d;
}

function divideRoundHalfEven(numerator: bigint, denominator: bigint): bigint {
  const negative = numerator < 0n !== denominator < 0n;
  const n = numerator < 0n ? -numerator : numerator;
  const d = denominator < 0n ? -denominator : denominator;
  const quotient = n / d;
  const twice = (n % d) * 2n;
  let rounded = quotient;
  if (twice > d || (twice === d && quotient % 2n === 1n)) rounded = quotient + 1n;
  return negative ? -rounded : rounded;
}

function rescale(value: Dec, scale: number, mode: "half-up" | "ceiling"): Dec {
  if (value.scale <= scale) {
    return { units: value.units * pow10(scale - value.scale), scale };
  }
  const factor = pow10(value.scale - scale);
  const negative = value.units < 0n;
  const magnitude = negative ? -value.units : value.units;
  const quotient = magnitude / factor;
  const remainder = magnitude % factor;

  let units: bigint;
  if (mode === "half-up") {
    // Python's ROUND_HALF_UP is away from zero at the midpoint, unlike the
    // banker's rounding used for division.
    units = remainder * 2n >= factor ? quotient + 1n : quotient;
    units = negative ? -units : units;
  } else {
    // ROUND_CEILING is toward positive infinity, so a negative value truncates.
    units = negative ? -quotient : remainder > 0n ? quotient + 1n : quotient;
  }
  return { units, scale };
}

/** `ceil_cent`: up to the next whole cent. Used for Kalshi's order-level fee. */
export const ceilCent = (value: Dec): Dec => rescale(value, 2, "ceiling");

/** `quantize_usd` / `quantize_price`: centicent resolution, ROUND_HALF_UP. */
export const quantizeUsd = (value: Dec): Dec => rescale(value, 4, "half-up");

/** Polymarket's published fee precision: 5 decimal places. */
export const quantizePolyFee = (value: Dec): Dec => rescale(value, 5, "half-up");

export function toString(value: Dec): string {
  const negative = value.units < 0n;
  const digits = (negative ? -value.units : value.units).toString();
  if (value.scale === 0) return `${negative ? "-" : ""}${digits}`;
  const padded = digits.padStart(value.scale + 1, "0");
  const whole = padded.slice(0, padded.length - value.scale);
  const fraction = padded.slice(padded.length - value.scale);
  return `${negative ? "-" : ""}${whole}.${fraction}`;
}

/** For display only. Never feed the result back into the cost maths. */
export function toNumber(value: Dec): number {
  return Number(toString(value));
}
