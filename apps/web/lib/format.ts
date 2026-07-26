/**
 * Display formatting.
 *
 * Every helper takes the string form the API sends and returns a string for
 * rendering. Values are parsed to `number` only at the moment of formatting, so no
 * intermediate float ever feeds back into a calculation.
 */

export function num(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Contract price in cents, the unit both venues quote in. */
export function cents(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  return `${(parsed * 100).toFixed(1)}¢`;
}

export function usd(
  value: string | number | null | undefined,
  digits = 2,
): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  const sign = parsed < 0 ? "-" : "";
  return `${sign}$${Math.abs(parsed).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function compactUsd(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  const abs = Math.abs(parsed);
  if (abs >= 1_000_000) return `$${(parsed / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `$${(parsed / 1_000).toFixed(1)}k`;
  return `$${parsed.toFixed(0)}`;
}

export function pct(
  value: string | number | null | undefined,
  digits = 1,
): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  return `${(parsed * 100).toFixed(digits)}%`;
}

export function prob(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  return `${(parsed * 100).toFixed(1)}%`;
}

export function signed(value: string | number | null | undefined, digits = 4): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  const sign = parsed > 0 ? "+" : "";
  return `${sign}${parsed.toFixed(digits)}`;
}

export function signedCents(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  const sign = parsed > 0 ? "+" : "";
  return `${sign}${(parsed * 100).toFixed(2)}¢`;
}

export function compactNumber(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  const abs = Math.abs(parsed);
  if (abs >= 1_000_000) return `${(parsed / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(parsed / 1_000).toFixed(1)}k`;
  return parsed.toFixed(0);
}

/** UTC timestamp rendered in the viewer's local timezone. */
export function localTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function localDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
}

/** "in 6h 20m" / "3d ago" — the reader needs urgency, not a raw timestamp. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return "—";
  const deltaSeconds = (target - Date.now()) / 1000;
  const past = deltaSeconds < 0;
  const abs = Math.abs(deltaSeconds);

  let text: string;
  if (abs < 60) text = `${Math.round(abs)}s`;
  else if (abs < 3600) text = `${Math.round(abs / 60)}m`;
  else if (abs < 86400) {
    const hours = Math.floor(abs / 3600);
    const minutes = Math.round((abs % 3600) / 60);
    text = minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  } else text = `${Math.round(abs / 86400)}d`;

  return past ? `${text} ago` : `in ${text}`;
}

export function ageLabel(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "unknown";
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m old`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h old`;
  return `${Math.round(seconds / 86400)}d old`;
}

/** Risk flags are snake_case identifiers; render them readably. */
export function humanizeFlag(flag: string): string {
  return flag.replace(/_/g, " ");
}

export const RISK_FLAG_EXPLANATIONS: Record<string, string> = {
  no_independent_prior:
    "The fair probability is derived from this market's own price, so no edge can be demonstrated against it.",
  low_model_confidence: "The ensemble's aggregate confidence is low.",
  no_research_evidence: "No dated external evidence was gathered for this market.",
  stale_quote: "The order book snapshot is older than the freshness limit.",
  wide_spread: "The bid/ask spread is wide, so exiting the position would be costly.",
  thin_liquidity: "Resting depth is below the minimum considered actionable.",
  low_volume: "Little has traded in the last 24 hours; the quote may not be real.",
  extreme_price:
    "Priced in the deep tail, where model error is most easily mistaken for edge and ROI figures are exaggerated.",
  imminent_settlement: "Resolution is under an hour away.",
  settles_before_close: "Expected settlement precedes the market's stated close time.",
  uma_oracle_settlement:
    "Settles through Polymarket's UMA optimistic oracle, which has a dispute window.",
  not_accepting_orders: "The venue is not currently accepting orders on this market.",
  demo_data: "Synthetic demonstration data. Not a real market or a real result.",
};

export function platformLabel(platform: string | null | undefined): string {
  if (platform === "kalshi") return "Kalshi";
  if (platform === "polymarket") return "Polymarket";
  if (platform === "demo") return "Demo";
  return platform ?? "—";
}
