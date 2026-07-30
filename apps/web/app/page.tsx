import Link from "next/link";
import {
  apiGet,
  qs,
  type DataMode,
  type Opportunity,
  type WatchlistItem,
} from "@/lib/api";
import type { Divergence, FunnelStage } from "@/lib/api";
import { ageRelativeToSnapshot, cents, compactUsd, displayTitle, localTime, pct, prob, relativeToSnapshot, signedCents, usd, utcTime } from "@/lib/format";
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
    apiGet<{
      snapshot_mode?: boolean;
      freshest_quote_observed_at?: string | null;
      runtime_mode?: string;
      model_version?: string;
      trading_execution_enabled?: boolean;
      row_counts?: Record<string, number>;
      jobs?: Array<{
        job_name: string;
        status: string;
        started_at?: string | null;
        error?: string;
      }>;
      pipeline?: { scheduler_status?: string; public_serving_mode?: string } | null;
    }>("/system"),
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
      {/*
       * The briefing comes first.
       *
       * The first viewport used to be entirely product introduction, so none of
       * the questions a returning reader actually opens this page with - what is
       * actionable, what is not, why nothing, how fresh - was answered above the
       * fold. Positioning still exists; it now sits below the data.
       */}
      <ResearchSnapshot
        actionable={rows.length}
        watchlist={watch?.data?.length ?? 0}
        divergences={diverge?.data?.length ?? 0}
        system={system?.data}
        generatedAt={generatedAt}
        horizon={horizon}
        mode={mode}
      />

      <PageHeader
        title="Today's opportunities"
        subtitle="Ranked by conservative net expected value against the executable ask, after fees, slippage, transfer and capital costs. Only markets with a probability estimate independent of their own price can appear here."
        right={
          <div className="text-right t-meta">
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
              aria-current={active ? "page" : undefined}
              className={`rounded-[2px] border px-3 py-2 text-sm transition-colors ${
                active
                  ? "border-accent bg-accent text-accent-ink"
                  : "border-line text-ink-muted hover:bg-sunken hover:text-ink"
              }`}
            >
              {h.label}
              <span className="ml-2 opacity-60">{counts[h.key] ?? 0}</span>
            </Link>
          );
        })}
      </div>

      {rows.length > 0 ? (
        <div className="space-y-3">
          {rows.map((o) => (
            <OpportunityCard key={o.id} o={o} snapshotAt={snapshotAt} />
          ))}
        </div>
      ) : funnel?.data?.length ? null : (
        <EmptyState
          title="No actionable opportunities right now"
          body={(opps.empty_reason as string) ?? ""}
        />
      )}

      {/*
       * The funnel is shown whether or not anything qualified.
       *
       * It used to render only on an empty list, which removed the one thing that
       * lets a reader calibrate a populated list: how many candidates were
       * examined and on what grounds the rest were declined. Ten rows with no
       * denominator is the weaker page.
       */}
      {funnel?.data?.length ? (
        <div className={rows.length > 0 ? "mt-10" : undefined}>
          <FilterFunnel
            stages={funnel.data}
            conclusion={
              (funnel.conclusion as string) ??
              "No actionable opportunities right now."
            }
            mode={mode}
            horizon={horizon}
            hasRows={rows.length > 0}
          />
        </div>
      ) : null}

      {diverge?.data?.length ? (
        <section className="mt-10">
          <h2 className="t-section-title">
            Where the model most disagrees with the market
          </h2>
          <p className="mt-1 t-prose">
            {(diverge.explanation as string) ??
              "These are not recommendations."}
          </p>
          <div className="panel table-wrap mt-3">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr>
                  <th scope="col" className="col-sticky col-title">Contract</th>
                  <th scope="col">Venue</th>
                  <th scope="col" className="num">Market-implied</th>
                  <th scope="col" className="num">Independent model</th>
                  <th scope="col" className="num">Model interval</th>
                  <th scope="col" className="num">Divergence</th>
                  <th scope="col" className="num">Confidence</th>
                  <th scope="col">Resolves</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {diverge.data.map((d) => {
                  const div = Number(d.divergence);
                  return (
                    <tr key={d.market_id}>
                      <td className="col-sticky col-title">
                        <Link href={`/market/${d.market_id}`} className="hover:underline">
                          {displayTitle(d.title)}
                        </Link>
                      </td>
                      <td><PlatformChip platform={d.platform} /></td>
                      <td className="num">{prob(d.market_implied_probability)}</td>
                      <td className="num font-semibold">{prob(d.model_probability)}</td>
                      <td className="num text-ink-faint">
                        {prob(d.model_low)}–{prob(d.model_high)}
                      </td>
                      <td className={`num font-semibold ${div > 0 ? "text-edge" : "text-risk"}`}>
                        {div > 0 ? "+" : ""}{(div * 100).toFixed(1)}pp
                      </td>
                      <td className="num">{pct(d.model_confidence)}</td>
                      <td className="text-ink-faint">
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
          <h2 className="t-section-title">
            Watchlist — not actionable yet
          </h2>
          <p className="mt-1 t-prose">
            {(watch.explanation as string) ??
              "These markets were scored, but the only information available was the market's own price."}{" "}
            They are shown so the coverage gap is visible. They are{" "}
            <strong>not</strong> opportunities.
          </p>
          <div className="panel table-wrap mt-3">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr>
                  <th scope="col" className="col-sticky col-title">Contract</th>
                  <th scope="col">Venue</th>
                  <th scope="col" className="num">YES ask</th>
                  <th scope="col" className="num">Spread</th>
                  <th scope="col" className="num">Depth</th>
                  <th scope="col">Resolves</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {watch.data.map((w) => (
                  <tr key={w.market_id}>
                    <td className="col-sticky col-title">
                      <Link href={`/market/${w.market_id}`} className="hover:underline">
                        {displayTitle(w.title)}
                      </Link>
                    </td>
                    <td><PlatformChip platform={w.platform} /></td>
                    <td className="num">{cents(w.best_yes_ask)}</td>
                    <td className="num">{cents(w.spread)}</td>
                    <td className="num">{compactUsd(w.liquidity_usd)}</td>
                    <td className="text-ink-faint">
                      {relativeToSnapshot(w.expected_resolution_time, snapshotAt)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <Hero
        demoHref={`/${qs({ horizon, mode: "demo" })}`}
        backtestHref={`/backtest${qs({ mode: "demo" })}`}
        guidedHref={`/demo${qs({ step: 1, mode: "demo" })}`}
        caseStudyHref={`/case-study${qs({ mode: "demo" })}`}
      />

      <p className="mt-6 t-meta">{opps.disclaimer}</p>
    </div>
  );
}

/**
 * The first viewport: what changed, what is actionable, what is not, how fresh the
 * data is, and where to look next.
 *
 * Every figure here is already on the page or already in `/system` - no new API
 * call and no new backend field. The point is ordering, not new data.
 */
function ResearchSnapshot({
  actionable,
  watchlist,
  divergences,
  system,
  generatedAt,
  horizon,
  mode,
}: {
  actionable: number;
  watchlist: number;
  divergences: number;
  system?: {
    snapshot_mode?: boolean;
    freshest_quote_observed_at?: string | null;
    runtime_mode?: string;
    model_version?: string;
    trading_execution_enabled?: boolean;
    row_counts?: Record<string, number>;
    jobs?: Array<{
      job_name: string;
      status: string;
      started_at?: string | null;
      error?: string;
    }>;
    pipeline?: { scheduler_status?: string; public_serving_mode?: string } | null;
  };
  generatedAt?: string;
  horizon: string;
  mode: DataMode;
}) {
  const jobs = system?.jobs ?? [];
  const failed = jobs.filter((j) => j.status === "failed");
  const marketCount = system?.row_counts?.markets ?? null;
  const servingMode =
    system?.pipeline?.public_serving_mode ??
    (system?.snapshot_mode ? "Read-only snapshot" : system?.runtime_mode ?? "unknown");

  return (
    <section className="mb-8" aria-labelledby="briefing">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h1 id="briefing" className="t-page-title">
          Research briefing
        </h1>
        <p className="t-meta">
          {generatedAt ? <>Ranking generated {localTime(generatedAt)}</> : null}
        </p>
      </div>

      {/* A rule-separated measurement band, not a row of cards. */}
      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 border-y border-line py-4 sm:grid-cols-3 lg:grid-cols-6">
        <Brief
          label={`Actionable · ${horizon}`}
          value={String(actionable)}
          note={actionable === 0 ? "nothing cleared every gate" : "ranked below"}
          tone={actionable > 0 ? "edge" : "muted"}
        />
        <Brief
          label="Watchlist"
          value={String(watchlist)}
          note="scored, not actionable"
        />
        <Brief
          label="Model disagreements"
          value={String(divergences)}
          note="not recommendations"
        />
        <Brief
          label="Markets covered"
          value={marketCount == null ? "—" : marketCount.toLocaleString()}
          note="ingested both venues"
        />
        <Brief
          label="Freshest quote"
          value={
            system?.freshest_quote_observed_at
              ? utcTime(system.freshest_quote_observed_at)
              : "—"
          }
          note="single most recent observation"
          wide
        />
        <Brief
          label="Serving mode"
          value={servingMode}
          note={
            system?.trading_execution_enabled
              ? "trading execution ENABLED"
              : "trading execution disabled"
          }
          tone={system?.trading_execution_enabled ? "warn" : "muted"}
        />
      </dl>

      {/* Pipeline health, stated in text and not only by colour. */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 t-meta">
        <span>
          <span className="t-label">Pipeline </span>
          {jobs.length === 0 ? (
            <span className="text-ink-muted">no job history recorded</span>
          ) : failed.length > 0 ? (
            <span className="font-semibold text-risk">
              {failed.length} of {jobs.length} jobs failed —{" "}
              {failed.map((j) => j.job_name).join(", ")}
            </span>
          ) : (
            <span className="text-ink-muted">
              all {jobs.length} recorded jobs succeeded
            </span>
          )}
        </span>
        {system?.model_version ? (
          <span>
            <span className="t-label">Model </span>
            <span className="num text-ink-muted">{system.model_version}</span>
          </span>
        ) : null}
        <Link href="/system" className="underline underline-offset-2">
          Operational detail
        </Link>
        {mode === "live" ? (
          <Link
            href={`/${qs({ horizon, mode: "demo" })}`}
            className="underline underline-offset-2"
          >
            Switch to the demo dataset
          </Link>
        ) : null}
      </div>
    </section>
  );
}

function Brief({
  label,
  value,
  note,
  tone = "muted",
  wide = false,
}: {
  label: string;
  value: string;
  note?: string;
  tone?: "muted" | "edge" | "warn";
  wide?: boolean;
}) {
  const toneClass =
    tone === "edge" ? "text-edge" : tone === "warn" ? "text-warn" : "text-ink";
  // Timestamps and mode names are much longer than a count. Stepping them down
  // one size keeps the band on a single line instead of reflowing one cell to two
  // rows and pulling the whole row out of alignment.
  const sizeClass = value.length > 12 ? "text-[0.8125rem]" : "text-base";
  return (
    <div className={wide ? "col-span-2 sm:col-span-1" : undefined}>
      <dt className="t-label">{label}</dt>
      <dd>
        <div className={`num font-semibold leading-snug ${sizeClass} ${toneClass}`}>
          {value}
        </div>
        {note ? <div className="t-meta mt-0.5">{note}</div> : null}
      </dd>
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
          <div className="num shrink-0 rounded bg-sunken px-2 py-1 text-sm font-bold">
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
                <span className="chip chip-neutral">
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
          <div className="text-[11px] text-ink-faint">per contract</div>
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

      <div className="mt-3 grid gap-2 border-t border-line-subtle pt-3 text-[11px] text-ink-faint sm:grid-cols-3">
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
  hasRows,
}: {
  stages: FunnelStage[];
  conclusion: string;
  mode: DataMode;
  horizon: string;
  hasRows: boolean;
}) {
  const widest = Math.max(...stages.map((s) => s.count), 1);
  return (
    <div className="panel p-5">
      <h2 className="t-section-title">
        {hasRows
          ? "What the ranking above was selected from"
          : "How today\u2019s markets were filtered"}
      </h2>
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
              <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-sunken">
                <div
                  className={`h-full rounded-full ${
                    last && stage.count === 0
                      ? "bg-ink-faint"
                      : "bg-ink"
                  }`}
                  style={{ width: `${share}%` }}
                />
              </div>
              <div className="mt-0.5 text-xs text-ink-faint">{stage.note}</div>
            </li>
          );
        })}
      </ol>
      <p className="mt-5 border-t border-line pt-4 text-sm font-medium">
        {conclusion}{" "}
        <span className="font-normal text-ink-muted">
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
