import Link from "next/link";
import { apiGet, qs, type BacktestRun, type DataMode } from "@/lib/api";
import { localDate, pct, usd } from "@/lib/format";
import {
  ApiDown, DemoBanner, EmptyState, Metric, PageHeader,
} from "@/components/ui";
import { CalibrationChart } from "@/components/CalibrationChart";

export const dynamic = "force-dynamic";

export default async function BacktestPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: DataMode; strategy?: string }>;
}) {
  const params = await searchParams;
  const mode: DataMode = params.mode === "demo" ? "demo" : "live";
  const res = await apiGet<BacktestRun[]>(`/backtest${qs({ mode })}`);
  if (!res) return <ApiDown />;

  const runs = res.data ?? [];
  const focus =
    runs.find((r) => r.strategy === params.strategy) ??
    runs.find((r) => r.strategy === "top10_equal_10usd" && r.n_settled > 0) ??
    runs.find((r) => r.n_settled > 0) ??
    runs[0];

  return (
    <div>
      <PageHeader
        title="Backtest"
        subtitle="Walk-forward by construction: the engine reads only immutable snapshots frozen at publication time. It never re-prices an entry, never re-runs the model, and applies selection within each publication day."
      />
      <DemoBanner notice={res.demo_notice} />

      {runs.length === 0 ? (
        <EmptyState
          title="No backtest results yet"
          body={(res.empty_reason as string) ?? "Published recommendations must reach their resolution date before there is anything to measure."}
          action={
            mode === "live" ? (
              <Link href={`/backtest${qs({ mode: "demo" })}`} className="text-sm underline">
                View the demo dataset
              </Link>
            ) : undefined
          }
        />
      ) : (
        <>
          <div className="card table-wrap mb-4">
            <table className="w-full">
              <thead className="border-b border-neutral-200 dark:border-neutral-800">
                <tr>
                  <th>Strategy</th><th>Recs</th><th>Settled</th><th>Win rate</th>
                  <th>ROI</th><th>Total P&amp;L</th><th>Max DD</th><th>Profit factor</th>
                  <th>Brier</th><th>vs market</th><th>Data quality</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                {runs.map((r) => {
                  const m = r.metrics ?? {};
                  const active = focus?.strategy === r.strategy;
                  return (
                    <tr key={r.run_id}
                      className={active ? "bg-neutral-50 dark:bg-neutral-900" : ""}>
                      <td>
                        <Link href={`/backtest${qs({ mode, strategy: r.strategy })}`}
                          className="hover:underline" title={r.description}>
                          {r.strategy}
                        </Link>
                      </td>
                      <td className="num">{r.n_recommendations}</td>
                      <td className="num">{r.n_settled}</td>
                      <td className="num">{m.win_rate != null ? pct(m.win_rate) : "—"}</td>
                      <td className={`num ${(m.roi ?? 0) > 0 ? "text-edge dark:text-edge-dark" : (m.roi ?? 0) < 0 ? "text-risk dark:text-risk-dark" : ""}`}>
                        {m.roi != null ? pct(m.roi) : "—"}
                      </td>
                      <td className="num">{m.total_pnl ? usd(m.total_pnl) : "—"}</td>
                      <td className="num">{m.max_drawdown ? usd(m.max_drawdown) : "—"}</td>
                      <td className="num">{m.profit_factor?.toFixed(2) ?? "—"}</td>
                      <td className="num">{m.brier_score?.toFixed(4) ?? "—"}</td>
                      <td className={`num ${(m.brier_improvement_vs_market ?? 0) > 0 ? "text-edge dark:text-edge-dark" : "text-risk dark:text-risk-dark"}`}>
                        {m.brier_improvement_vs_market?.toFixed(5) ?? "—"}
                      </td>
                      <td>
                        <span className="chip bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400"
                          title={r.data_quality_meaning}>
                          {r.data_quality}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <p className="mb-4 text-xs text-neutral-500 dark:text-neutral-400">
            <strong>vs market</strong> is the Brier improvement over simply trusting the
            market&apos;s own implied probability. Positive means the model added
            information; zero or negative means it did not, regardless of P&amp;L.
          </p>

          {focus && (
            <>
              <section className="card mb-4 p-4">
                <h2 className="text-sm font-semibold">{focus.strategy}</h2>
                <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
                  {focus.description}
                </p>
                <div className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-6">
                  <Metric label="Window start" value={localDate(focus.window_start)} />
                  <Metric label="Window end" value={localDate(focus.window_end)} />
                  <Metric label="Settled trades" value={focus.n_settled} />
                  <Metric label="Total staked" value={focus.metrics.total_stake ? usd(focus.metrics.total_stake) : "—"} />
                  <Metric label="Log loss" value={focus.metrics.log_loss?.toFixed(4) ?? "—"} />
                  <Metric label="Sharpe-like / bet" value={focus.metrics.sharpe_like_per_bet?.toFixed(3) ?? "—"} />
                  <Metric label="Avg predicted" value={focus.metrics.avg_predicted_probability != null ? pct(focus.metrics.avg_predicted_probability) : "—"} />
                  <Metric label="Avg market" value={focus.metrics.avg_market_probability != null ? pct(focus.metrics.avg_market_probability) : "—"} />
                </div>
                {focus.notes && (
                  <p className="mt-3 rounded border border-warn/40 bg-warn/10 p-2 text-xs dark:border-warn-dark/40 dark:bg-warn-dark/10">
                    {focus.notes}
                  </p>
                )}
              </section>

              <section className="card mb-4 p-4">
                <h2 className="mb-1 text-sm font-semibold">Reliability diagram</h2>
                <p className="mb-3 text-xs text-neutral-500">
                  Points below the diagonal mean the model was overconfident in that
                  band; above means underconfident. Hover a point for its sample size —
                  a bin with a handful of observations says nothing.
                </p>
                <CalibrationChart
                  model={focus.metrics.calibration_curve ?? []}
                  market={focus.metrics.market_calibration_curve}
                />
              </section>

              {Object.keys(focus.by_slice ?? {}).length > 0 && (
                <section className="card p-4">
                  <h2 className="mb-3 text-sm font-semibold">Breakdown</h2>
                  <div className="table-wrap">
                    <table className="w-full">
                      <thead className="border-b border-neutral-200 dark:border-neutral-800">
                        <tr><th>Slice</th><th>Settled</th><th>Win rate</th><th>ROI</th><th>Brier</th><th>vs market</th></tr>
                      </thead>
                      <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                        {Object.entries(focus.by_slice).map(([name, m]) => (
                          <tr key={name}>
                            <td>{name}</td>
                            <td className="num">{m.n_settled}</td>
                            <td className="num">{m.win_rate != null ? pct(m.win_rate) : "—"}</td>
                            <td className="num">{m.roi != null ? pct(m.roi) : "—"}</td>
                            <td className="num">{m.brier_score?.toFixed(4) ?? "—"}</td>
                            <td className="num">{m.brier_improvement_vs_market?.toFixed(5) ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}
            </>
          )}
        </>
      )}
      <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">{res.disclaimer}</p>
    </div>
  );
}
