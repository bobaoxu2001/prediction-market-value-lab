/**
 * Typed client for the research API.
 *
 * Money and probabilities arrive as **strings**, deliberately: the backend carries
 * them as Decimals and sending them as JSON numbers would convert them to IEEE
 * doubles in the browser. Nothing here does arithmetic on those strings — they are
 * parsed only for display formatting and for chart geometry, where a rounding error
 * in the fifth decimal cannot change a conclusion.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export type DataMode = "live" | "demo" | "all";

export interface Envelope<T> {
  data: T;
  data_mode: DataMode;
  disclaimer: string;
  demo_notice?: string;
  [key: string]: unknown;
}

export interface Opportunity {
  id: number;
  rank: number;
  horizon: string;
  market_id: number;
  platform: string | null;
  platform_market_id: string | null;
  title: string;
  category: string | null;
  side: "yes" | "no";
  entry_price: string;
  current_price: string | null;
  total_cost_per_contract: string;
  executable_size: string;
  fair_probability: string;
  fair_probability_low: string;
  fair_probability_high: string;
  net_edge: string;
  conservative_net_ev: string;
  net_roi: string;
  expected_profit_10: string;
  expected_profit_50: string;
  expected_profit_100: string;
  expected_profit_per_100_usd: string;
  fractional_kelly: string;
  recommended_position_cap: string;
  composite_score: string;
  model_confidence: string;
  spread: string | null;
  liquidity_usd: string | null;
  expected_resolution_time: string | null;
  risk_flags: string[];
  cost_breakdown: Record<string, string>;
  model_version: string;
  state: string;
  created_at: string;
  evidence_updated_at: string | null;
  settlement_result: string | null;
  realized_profit_per_contract: string | null;
  provenance: string;
}

export interface WatchlistItem {
  market_id: number;
  platform: string;
  platform_market_id: string;
  title: string;
  category: string;
  best_yes_ask: string | null;
  best_no_ask: string | null;
  spread: string | null;
  liquidity_usd: string | null;
  volume_24h: string | null;
  expected_resolution_time: string | null;
  market_implied_probability: string | null;
  model_confidence: string;
  reason: string;
}

export interface ArbLeg {
  platform: string;
  platform_market_id: string;
  market_id: number | null;
  title: string;
  side: string;
  price: string;
  size_available: string;
  fee_per_contract: string;
}

export interface ArbitrageOpportunity {
  id: number;
  kind: string;
  label: string;
  label_meaning: string;
  title: string;
  legs: ArbLeg[];
  gross_edge_per_set: string;
  total_cost_per_set: string;
  net_profit_per_set: string;
  max_executable_sets: string;
  max_net_profit: string;
  capital_required: string;
  net_roi: string;
  rule_compatibility: string;
  risk_flags: string[];
  quote_age_seconds: number | null;
  expected_resolution_time: string | null;
  cost_breakdown: Record<string, unknown>;
  created_at: string;
  provenance: string;
}

export interface MarketRow {
  id: number;
  platform: string;
  platform_market_id: string;
  title: string;
  subtitle: string;
  category: string;
  status: string;
  accepting_orders: boolean;
  best_yes_bid: string | null;
  best_yes_ask: string | null;
  best_no_bid: string | null;
  best_no_ask: string | null;
  spread: string | null;
  orderbook_depth_usd: string | null;
  volume_24h: string | null;
  total_volume: string | null;
  open_interest: string | null;
  last_trade_price: string | null;
  tick_size: string;
  fee_rate: string;
  close_time: string | null;
  expected_resolution_time: string | null;
  horizon: string | null;
  quote_observed_at: string | null;
  result: string | null;
  provenance: string;
  venue_availability?: { venue: string; status: string; label: string }[];
}

export interface TrackRecordRow {
  id: number;
  snapshot_date: string;
  recommendation_created_at: string;
  market_id: number;
  platform: string;
  platform_market_id: string;
  market_title: string;
  horizon: string;
  rank: number;
  side: string;
  entry_price_at_recommendation: string;
  total_cost_at_recommendation: string;
  executable_size: string;
  fair_probability: string;
  confidence_interval: [string, string];
  expected_value: string;
  conservative_net_ev: string;
  model_confidence: string;
  model_version: string;
  expected_resolution_time: string | null;
  evidence_snapshot: Record<string, unknown>;
  orderbook_snapshot: Record<string, unknown>;
  risk_flags: string[];
  final_result: string | null;
  realized_profit_per_contract: string | null;
  realized_profit_at_100_usd: string | null;
  settled_at: string | null;
  provenance: string;
}

export interface Divergence {
  market_id: number;
  platform: string;
  platform_market_id: string;
  title: string;
  subtitle: string;
  category: string;
  market_implied_probability: string;
  model_probability: string;
  model_low: string;
  model_high: string;
  divergence: string;
  abs_divergence: string;
  direction: string;
  model_confidence: string;
  best_yes_ask: string | null;
  best_no_ask: string | null;
  spread: string | null;
  liquidity_usd: string | null;
  volume_24h: string | null;
  expected_resolution_time: string | null;
  explanation: string;
  model_version: string;
}

export interface CalibrationBin {
  bin_lower: number;
  bin_upper: number;
  n: number;
  mean_predicted: number;
  observed_frequency: number;
}

export interface BacktestMetrics {
  n_settled: number;
  wins?: number;
  win_rate?: number;
  avg_predicted_probability?: number;
  avg_market_probability?: number | null;
  total_stake?: string;
  total_pnl?: string;
  roi?: number | null;
  max_drawdown?: string;
  profit_factor?: number | null;
  sharpe_like_per_bet?: number | null;
  brier_score?: number | null;
  log_loss?: number | null;
  market_brier_score?: number | null;
  market_log_loss?: number | null;
  brier_improvement_vs_market?: number | null;
  calibration_curve?: CalibrationBin[];
  market_calibration_curve?: CalibrationBin[];
  note?: string;
}

export interface BacktestRun {
  run_id: string;
  strategy: string;
  description: string;
  model_version: string;
  window_start: string | null;
  window_end: string | null;
  walk_forward: boolean;
  data_quality: string;
  data_quality_meaning: string;
  n_recommendations: number;
  n_settled: number;
  metrics: BacktestMetrics;
  by_slice: Record<string, BacktestMetrics>;
  notes: string;
  created_at: string;
  provenance: string;
}

export interface JobStatusRow {
  job_name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  records_written: number;
  error: string;
  details: Record<string, unknown>;
}

export interface SystemInfo {
  environment: string;
  model_version: string;
  row_counts: Record<string, number>;
  provenance_split: Record<string, Record<string, number>>;
  jobs: JobStatusRow[];
  freshest_quote_observed_at: string | null;
  data_sources: Array<{
    name: string;
    base_url: string;
    auth_required: boolean;
    configured?: boolean;
    enabled?: boolean;
    used_for: string;
    docs: string;
  }>;
  update_frequencies: Record<string, string>;
  trading_execution_enabled: boolean;
}

/**
 * Fetch a JSON envelope.
 *
 * Never throws on a failed request: pages must render an explicit "data
 * unavailable" state rather than a crash, because an empty page is
 * indistinguishable from "there are no opportunities today" and that ambiguity is
 * exactly what this platform must not create.
 */
