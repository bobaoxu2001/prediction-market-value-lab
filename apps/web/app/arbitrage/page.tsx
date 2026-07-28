import Link from "next/link";
import { apiGet, qs, type ArbitrageOpportunity, type DataMode } from "@/lib/api";
import { cents, compactUsd, displayTitle, localTime, pct, relativeTime, usd } from "@/lib/format";
import {
  ApiDown,
  ArbLabelChip,
  DemoBanner,
  EmptyState,
  Metric,
  PageHeader,
  PlatformChip,
  RiskFlags,
} from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function ArbitragePage({
  searchParams,
}: {
  searchParams: Promise<{
    mode?: DataMode;
    kind?: string;
    label?: string;
    view?: string;
  }>;
}) {
  const params = await searchParams;
  const mode: DataMode = params.mode === "demo" ? "demo" : "live";
  const view = params.view === "diagnostics" ? "diagnostics" : "actionable";

  const res = await apiGet<ArbitrageOpportunity[]>(
    `/arbitrage${qs({ mode, view, label: params.label, kind: params.kind, limit: 50 })}`,
  );
  if (!res) return <ApiDown />;

  const rows = res.data ?? [];
  const diagnostics = res.matching_diagnostics as
    | {
        pairs_examined?: number;
        verified_equivalent?: number;
        blocked_only_by_missing_info?: number;
        top_reasons?: { code: string; count: number; kind: string }[];
        diagnosis?: string;
      }
    | null
    | undefined;
  const meanings = (res.label_meanings ?? {}) as Record<string, string>;
  const counts = (res.counts_by_label ?? {}) as Record<string, number>;

  return (
    <div>
      <PageHeader
        title="Arbitrage scan"
        subtitle="Five scanners: binary complete-set, cross-platform, multi-outcome, logical-constraint and stale-quote. Only 'executable' claims a locked-in result, and it requires an exact settlement-rule match plus fillable depth after every cost."
      />
      <DemoBanner notice={res.demo_notice} />

      <div className="mb-4 flex flex-wrap gap-2">
        {(
          [
            ["actionable", "Actionable"],
            ["diagnostics", "Diagnostics"],
          ] as const
        ).map(([key, label]) => (
          <Link
            key={key}
            href={`/arbitrage${qs({ mode, view: key })}`}
            aria-current={view === key ? "page" : undefined}
            className={`rounded-lg border px-3 py-2 text-sm transition ${
              view === key
                ? "border-neutral-900 bg-neutral-900 text-white dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900"
                : "border-neutral-200 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:bg-neutral-900"
            }`}
          >
            {label}
          </Link>
        ))}
      </div>

      <p className="mb-4 max-w-3xl text-sm text-neutral-600 dark:text-neutral-400">
        {view === "actionable"
          ? "Only opportunities that cleared every gate: an exact settlement-rule match, fillable depth on every leg, and a net edge above the required safety margin after all costs."
          : "Research output, not trades. A stale quote, a rule mismatch or a logical inconsistency is a finding worth recording — it is not something to act on."}
      </p>

      {view === "diagnostics" && diagnostics?.pairs_examined ? (
        <div className="card mb-4 p-4">
          <h2 className="text-sm font-semibold">
            Why no cross-platform pair qualified
          </h2>
          <div className="mt-2 grid gap-3 sm:grid-cols-3">
            <Metric label="Pairs examined" value={String(diagnostics.pairs_examined)} />
            <Metric
              label="Verified equivalent"
              value={String(diagnostics.verified_equivalent ?? 0)}
            />
            <Metric
              label="Blocked only by missing data"
              value={String(diagnostics.blocked_only_by_missing_info ?? 0)}
            />
          </div>
          {diagnostics.top_reasons?.length ? (
            <ul className="mt-3 space-y-1 text-xs">
              {diagnostics.top_reasons.slice(0, 6).map((r) => (
                <li key={r.code} className="flex justify-between gap-3">
                  <span className="text-neutral-600 dark:text-neutral-400">
                    {r.code.replace(/_/g, " ")}
                    <span className="ml-2 opacity-60">({r.kind.replace(/_/g, " ")})</span>
                  </span>
                  <span className="num font-mono">{r.count}</span>
                </li>
              ))}
            </ul>
          ) : null}
          {diagnostics.diagnosis ? (
            <p className="mt-3 border-t border-neutral-200 pt-3 text-sm dark:border-neutral-800">
              {diagnostics.diagnosis}
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="card mb-4 p-4">
        <h2 className="text-sm font-semibold">What each label means</h2>
        <dl className="mt-2 grid gap-2 sm:grid-cols-2">
          {Object.entries(meanings).map(([label, meaning]) => (
            <div key={label} className="flex gap-2">
              <dt className="shrink-0">
                <ArbLabelChip label={label} />
              </dt>
              <dd className="text-xs text-neutral-600 dark:text-neutral-400">
                {meaning}
                {counts[label] ? (
                  <span className="ml-1 font-semibold">({counts[label]} now)</span>
                ) : null}
              </dd>
            </div>
          ))}
        </dl>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No arbitrage found in the latest scan"
          body={
            (res.empty_reason as string) ??
            "Finding nothing is the normal result. Both venues are actively arbitraged, and this scanner refuses to label anything executable unless every leg is fillable after all fees, slippage and capital costs."
          }
          action={
            mode === "live" ? (
              <Link href={`/arbitrage${qs({ mode: "demo" })}`} className="text-sm underline">
                View the demo dataset
              </Link>
            ) : undefined
          }
        />
      ) : (
        <div className="space-y-3">
          {rows.map((a) => (
            <article key={a.id} className="card p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <ArbLabelChip label={a.label} />
                    <span className="chip bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400">
                      {a.kind.replace(/_/g, " ")}
                    </span>
                    <span className="chip bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400">
                      rules: {a.rule_compatibility}
                    </span>
                  </div>
                  <p className="mt-2 truncate font-medium">{displayTitle(a.title)}</p>
                </div>
                <div className="text-right">
                  <div className="metric-label">Net profit / set</div>
                  <div className="num text-lg font-bold">
                    {cents(a.net_profit_per_set)}
                  </div>
                </div>
              </div>

              <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
                <Metric label="Gross edge / set" value={cents(a.gross_edge_per_set)} />
                <Metric label="All-in cost / set" value={cents(a.total_cost_per_set)} />
                <Metric label="Executable sets" value={Number(a.max_executable_sets).toFixed(0)} />
                <Metric label="Max net profit" value={usd(a.max_net_profit)} />
                <Metric label="Capital required" value={compactUsd(a.capital_required)} />
                <Metric label="Net ROI" value={pct(a.net_roi)} />
              </div>

              <div className="table-wrap mt-3">
                <table className="w-full">
                  <thead className="border-b border-neutral-200 dark:border-neutral-800">
                    <tr>
                      <th>Leg</th>
                      <th>Venue</th>
                      <th>Side</th>
                      <th>Price</th>
                      <th>Size available</th>
                      <th>Fee / ct</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                    {a.legs.map((leg, i) => (
                      <tr key={`${leg.platform_market_id}-${leg.side}-${i}`}>
                        <td className="max-w-sm truncate">
                          {leg.market_id ? (
                            <Link href={`/market/${leg.market_id}`} className="hover:underline">
                              {leg.title || leg.platform_market_id}
                            </Link>
                          ) : (
                            leg.title || leg.platform_market_id
                          )}
                        </td>
                        <td><PlatformChip platform={leg.platform} /></td>
                        <td className="uppercase">{leg.side}</td>
                        <td className="num">{cents(leg.price)}</td>
                        <td className="num">{Number(leg.size_available).toFixed(0)}</td>
                        <td className="num">{cents(leg.fee_per_contract)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {a.risk_flags?.length > 0 && (
                <ul className="mt-3 space-y-1 text-xs text-neutral-600 dark:text-neutral-400">
                  {a.risk_flags.map((flag, i) => (
                    <li key={i} className="flex gap-2">
                      <span className="text-warn dark:text-warn-dark">!</span>
                      <span>{flag}</span>
                    </li>
                  ))}
                </ul>
              )}

              <div className="mt-3 border-t border-neutral-100 pt-2 text-[11px] text-neutral-500 dark:border-neutral-800">
                Quote age {a.quote_age_seconds ?? "—"}s · resolves{" "}
                {relativeTime(a.expected_resolution_time)} · scanned {localTime(a.created_at)}
              </div>
            </article>
          ))}
        </div>
      )}

      <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">{res.disclaimer}</p>
    </div>
  );
}
