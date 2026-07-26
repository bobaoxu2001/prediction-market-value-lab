import Link from "next/link";
import {
  apiGet,
  qs,
  type DataMode,
  type Opportunity,
  type WatchlistItem,
} from "@/lib/api";
import {
  ageLabel,
  cents,
  compactUsd,
  localTime,
  pct,
  prob,
  relativeTime,
  signedCents,
  usd,
} from "@/lib/format";
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

  const [opps, summary, watch] = await Promise.all([
    apiGet<Opportunity[]>(`/opportunities${query}`),
    apiGet<Record<string, number>>(`/opportunities/summary${qs({ mode })}`),
    apiGet<WatchlistItem[]>(`/opportunities/watchlist${qs({ horizon, mode, limit: 12 })}`),
  ]);

  if (!opps) return <ApiDown />;

  const rows = opps.data ?? [];
  const counts = summary?.data ?? {};
  const generatedAt = opps.generated_at as string | undefined;

  return (
    <div>
      <PageHeader
        title="Today's opportunities"
        subtitle="Ranked by conservative net expected value against the executable ask, after fees, slippage, transfer and capital costs. Only markets with a probability estimate independent of their own price can appear here."
        right={
          <div className="text-right text-xs text-neutral-500 dark:text-neutral-400">
            {generatedAt && <div>Generated {localTime(generatedAt)}</div>}
            <div className="mt-1 flex gap-2">
              <ModeLink current={mode} target="live" horizon={horizon} />
              <ModeLink current={mode} target="demo" horizon={horizon} />
            </div>
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
        <EmptyState
          title="No qualifying opportunities in this window"
          body={
            (opps.empty_reason as string) ??
            "Nothing cleared the admission gate. A recommendation requires a conservative net EV above threshold against a probability estimate that does not come from the market's own price. Efficiently-priced markets routinely produce nothing, and reporting nothing is the correct result — see the watchlist below for markets that were scored but could not qualify."
          }
          action={
            mode === "live" ? (
              <Link
                href={`/${qs({ horizon, mode: "demo" })}`}
                className="text-sm underline"
              >
                View the demo dataset to see how a populated list looks
              </Link>
            ) : undefined
          }
        />
      ) : (
        <div className="space-y-3">
          {rows.map((o) => (
            <OpportunityCard key={o.id} o={o} />
          ))}
        </div>
      )}

      {watch?.data?.length ? (
        <section className="mt-10">
          <h2 className="text-sm font-semibold">
            Scored but not recommendable
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
                        {w.title}
                      </Link>
                    </td>
                    <td><PlatformChip platform={w.platform} /></td>
                    <td className="num">{cents(w.best_yes_ask)}</td>
                    <td className="num">{cents(w.spread)}</td>
                    <td className="num">{compactUsd(w.liquidity_usd)}</td>
                    <td className="text-neutral-500">
                      {relativeTime(w.expected_resolution_time)}
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

function ModeLink({
  current,
  target,
  horizon,
}: {
  current: DataMode;
  target: DataMode;
  horizon: string;
}) {
  const active = current === target;
  return (
    <Link
      href={`/${qs({ horizon, mode: target })}`}
      className={`rounded px-2 py-0.5 ${
        active
          ? "bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-900"
          : "border border-neutral-300 dark:border-neutral-700"
      }`}
    >
      {target}
    </Link>
  );
}

function OpportunityCard({ o }: { o: Opportunity }) {
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
              {o.title}
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
          value={relativeTime(o.expected_resolution_time)}
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
          {o.evidence_updated_at ? ageLabel(o.evidence_updated_at) : "none"}
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
