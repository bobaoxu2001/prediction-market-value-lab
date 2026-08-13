import Link from "next/link";
import { apiGet, qs, type BacktestRun, type DataMode } from "@/lib/api";
import { METRIC_HELP, localDate, pct, strategyLabel, usd } from "@/lib/format";
import { withResearchMode } from "@/lib/research-mode";
import {
  ApiDown, DemoBanner, EmptyState, Metric, PageHeader, VerdictCard, toneFor,
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

      {/*
       * This caveat used to render in a neutral grey box while the ROI figures
       * beside it were coloured green - the most important warning on the page was
       * its quietest element. It now carries the same weight as any other demo
       * notice, because "these returns are not evidence of returns" is the single
       * claim a reader must not miss here.
       */}
      {mode === "demo" && runs.length > 0 ? (
        <div className="mb-5 rounded-[3px] border-l-2 border border-demo/50 border-l-demo bg-demo/10 p-4 text-sm">
          <div className="t-label text-demo">Demo forecaster — not a track record</div>
          <p className="mt-1.5 text-ink">
            <span className="font-semibold">
              This demo forecaster is deliberately imperfect.
            </span>{" "}
            It is overconfident in the tails and several strategies lose money. Use it
            to see how the platform exposes poor calibration, weak strategies and
            misleading profitability — not as evidence of expected returns.
          </p>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 t-meta">
            <span>1. Compare strategies below</span>
            <span>2. Inspect the calibration curve</span>
            <span>
              3.{" "}
              <Link
                href={withResearchMode(
                  `/track-record${qs({ settled_only: true })}`,
                  mode,
                )}
                className="underline"
              >
                Open the losing recommendations
              </Link>
            </span>
          </div>
        </div>
      ) : null}

      {focus && focus.n_settled > 0 ? (
        <Verdict run={focus} mode={mode} />
      ) : null}

      {runs.length === 0 ? (
        <EmptyState
          title="No backtest results yet"
          body={(res.empty_reason as string) ?? "Published recommendations must reach their resolution date before there is anything to measure."}
          action={
            mode === "live" ? (
              <Link href={withResearchMode("/backtest", "demo")} className="text-sm underline">
                View the demo dataset
              </Link>
            ) : undefined
          }
        />
      ) : (
        <>
          <div className="panel table-wrap mb-4">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr>
                  <th scope="col" className="col-sticky col-title">Strategy</th>
                  <th scope="col" className="num">Recs</th>
                  <th scope="col" className="num">Settled</th>
                  <th scope="col" className="num">Win rate</th>
                  <th scope="col" className="num">ROI</th>
                  <th scope="col" className="num">Total P&amp;L</th>
                  <th scope="col" className="num">Max DD</th>
                  <th scope="col" className="num">Profit factor</th>
                  <th scope="col" className="num">Brier</th>
                  <th scope="col" className="num">vs market</th>
                  <th scope="col">Data quality</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {runs.map((r) => {
                  const m = r.metrics ?? {};
                  const active = focus?.strategy === r.strategy;
                  return (
                    <tr key={r.run_id} className={active ? "bg-sunken" : "row-hover"}>
                      {/* Sticky, so the strategy name stays visible while the
                          eleven measurement columns scroll. */}
                      <td className={`col-sticky col-title ${active ? "bg-sunken" : ""}`}>
                        <Link href={withResearchMode(
                          `/backtest${qs({ strategy: r.strategy })}`,
                          mode,
                        )}
                          className="hover:underline" title={r.description}>
                          {strategyLabel(r.strategy)}
                        </Link>
                      </td>
                      <td className="num">{r.n_recommendations}</td>
                      {/*
                       * Sample size is the qualifier on every other number in the
                       * row, so a thin sample is labelled in the row itself rather
                       * than left to a footnote nobody reads.
                       */}
                      <td className="num">
                        {r.n_settled}
                        {r.n_settled > 0 && r.n_settled < 100 ? (
                          <span
                            className="ml-1 text-[10px] uppercase tracking-wide text-warn"
                            title="Fewer than 100 settled results — cannot separate skill from luck."
                          >
                            thin
                          </span>
                        ) : null}
                      </td>
                      <td className="num">{m.win_rate != null ? pct(m.win_rate) : "—"}</td>
                      {/*
                       * In demo mode the ROI column is left uncoloured. Painting a
                       * synthetic forecaster's return green is the page celebrating
                       * a number it has just finished disclaiming.
                       */}
                      <td
                        className={`num ${
                          mode === "demo"
                            ? "text-ink-muted"
                            : (m.roi ?? 0) > 0
                              ? "text-edge"
                              : (m.roi ?? 0) < 0
                                ? "text-risk"
                                : ""
                        }`}
                      >
                        {m.roi != null ? pct(m.roi) : "—"}
                      </td>
                      <td className="num">{m.total_pnl != null ? usd(m.total_pnl) : "—"}</td>
                      <td className="num">{m.max_drawdown != null ? usd(m.max_drawdown) : "—"}</td>
                      <td className="num">{m.profit_factor?.toFixed(2) ?? "—"}</td>
                      <td className="num">{m.brier_score?.toFixed(4) ?? "—"}</td>
                      <td className={`num ${toneFor(m.brier_improvement_vs_market)}`}>
                        {m.brier_improvement_vs_market?.toFixed(5) ?? "—"}
                      </td>
                      <td>
                        <span className="chip bg-sunken text-ink-muted"
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

          <p className="mb-4 text-xs text-ink-faint">
            <strong>vs market</strong> is the Brier improvement over simply trusting the
            market&apos;s own implied probability. Positive means the model added
            information; zero or negative means it did not, regardless of P&amp;L.
          </p>

          {focus && (
            <>
              <section className="panel mb-4 p-4">
                <h2 className="text-sm font-semibold">
                  {strategyLabel(focus.strategy)}{" "}
                  <span className="font-mono text-xs font-normal text-ink-faint">
                    {focus.strategy}
                  </span>
                </h2>
                <p className="mt-1 text-sm text-ink-muted">
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
                  <p className="mt-3 rounded border border-warn/40 bg-warn/10 p-2 text-xs">
                    {focus.notes}
                  </p>
                )}
              </section>

              <section className="panel mb-4 p-4">
                <h2 className="t-section-title mb-1">Reliability diagram</h2>
                <p className="mb-3 text-xs text-ink-faint">
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
                <section className="panel p-4">
                  <h2 className="t-section-title mb-3">Breakdown</h2>
                  <div className="table-wrap">
                    <table className="w-full">
                      <thead className="border-b border-line">
                        <tr><th>Slice</th><th>Settled</th><th>Win rate</th><th>ROI</th><th>Brier</th><th>vs market</th></tr>
                      </thead>
                      <tbody className="divide-y divide-line-subtle">
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
      <p className="mt-6 text-xs text-ink-faint">{res.disclaimer}</p>
    </div>
  );
}

/**
 * Answers the three questions a reader actually has, before the 11-column table:
 * did it make money, was it more accurate than the market, and is the sample big
 * enough to mean anything.
 */
function Verdict({ run, mode }: { run: BacktestRun; mode: DataMode }) {
  const m = run.metrics;
  const beat = m.brier_improvement_vs_market;
  const beatsMarket = beat != null && beat > 0;
  const roi = m.roi;
  const settled = m.n_settled ?? 0;
  const thinSample = settled < 100;

  return (
    <section className="mb-6">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <VerdictCard
          label="More accurate than the market?"
          value={beat == null ? "Unknown" : beatsMarket ? "Yes" : "No"}
          sub={beat == null ? "no comparison available" : `Brier ${beat > 0 ? "+" : ""}${beat.toFixed(5)}`}
          tone={beat == null ? "text-ink-faint" : toneFor(beat)}
          help={METRIC_HELP.vs_market}
        />
        {/*
          * Uncoloured in demo mode, for the same reason the ROI column is: a
          * green headline return from a forecaster the page has just called
          * deliberately imperfect reads as a result rather than an illustration.
          */}
        <VerdictCard
          label="Net ROI"
          value={roi == null ? "—" : `${roi > 0 ? "+" : ""}${(roi * 100).toFixed(1)}%`}
          sub={
            m.total_pnl != null
              ? `${usd(m.total_pnl)} on ${m.total_stake != null ? usd(m.total_stake) : "—"} staked${mode === "demo" ? " (demo)" : ""}`
              : undefined
          }
          tone={mode === "demo" ? "text-ink" : toneFor(roi)}
          help={METRIC_HELP.roi}
        />
        <VerdictCard
          label="Settled trades"
          value={String(settled)}
          sub={thinSample ? "too few to separate skill from luck" : "sample size"}
          tone={thinSample ? "text-ink-faint" : ""}
          help={METRIC_HELP.settled}
        />
        <VerdictCard
          label="Max drawdown"
          value={m.max_drawdown != null ? usd(m.max_drawdown) : "—"}
          sub="worst peak-to-trough"
          tone={toneFor(m.max_drawdown != null ? Number(m.max_drawdown) : null)}
          help={METRIC_HELP.max_drawdown}
        />
      </div>
      <p className="mt-3 text-sm text-ink">
        <span className="font-medium">{strategyLabel(run.strategy)}</span>{" "}
        {beat == null
          ? "has no market comparison available"
          : beatsMarket
            ? `improved Brier score by ${beat.toFixed(5)} versus market prices`
            : `did NOT beat market prices (Brier ${beat.toFixed(5)})`}
        {roi != null ? `, with a net ROI of ${(roi * 100).toFixed(1)}%` : ""}
        {`, based on ${settled} settled recommendation${settled === 1 ? "" : "s"}`}
        {thinSample ? " — too small a sample to draw conclusions from." : "."}
      </p>
    </section>
  );
}