export async function apiGet<T>(
  path: string,
  init?: RequestInit,
): Promise<Envelope<T> | null> {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      // Opportunity and arbitrage data is time-sensitive; never serve it from cache.
      cache: "no-store",
      ...init,
    });
    if (!res.ok) return null;
    return (await res.json()) as Envelope<T>;
  } catch {
    return null;
  }
}

export function qs(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const out = search.toString();
  return out ? `?${out}` : "";
}

export interface FunnelStage {
  label: string;
  count: number;
  note: string;
}

export interface CostComponent {
  key: string;
  label: string;
  amount: string | null;
  applicable: boolean;
}

export interface CaseStudy {
  market: {
    snapshot_id: number;
    market_id: number;
    title: string;
    platform: string;
    platform_market_id: string;
    category: string | null;
    side: "yes" | "no";
    horizon: string;
    rank: number;
    published_at: string;
    expected_resolution_time: string | null;
    settled_at: string | null;
    final_result: string | null;
    settlement_rules: string;
    settlement_source: string;
    provenance: string;
  };
  execution: {
    best_ask: string | null;
    best_no_ask: string | null;
    entry_vwap: string;
    reference_size: string;
    spread: string | null;
    depth_usd: string | null;
    quote_observed_at: string | null;
    levels: Array<{ price: string; size: string }>;
  };
  probability: {
    market_implied: string | null;
    fair_probability_yes: string;
    win_probability_for_side: string;
    conservative_bound: string;
    conservative_bound_label: string;
    interval_low: string;
    interval_high: string;
    model_confidence: string;
    model_version: string;
    has_independent_prior: boolean | null;
    components: Array<Record<string, unknown>>;
    explanation: string;
    evidence_items: Array<Record<string, unknown>>;
  };
  costs: {
    components: CostComponent[];
    total_cost_per_contract: string;
    cost_above_entry: string;
  };
  decision: {
    raw_edge: string;
    net_edge: string;
    conservative_net_ev: string;
    net_roi: string | null;
    expected_profit_per_100_usd: string | null;
    position_cap: string | null;
    risk_flags: string[];
    state: string | null;
    qualified: boolean;
    verdict: string;
  };
  outcome: {
    settled: boolean;
    final_result: string | null;
    yes_payout: string | null;
    payout_for_side: string | null;
    realized_profit_per_contract: string | null;
    realized_profit_at_100_usd: string | null;
    trade_won: boolean | null;
    model_brier: string | null;
    market_brier: string | null;
    forecast_beat_market: boolean | null;
    summary: string | null;
  };
}
