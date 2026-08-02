import { apiGet, qs, type Opportunity, type WatchlistItem } from "@/lib/api";

/**
 * The homepage's evidence.
 *
 * Every number on the marketing page comes from the same API the research pages
 * read, at request time, on the server. Nothing here is a literal: a hard-coded
 * "2,000+ markets" would be a claim the deployment could not back up the moment
 * ingestion changed, and this product's entire argument is that it does not make
 * claims it cannot back up.
 *
 * When the API cannot be reached the result is `available: false` and the page
 * renders a plain "figures unavailable" state. Substituting last-known or
 * plausible numbers would be exactly the failure mode the snapshot banner and
 * the demo banner exist to prevent.
 */

export interface ResearchProof {
  readonly available: boolean;
  /** Total markets ingested across both venues. */
  readonly markets: number | null;
  /** The single most recent quote observation. NOT a capture time for the set. */
  readonly freshestQuoteAt: string | null;
  readonly arbitrageScanAt: string | null;
  readonly jobsSucceeded: number | null;
  readonly jobsTotal: number | null;
  /** Opportunities that cleared every gate at the 7-day horizon. */
  readonly actionable: number | null;
  /** Scored but not actionable - the coverage gap, shown deliberately. */
  readonly watchlist: number | null;
  readonly servingMode: string | null;
  readonly snapshotMode: boolean;
  readonly tradingExecutionEnabled: boolean;
  readonly modelVersion: string | null;
  readonly commitSha: string | null;
}

const UNAVAILABLE: ResearchProof = {
  available: false,
  markets: null,
  freshestQuoteAt: null,
  arbitrageScanAt: null,
  jobsSucceeded: null,
  jobsTotal: null,
  actionable: null,
  watchlist: null,
  servingMode: null,
  snapshotMode: false,
  // Fail closed on the one claim that matters most: if the API cannot be asked
  // whether trading is disabled, the page must not assert that it is.
  tradingExecutionEnabled: false,
  modelVersion: null,
  commitSha: null,
};

interface SystemPayload {
  snapshot_mode?: boolean;
  runtime_mode?: string;
  model_version?: string;
  trading_execution_enabled?: boolean;
  row_counts?: Record<string, number>;
  freshest_quote_observed_at?: string | null;
  deployment?: { commit_sha?: string | null };
  jobs?: Array<{ job_name: string; status: string }>;
  pipeline?: { public_serving_mode?: string } | null;
  snapshot_timing?: {
    freshest_quote_observed_at?: string | null;
    arbitrage_scan_at?: string | null;
  } | null;
}

/** The horizon the homepage quotes. Named so the page can say which one. */
export const PROOF_HORIZON = "7d";

export async function getResearchProof(): Promise<ResearchProof> {
  const [system, opportunities, watchlist] = await Promise.all([
    apiGet<SystemPayload>("/system"),
    apiGet<Opportunity[]>(
      `/opportunities${qs({ horizon: PROOF_HORIZON, mode: "live", limit: 50 })}`,
    ),
    apiGet<WatchlistItem[]>(
      `/opportunities/watchlist${qs({ horizon: PROOF_HORIZON, mode: "live", limit: 50 })}`,
    ),
  ]);

  // `/system` is the one call the page cannot do without: it carries the
  // freshness, the serving mode and the trading-disabled fact that give every
  // other figure its context.
  if (!system?.data) return UNAVAILABLE;

  const data = system.data;
  const jobs = data.jobs ?? [];

  return {
    available: true,
    markets: data.row_counts?.markets ?? null,
    freshestQuoteAt:
      data.snapshot_timing?.freshest_quote_observed_at ??
      data.freshest_quote_observed_at ??
      null,
    arbitrageScanAt: data.snapshot_timing?.arbitrage_scan_at ?? null,
    jobsSucceeded: jobs.length > 0 ? jobs.filter((j) => j.status === "success").length : null,
    jobsTotal: jobs.length > 0 ? jobs.length : null,
    // A null here means the endpoint failed; a zero means it answered "none".
    // Those are different facts and the page renders them differently.
    actionable: opportunities?.data ? opportunities.data.length : null,
    watchlist: watchlist?.data ? watchlist.data.length : null,
    servingMode:
      data.pipeline?.public_serving_mode ??
      (data.snapshot_mode ? "Read-only snapshot" : (data.runtime_mode ?? null)),
    snapshotMode: Boolean(data.snapshot_mode),
    tradingExecutionEnabled: data.trading_execution_enabled === true,
    modelVersion: data.model_version ?? null,
    commitSha: data.deployment?.commit_sha ?? null,
  };
}
