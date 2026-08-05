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

/**
 * Contract price in cents, keeping sub-tenth-of-a-cent detail.
 *
 * `cents` rounds to one decimal, which is right for a price column and wrong for
 * a cost decomposition: a venue fee of $0.0001 is 0.01¢ and renders as "0.0¢",
 * so the table breaking a premium into its parts showed several components as
 * zero and did not sum to its own total. Trailing zeros are trimmed so the common
 * case still reads as "1.0¢" rather than "1.00¢".
 */
export function centsFine(value: string | number | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return "—";
  const scaled = parsed * 100;
  if (scaled !== 0 && Math.abs(scaled) < 0.1) return `${scaled.toFixed(3)}¢`;
  const fixed = scaled.toFixed(2);
  return `${fixed.endsWith("0") ? scaled.toFixed(1) : fixed}¢`;
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

/*
 * `relativeTime` and `ageLabel` lived here and measured from `Date.now()`. On a
 * deployment serving a frozen snapshot that is simply wrong: an age grew while
 * the reader sat on the page, and a market that had hours left when the data was
 * captured drifted into "resolved 12h ago".
 *
 * They are deleted rather than left unused, because an exported helper with the
 * right-sounding name is how the bug came back the last two times. Use
 * `ageRelativeToSnapshot` / `relativeToSnapshot` (both take an explicit anchor
 * and fall back to the live clock only when passed `null`), or `utcTime` for a
 * timestamp describing the dataset itself.
 */


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

/**
 * Human-readable labels for internal strategy keys.
 *
 * The table used to render the raw key (`top10_equal_10usd`), which is a database
 * identifier, not something a reader should have to decode. The key is still shown
 * in the per-strategy detail panel so it stays traceable back to the run record.
 */
export const STRATEGY_LABELS: Record<string, string> = {
  top1_10usd: "Top 1 · $10",
  top3_equal_10usd: "Top 3 · $10 each",
  top10_equal_10usd: "Top 10 · $10 each",
  top10_equal_25usd: "Top 10 · $25 each",
  top10_fractional_kelly: "Top 10 · Fractional Kelly",
  high_confidence_only: "High confidence only",
  resolves_within_24h: "Resolves within 24h",
  kalshi_only: "Kalshi only",
  polymarket_only: "Polymarket only",
  cross_platform_combined: "Both venues combined",
};

export function strategyLabel(key: string): string {
  return STRATEGY_LABELS[key] ?? key.replace(/_/g, " ");
}

/**
 * One-line plain-English definitions for the quant metrics.
 *
 * Rendered next to a tappable "?" rather than a hover tooltip: hover does not exist
 * on touch devices, so a hover-only explanation is no explanation on mobile.
 */
export const METRIC_HELP: Record<string, string> = {
  brier:
    "Brier score measures probability accuracy — the average squared error between " +
    "forecast and outcome. Lower is better; 0.25 is what you get by always guessing 50%.",
  log_loss:
    "Log loss also measures probability accuracy but punishes confident mistakes far " +
    "more harshly than Brier does. Lower is better.",
  vs_market:
    "How much the model's Brier score beat the market's own implied probability. " +
    "Positive means the model added information; zero or negative means it did not, " +
    "regardless of whether the strategy made money.",
  profit_factor:
    "Gross winnings divided by gross losses. Above 1 means the winners outweigh the " +
    "losers; below 1 means the reverse.",
  sharpe_like:
    "Average profit per bet divided by its standard deviation — a rough " +
    "return-per-unit-of-risk figure. It is not an annualised Sharpe ratio.",
  max_drawdown:
    "The largest peak-to-trough fall in cumulative profit — the worst losing streak " +
    "you would have had to sit through.",
  roi: "Net profit divided by total money staked.",
  win_rate:
    "Share of settled recommendations that made money. On its own it says little: " +
    "a strategy can win often and still lose money if the losses are larger.",
  settled:
    "Recommendations that have reached their resolution date and been graded. Small " +
    "samples cannot distinguish skill from luck.",
  conservative_bound:
    "The pessimistic end of the model's probability range. A recommendation must be " +
    "profitable even at this bound, not just at the central estimate.",
  conservative_net_ev:
    "Expected profit per contract using the conservative probability bound and the " +
    "full all-in cost. Must be positive for a market to be recommended.",
  calibration:
    "Compares forecast probability against how often those forecasts actually came " +
    "true. A well-calibrated model's 70% predictions happen about 70% of the time.",
};

/** Thousands-separated integer for display. Renders a real 0 as "0", never a dash. */
export function count(value: string | number | null | undefined): string {
  const n = num(value);
  return n == null ? "—" : Math.round(n).toLocaleString();
}

/**
 * Strip venue markdown from a market title.
 *
 * Kalshi emphasises the subject with `**...**`, which rendered literally as
 * `Will the **high temp in Austin** be >103°`. The fix is deliberately NOT to render
 * the markdown: these strings come from a third party and go straight into the page,
 * so interpreting their formatting means interpreting whatever else they contain.
 * Removing the syntax is the smaller, safer operation - React keeps escaping the
 * result, and no `dangerouslySetInnerHTML` is involved anywhere.
 */
export function displayTitle(raw: string | null | undefined): string {
  if (!raw) return "";
  return raw
    .replace(/\*\*(.+?)\*\*/g, "$1") // bold
    .replace(/__(.+?)__/g, "$1") // bold, underscore form
    .replace(/(^|\s)\*(?!\s)(.+?)\*(?=\s|$|[.,!?])/g, "$1$2") // italics
    .replace(/`([^`]+)`/g, "$1") // inline code
    .replace(/\s{2,}/g, " ")
    .trim();
}

/**
 * Time relative to the snapshot instant, not to now.
 *
 * On a frozen snapshot, `now()` keeps advancing while the data does not, so a market
 * that had 3 hours left when the data was captured drifts into "resolved 12h ago"
 * while still sitting under "Today's opportunities". Anchoring to the capture time
 * keeps the page internally consistent, and passing `null` falls back to live
 * behaviour for a real-time deployment.
 */
export function relativeToSnapshot(
  target: string | null | undefined,
  snapshotAt: string | null | undefined,
): string {
  if (!target) return "—";
  const anchor = snapshotAt ? new Date(snapshotAt).getTime() : Date.now();
  const t = new Date(target).getTime();
  if (Number.isNaN(t) || Number.isNaN(anchor)) return "—";

  const diffMs = t - anchor;
  const abs = Math.abs(diffMs);
  const mins = Math.round(abs / 60000);
  const hours = Math.round(abs / 3600000);
  const days = Math.round(abs / 86400000);
  const size = mins < 60 ? `${mins}m` : hours < 48 ? `${hours}h` : `${days}d`;

  if (diffMs >= 0) return snapshotAt ? `in ${size} (as of snapshot)` : `in ${size}`;
  // Already past at capture time. On a snapshot this is a settled market, not a
  // stale row that leaked into today's list.
  return snapshotAt ? `settled ${size} before snapshot` : `${size} ago`;
}

/** Venue availability labels. Broker availability is never inferred. */
export const VENUE_AVAILABILITY_LABEL: Record<string, string> = {
  confirmed_available: "Confirmed",
  confirmed_unavailable: "Not listed",
  unverified: "Unverified",
  not_observed: "Not checked",
};

/**
 * How old a quote was **at the moment the data was captured**.
 *
 * `ageLabel` measures against the real clock. On a frozen snapshot that is wrong in a
 * way that gets worse every day: resolution times were anchored to the capture
 * instant while quote age kept counting up against now(), so one page showed a quote
 * "3d old" next to a market resolving "in 2h" — two different clocks, and a reader
 * has no way to know which one to trust.
 *
 * Passing `snapshotAt = null` restores live behaviour for a real-time deployment.
 */
export function ageRelativeToSnapshot(
  observedAt: string | null | undefined,
  snapshotAt: string | null | undefined,
): string {
  if (!observedAt) return "—";
  const observed = new Date(observedAt).getTime();
  const anchor = snapshotAt ? new Date(snapshotAt).getTime() : Date.now();
  if (Number.isNaN(observed) || Number.isNaN(anchor)) return "—";

  const diffMs = anchor - observed;
  // A quote captured after the anchor is a clock or ordering artefact, not a
  // negative age. Report it plainly rather than rendering "-3h old".
  if (diffMs < 0) return snapshotAt ? "after snapshot" : "just now";

  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "<1m";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(diffMs / 3600000);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(diffMs / 86400000)}d`;
}

/**
 * Render a duration so the number stays small enough to read at a glance.
 *
 * "Data freshness 3269m" is two and a quarter days, but a reader scanning a
 * metric row does not divide by 1440. Mirrors `humanize_seconds` in
 * pmvl_shared.timeutil so the API and the page describe an age the same way.
 */
export function humanizeSeconds(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.abs(seconds);
  if (s < 90) return `${Math.round(s)}s`;
  const minutes = s / 60;
  if (minutes < 90) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1).replace(/\.0$/, "")}h`;
  return `${(hours / 24).toFixed(1).replace(/\.0$/, "")}d`;
}

/**
 * An absolute instant in UTC, for timestamps that describe the dataset itself.
 *
 * `localTime` renders in the reader's zone, which is right for "when does this
 * market resolve" and wrong for provenance: two people comparing notes on the
 * same snapshot should read the same string. `ageLabel` is worse here — it
 * measures from `Date.now()`, so on a frozen snapshot it reported an age that
 * grew while the reader sat on the page.
 */
export function utcTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toLocaleString("en-US", {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })} UTC`;
}
