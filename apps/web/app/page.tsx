import Link from "next/link";
import {
  apiGet,
  qs,
  type DataMode,
  type Opportunity,
  type WatchlistItem,
} from "@/lib/api";
import type { Divergence, FunnelStage } from "@/lib/api";
import { ageLabel, ageRelativeToSnapshot, cents, compactUsd, displayTitle, localTime, pct, prob, relativeTime, relativeToSnapshot, signedCents, usd } from "@/lib/format";
import {
  ApiDown,
  DemoBanner,
  EmptyState,
  Metric,
  PageHeader,
  PlatformChip,
  RiskFlags,
  SideChip,
  StateChip,
  ValueTone,
} from "@/components/ui";
import { Hero } from "@/components/hero";

export const dynamic = "force-dynamic";

const HORIZONS = [
  { key: "24h", label: "Resolves within 24h" },
  { key: "7d", label: "Resolves within 7d" },
  { key: "30d", label: "Resolves within 30d" },
] as const;

type Search = {
  horizon?: string;
  mode?: DataMode;
  platform?: string;
  side?: string;
  min_edge?: string;
  min_confidence?: string;
  min_liquidity?: string;
  include_inactive?: string;
};

export default async function TodayPage({
  searchParams,
}: {
  searchParams: Promise<Search>;
}) {
  const params = await searchParams;
  const horizon = HORIZONS.some((h) => h.key === params.horizon)
    ? params.horizon!
    : "24h";
  const mode: DataMode = params.mode === "demo" ? "demo" : "live";

  const query = qs({
    horizon,
    mode,
    platform: params.platform,
    side: params.side,
    min_edge: params.min_edge,
    min_confidence: params.min_confidence,
    min_liquidity: params.min_liquidity,
    include_inactive: params.include_inactive === "1" ? true : undefined,
    limit: 10,
  });

  const [opps, summary, watch, diverge, funnel, system] = await Promise.all([
    apiGet<Opportunity[]>(`/opportunities${query}`),
    apiGet<Record<string, number>>(`/opportunities/summary${qs({ mode })}`),
    apiGet<WatchlistItem[]>(`/opportunities/watchlist${qs({ horizon, mode, limit: 12 })}`),
    apiGet<Divergence[]>(
      `/opportunities/disagreements${qs({ horizon, mode, limit: 10, min_divergence: "0.02" })}`,
    ),
    apiGet<FunnelStage[]>(`/opportunities/funnel${qs({ horizon, mode })}`),
    // Anchor relative times to the snapshot instant. On a frozen deployment now()
    // keeps advancing while the data does not, so a market with 3h left at capture
    // drifts into "resolved 12h ago" under a heading that says "today".
    apiGet<{ snapshot_mode?: boolean; freshest_quote_observed_at?: string | null }>(
      "/system",
    ),
  ]);
  const snapshotAt = system?.data?.snapshot_mode
    ? (system?.data?.freshest_quote_observed_at ?? null)
    : null;

  if (!opps) return <ApiDown />;

  const rows = opps.data ?? [];
  const counts = summary?.data ?? {};
  const generatedAt = opps.generated_at as string | undefined;

  return (
    <div>
      <Hero
        demoHref={`/${qs({ horizon, mode: "demo" })}`}
        backtestHref={`/backtest${qs({ mode: "demo" })}`}
        guidedHref={`/demo${qs({ step: 1, mode: "demo" })}`}
        caseStudyHref={`/case-study${qs({ mode: "demo" })}`}
      />

      <PageHeader
        title="Today's opportunities"
        subtitle="Ranked by conservative net expected value against the executable ask, after fees, slippage, transfer and capital costs. Only markets with a probability estimate independent of their own price can appear here."
        right={
          <div className="text-right text-xs text-neutral-500 dark:text-neutral-400">
            {generatedAt && <div>Generated {localTime(generatedAt)}</div>}
          </div>
        }
      />

      <DemoBanner notice={opps.demo_notice} />

      <div className="mb-4 flex flex-wrap gap-2">
        {HORIZONS.map((h) => {
          const active = h.key === horizon;
          return (
            <Link
              key={h.key}
              href={`/${qs({ horizon: h.key, mode })}`}
              className={`rounded-lg border px-3 py-2 text-sm transition ${
                active
                  ? "border-neutral-900 bg-neutral-900 text-white dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900"
                  : "border-neutral-200 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:bg-neutral-900"
              }`}
            >
              {h.label}
              <span className="ml-2 opacity-60">{counts[h.key] ?? 0}</span>
            </Link>
          );
        })}
      </div>

      {rows.length === 0 ? (
        funnel?.data?.length ? (
          <FilterFunnel
            stages={funnel.data}
            conclusion={(funnel.conclusion as string) ?? "No actionable opportunities right now."}
            mode={mode}
            horizon={horizon}
          />
        ) : (
          <EmptyState
            title="No actionable opportunities right now"
            body={(opps.empty_reason as string) ?? ""}
          />
        )
      ) : (
        <div className="space-y-3">
          {rows.map((o) => (
            <OpportunityCard key={o.id} o={o} snapshotAt={snapshotAt} />
          ))}
        </div>
      )}

      {diverge?.data?.length ? (
        <section className="mt-10">
          <h2 className="text-sm font-semibold">
            Where the model most disagrees with the market
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-neutral-600 dark:text-neutral-400">
            {(diverge.explanation as string) ??
              "These are not recommendations."}
          </p>
          <div className="card table-wrap mt-3">
            <table className="w-full">
              <thead className="border-b border-neutral-200 dark:border-neutral-800">
                <tr>
                  <th>Market</th><th>Venue</th><th>Market</th><th>Model</th>
                  <th>Interval</th><th>Divergence</th><th>Confidence</th><th>Resolves</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                {diverge.data.map((d) => {
                  const div = Number(d.divergence);
                  return (
                    <tr key={d.market_id}>
                      <td className="max-w-sm truncate">
                        <Link href={`/market/${d.market_id}`} className="hover:underline">
                          {displayTitle(d.title)}
                        </Link>
                      </td>
                      <td><PlatformChip platform={d.platform} /></td>
                      <td className="num">{prob(d.market_implied_probability)}</td>
                      <td className="num font-semibold">{prob(d.model_probability)}</td>
                      <td className="num text-neutral-500">
                        {prob(d.model_low)}–{prob(d.model_high)}
                      </td>
                      <td className={`num font-semibold ${div > 0 ? "text-edge dark:text-edge-dark" : "text-risk dark:text-risk-dark"}`}>
                        {div > 0 ? "+" : ""}{(div * 100).toFixed(1)}pp
                      </td>
                      <td className="num">{pct(d.model_confidence)}</td>
                      <td className="text-neutral-500">
                        {relativeToSnapshot(d.expected_resolution_time, snapshotAt)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {watch?.data?.length ? (
        <section className="mt-10">
          <h2 className="text-sm font-semibold">
            Watchlist — not actionable yet
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-neutral-600 dark:text-neutral-400">
            {(watch.explanation as string) ??
              "These markets were scored, but the only information available was the market's own price."}{" "}
            They are shown so the coverage gap is visible. They are{" "}
            <strong>not</strong> opportunities.
          </p>
          <div className="card table-wrap mt-3">
            <table className="w-full">
              <thead className="border-b border-neutral-200 dark:border-neutral-800">
                <tr>
                  <th>Market</th>
                  <th>Venue</th>
                  <th>YES ask</th>
                  <th>Spread</th>
                  <th>Depth</th>
                  <th>Resolves</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                {watch.data.map((w) => (
                  <tr key={w.market_id}>
                    <td className="max-w-md truncate">
                      <Link href={`/market/${w.market_id}`} className="hover:underline">
                        {displayTitle(w.title)}
                      </Link>
                    </td>
                    <td><PlatformChip platform={w.platform} /></td>
                    <td className="num">{cents(w.best_yes_ask)}</td>
                    <td className="num">{cents(w.spread)}</td>
                    <td className="num">{compactUsd(w.liquidity_usd)}</td>
                    <td className="text-neutral-500">
                      {relativeToSnapshot(w.expected_resolution_time, snapshotAt)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">
        {opps.disclaimer}
      </p>
    </div>
  );
}


function OpportunityCard({
  o,
  snapshotAt,
}: {
  o: Opportunity;
  snapshotAt: string | null;
}) {
  return (
    <article className="card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="num shrink-0 rounded bg-neutral-100 px-2 py-1 text-sm font-bold dark:bg-neutral-800">
            #{o.rank}
          </div>
          <div className="min-w-0">
            <Link
              href={`/market/${o.market_id}`}
              className="block truncate font-medium hover:underline"
            >
              {displayTitle(o.title)}
            </Link>
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <PlatformChip platform={o.platform} />
              <SideChip side={o.side} />
              <StateChip state={o.state} />
              {o.category && (
                <span className="chip bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400">
                  {o.category}
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="text-right">
          <div className="metric-label">Conservative net EV</div>
          <ValueTone value={o.conservative_net_ev}>
            <span className="text-lg font-bold">
              {signedCents(o.conservative_net_ev)}
            </span>
          </ValueTone>
          <div className="text-[11px] text-neutral-500">per contract</div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4 lg:grid-cols-6">
        <Metric
          label="Executable price"
          value={cents(o.entry_price)}
          hint="VWAP of the ask ladder at reference size — not a last trade or midpoint"
        />
        <Metric
          label="All-in cost"
          value={cents(o.total_cost_per_contract)}
          hint="Entry price plus fees, rounding, slippage, transfer and capital cost"
        />
        <Metric
          label="Fair probability"
          value={prob(o.fair_probability)}
          hint="Ensemble mean"
        />
        <Metric
          label="Conservative bound"
          value={prob(o.fair_probability_low)}
          hint="Lower bound of the uncertainty band — this is what the ranking uses"
        />
        <Metric
          label="Net edge"
          value={signedCents(o.net_edge)}
          tone={Number(o.net_edge) > 0 ? "good" : "bad"}
        />
        <Metric
          label="Net ROI"
          value={pct(o.net_roi)}
          tone={Number(o.net_roi) > 0 ? "good" : "bad"}
        />
        <Metric label="Profit on $100" value={usd(o.expected_profit_per_100_usd)} />
        <Metric
          label="Max capacity"
          value={`${Number(o.recommended_position_cap).toFixed(0)} ct`}
          hint="Largest size whose marginal contract still has positive EV"
        />
        <Metric label="Spread" value={cents(o.spread)} />
        <Metric label="Liquidity" value={compactUsd(o.liquidity_usd)} />
        <Metric
          label="Confidence"
          value={pct(o.model_confidence)}
          tone={Number(o.model_confidence) < 0.3 ? "warn" : "neutral"}
        />
        <Metric
          label="Resolves"
          value={relativeToSnapshot(o.expected_resolution_time, snapshotAt)}
          hint={localTime(o.expected_resolution_time)}
        />
      </div>

      <div className="mt-3 grid gap-2 border-t border-neutral-100 pt-3 text-[11px] text-neutral-500 dark:border-neutral-800 dark:text-neutral-400 sm:grid-cols-3">
        <div>
          Sized: 10 ct {usd(o.expected_profit_10)} · 50 ct{" "}
          {usd(o.expected_profit_50)} · 100 ct {usd(o.expected_profit_100)}
        </div>
        <div>
          Kelly {pct(o.fractional_kelly)} · model {o.model_version}
        </div>
        <div className="sm:text-right">
          Recommended {localTime(o.created_at)} · evidence{" "}
          {o.evidence_updated_at ? ageRelativeToSnapshot(o.evidence_updated_at, snapshotAt) : "none"}
        </div>
      </div>

      {o.risk_flags?.length > 0 && (
        <div className="mt-3">
          <RiskFlags flags={o.risk_flags} />
        </div>
      )}
    </article>
  );
}

/**
 * The filtering funnel, shown instead of a paragraph when nothing qualifies.
 *
 * An empty list is the normal and correct outcome on efficiently-priced venues, but
 * explaining that in prose reads as an excuse. The counts are the evidence: the
 * system looked at everything and declined on stated grounds. Being willing to
 * recommend nothing is the product, not a shortfall of it.
 */
function FilterFunnel({
  stages,
  conclusion,
  mode,
  horizon,
}: {
  stages: FunnelStage[];
  conclusion: string;
  mode: DataMode;
  horizon: string;
}) {
  const widest = Math.max(...stages.map((s) => s.count), 1);
  return (
    <div className="card p-5">
      <h2 className="text-sm font-semibold">How today&apos;s markets were filtered</h2>
      <ol className="mt-4 space-y-2">
        {stages.map((stage, i) => {
          const share = Math.max(2, (stage.count / widest) * 100);
          const last = i === stages.length - 1;
          return (
            <li key={stage.label}>
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className={last ? "font-semibold" : ""}>{stage.label}</span>
                <span className="num shrink-0 font-mono font-semibold">
                  {stage.count.toLocaleString()}
                </span>
              </div>
              <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-800">
                <div
                  className={`h-full rounded-full ${
                    last && stage.count === 0
                      ? "bg-neutral-400 dark:bg-neutral-600"
                      : "bg-neutral-900 dark:bg-neutral-100"
                  }`}
                  style={{ width: `${share}%` }}
                />
              </div>
              <div className="mt-0.5 text-xs text-neutral-500">{stage.note}</div>
            </li>
          );
        })}
      </ol>
      <p className="mt-5 border-t border-neutral-200 pt-4 text-sm font-medium dark:border-neutral-800">
        {conclusion}{" "}
        <span className="font-normal text-neutral-600 dark:text-neutral-400">
          A recommendation has to survive every stage above. Efficiently-priced
          markets routinely produce none, and reporting none is the correct result.
        </span>
      </p>
      {mode === "live" ? (
        <Link
          href={`/${qs({ horizon, mode: "demo" })}`}
          className="mt-3 inline-block text-sm underline"
        >
          See the demo dataset for how a populated list looks
        </Link>
      ) : null}
    </div>
  );
}
