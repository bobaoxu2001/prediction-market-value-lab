import Link from "next/link";
import { apiGet, qs, type DataMode, type TrackRecordRow } from "@/lib/api";
import { cents, displayTitle, localTime, prob, signedCents, usd } from "@/lib/format";
import { withResearchMode } from "@/lib/research-mode";
import {
  ApiDown, DemoBanner, EmptyState, PageHeader, PlatformChip, SideChip,
} from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function TrackRecordPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: DataMode; horizon?: string; settled?: string; offset?: string }>;
}) {
  const params = await searchParams;
  const mode: DataMode = params.mode === "demo" ? "demo" : "live";
  const offset = Number(params.offset ?? 0) || 0;

  const res = await apiGet<TrackRecordRow[]>(
    `/track-record${qs({
      mode, horizon: params.horizon,
      settled_only: params.settled === "1" ? true : undefined,
      limit: 100, offset,
    })}`,
  );
  if (!res) return <ApiDown />;

  const rows = res.data ?? [];
  const total = (res.total as number) ?? 0;
  const wins = (res.wins_in_page as number) ?? 0;
  const losses = (res.losses_in_page as number) ?? 0;

  return (
    <div>
      <PageHeader
        title="Track record"
        subtitle="Every recommendation ever published, exactly as published. Entry price, fair probability, confidence interval and evidence are frozen at publication and are never rewritten."
      />
      <DemoBanner notice={res.demo_notice} />

      <div className="card mb-4 p-3 text-sm">
        <p className="text-ink-muted">
          {(res.integrity_note as string) ??
            "Snapshots are append-only. Losing recommendations are shown and cannot be filtered out."}
        </p>
        {(wins > 0 || losses > 0) && (
          <p className="mt-2">
            On this page: <strong className="text-edge">{wins} winners</strong>{" "}
            and <strong className="text-risk">{losses} losers</strong> among
            settled recommendations.
          </p>
        )}
      </div>

      <div className="mb-3 flex flex-wrap gap-2 text-sm">
        {["", "24h", "7d", "30d"].map((h) => (
          <Link key={h || "all"}
            href={withResearchMode(
              `/track-record${qs({ horizon: h || undefined, settled: params.settled })}`,
              mode,
            )}
            className={`rounded border px-3 py-1 ${(params.horizon ?? "") === h ? "border-neutral-900 bg-neutral-900 text-white dark:border-line-subtle dark:bg-neutral-100 dark:text-neutral-900" : "border-line"}`}>
            {h || "All horizons"}
          </Link>
        ))}
        <Link href={withResearchMode(
          `/track-record${qs({ horizon: params.horizon, settled: params.settled === "1" ? undefined : "1" })}`,
          mode,
        )}
          className={`rounded border px-3 py-1 ${params.settled === "1" ? "border-neutral-900 bg-neutral-900 text-white dark:border-line-subtle dark:bg-neutral-100 dark:text-neutral-900" : "border-line"}`}>
          Settled only
        </Link>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No recommendations recorded yet"
          body="Snapshots are written by `make snapshot` after a ranking run. Until then there is no history to show."
          action={mode === "live" ? (
            <Link href={withResearchMode("/track-record", "demo")} className="text-sm underline">
              View the demo dataset
            </Link>
          ) : undefined}
        />
      ) : (
        <>
          <div className="panel table-wrap">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr>
                  <th>Published</th><th>Market</th><th>Venue</th><th>Side</th>
                  <th>Entry then</th><th>All-in cost</th><th>Fair prob</th>
                  <th>Interval</th><th>EV then</th><th>Result</th>
                  <th>Realised / ct</th><th>On $100</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {rows.map((r) => {
                  const realised = r.realized_profit_per_contract
                    ? Number(r.realized_profit_per_contract) : null;
                  return (
                    <tr key={r.id} className="hover:bg-neutral-50 dark:hover:bg-neutral-900">
                      <td className="text-ink-faint">{localTime(r.recommendation_created_at)}</td>
                      <td className="max-w-xs truncate">
                        <Link
                          href={withResearchMode(`/market/${r.market_id}`, mode)}
                          className="hover:underline"
                        >
                          {displayTitle(r.market_title)}
                        </Link>
                      </td>
                      <td><PlatformChip platform={r.platform} /></td>
                      <td><SideChip side={r.side} /></td>
                      <td className="num">{cents(r.entry_price_at_recommendation)}</td>
                      <td className="num">{cents(r.total_cost_at_recommendation)}</td>
                      <td className="num">{prob(r.fair_probability)}</td>
                      <td className="num text-ink-faint">
                        {prob(r.confidence_interval?.[0])}–{prob(r.confidence_interval?.[1])}
                      </td>
                      <td className="num">{signedCents(r.expected_value)}</td>
                      <td>
                        {r.final_result ? (
                          <span className="chip bg-neutral-100 uppercase text-neutral-600 dark:bg-neutral-800 dark:text-ink-faint">
                            {r.final_result}
                          </span>
                        ) : (
                          <span className="text-ink-faint">pending</span>
                        )}
                      </td>
                      <td className={`num ${realised == null ? "" : realised > 0 ? "text-edge" : "text-risk"}`}>
                        {signedCents(r.realized_profit_per_contract)}
                      </td>
                      <td className={`num ${realised == null ? "" : realised > 0 ? "text-edge" : "text-risk"}`}>
                        {r.realized_profit_at_100_usd ? usd(r.realized_profit_at_100_usd) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex items-center justify-between text-sm">
            <span className="text-ink-faint">
              {offset + 1}–{offset + rows.length} of {total.toLocaleString()}
            </span>
            <div className="flex gap-2">
              {offset > 0 && (
                <Link href={withResearchMode(
                  `/track-record${qs({ horizon: params.horizon, settled: params.settled, offset: Math.max(0, offset - 100) })}`,
                  mode,
                )}
                  className="rounded border border-neutral-300 px-3 py-1 dark:border-neutral-700">Previous</Link>
              )}
              {rows.length === 100 && (
                <Link href={withResearchMode(
                  `/track-record${qs({ horizon: params.horizon, settled: params.settled, offset: offset + 100 })}`,
                  mode,
                )}
                  className="rounded border border-neutral-300 px-3 py-1 dark:border-neutral-700">Next</Link>
              )}
            </div>
          </div>
        </>
      )}
      <p className="mt-6 text-xs text-ink-faint">{res.disclaimer}</p>
    </div>
  );
}
