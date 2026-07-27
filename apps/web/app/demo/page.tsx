import Link from "next/link";
import {
  apiGet, qs,
  type BacktestRun, type CaseStudy, type DataMode, type FunnelStage, type SystemInfo,
  type TrackRecordRow,
} from "@/lib/api";
import { METRIC_HELP, cents, count, localTime, pct, prob, strategyLabel, usd } from "@/lib/format";
import { ApiDown, DemoBanner, HelpDot, VerdictCard, toneFor } from "@/components/ui";

export const dynamic = "force-dynamic";

const TOTAL_STEPS = 5;

/**
 * URL-driven guided tour.
 *
 * Steps live in the query string rather than React state so a step is shareable,
 * directly openable, testable, and survives browser Back — all of which matter for
 * recording a walkthrough. Mode is pinned to demo throughout: the tour explains the
 * product using the synthetic dataset, and silently dropping to live would show
 * empty pages mid-tour.
 */
export default async function GuidedDemoPage({
  searchParams,
}: {
  searchParams: Promise<{ step?: string }>;
}) {
  const params = await searchParams;
  const parsed = Number(params.step);
  // Any invalid, missing or out-of-range step falls back to 1 rather than erroring.
  const step =
    Number.isInteger(parsed) && parsed >= 1 && parsed <= TOTAL_STEPS ? parsed : 1;
  const mode: DataMode = "demo";

  const [system, funnel, cs, backtest, track] = await Promise.all([
    apiGet<SystemInfo>(`/system${qs({ mode })}`),
    apiGet<FunnelStage[]>(`/opportunities/funnel${qs({ horizon: "24h", mode })}`),
    apiGet<CaseStudy | null>(`/case-study${qs({ mode, result: "featured" })}`),
    apiGet<BacktestRun[]>(`/backtest${qs({ mode })}`),
    apiGet<TrackRecordRow[]>(`/track-record${qs({ mode, limit: 500 })}`),
  ]);

  if (!system && !funnel && !cs && !backtest) return <ApiDown />;

  return (
    <div>
      <DemoBanner notice={(funnel?.demo_notice ?? backtest?.demo_notice) as string} />

      <div className="mb-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-xl font-semibold tracking-tight">Guided demo</h1>
          <Link
            href={`/${qs({ mode })}`}
            className="text-sm text-neutral-500 underline hover:text-neutral-900 dark:hover:text-neutral-100"
          >
            Exit guided demo
          </Link>
        </div>
        <Progress step={step} />
      </div>

      {step === 1 ? <StepScan system={system?.data} /> : null}
      {step === 2 ? <StepFilter stages={funnel?.data ?? []} conclusion={funnel?.conclusion as string} /> : null}
      {step === 3 ? <StepInspect cs={cs?.data ?? null} /> : null}
      {step === 4 ? <StepBacktest runs={backtest?.data ?? []} /> : null}
      {step === 5 ? <StepTrackRecord rows={track?.data ?? []} total={track?.total as number} /> : null}

      <Controls step={step} />
    </div>
  );
}

function Progress({ step }: { step: number }) {
  return (
    <div className="mt-3">
      <p className="text-xs uppercase tracking-wide text-neutral-500">
        Step {step} of {TOTAL_STEPS}
      </p>
      <ol className="mt-2 flex gap-1.5" aria-label="Progress">
        {Array.from({ length: TOTAL_STEPS }, (_, i) => i + 1).map((n) => (
          <li key={n} className="flex-1">
            <Link
              href={`/demo${qs({ step: n, mode: "demo" })}`}
              aria-label={`Go to step ${n}`}
              aria-current={n === step ? "step" : undefined}
              className={`block h-1.5 rounded-full transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ${
                n <= step
                  ? "bg-neutral-900 dark:bg-neutral-100"
                  : "bg-neutral-200 dark:bg-neutral-800"
              }`}
            />
          </li>
        ))}
      </ol>
    </div>
  );
}

function Controls({ step }: { step: number }) {
  return (
    <nav className="mt-6 flex flex-wrap items-center gap-3 border-t border-neutral-200 pt-4 dark:border-neutral-800">
      {step > 1 ? (
        <Link href={`/demo${qs({ step: step - 1, mode: "demo" })}`} className="rounded-lg border border-neutral-300 px-3 py-2 text-sm transition hover:bg-neutral-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 dark:border-neutral-700 dark:hover:bg-neutral-800">
          Previous
        </Link>
      ) : null}
      {step < TOTAL_STEPS ? (
        <Link href={`/demo${qs({ step: step + 1, mode: "demo" })}`} className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-neutral-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300">
          {NEXT_LABELS[step]}
        </Link>
      ) : null}
      <Link href={`/case-study${qs({ mode: "demo" })}`} className="text-sm underline">
        Skip to case study
      </Link>
      <Link href={`/${qs({ mode: "demo" })}`} className="text-sm text-neutral-500 underline">
        Exit
      </Link>
    </nav>
  );
}

const NEXT_LABELS: Record<number, string> = {
  1: "Next: See the filtering gates",
  2: "Next: Inspect one recommendation",
  3: "Next: Check the model's history",
  4: "Next: Audit every published call",
};

function Section({ title, lead, children }: { title: string; lead: string; children: React.ReactNode }) {
  return (
    <section className="card p-5">
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-600 dark:text-neutral-400">
        {lead}
      </p>
      <div className="mt-5">{children}</div>
    </section>
  );
}

function StepScan({ system }: { system?: SystemInfo }) {
  const counts = system?.row_counts ?? {};
  return (
    <Section
      title="Start with the full market universe"
      lead="PMVL begins with market discovery and executable order-book data. A market appearing in the database does not mean it is recommendable."
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <VerdictCard label="Markets tracked" value={count(counts.markets ?? 0)} sub="in this snapshot" />
        <VerdictCard label="Order books" value={count(counts.orderbook_snapshots ?? 0)} sub="captured" />
        <VerdictCard label="Model estimates" value={count(counts.model_predictions ?? 0)} sub="scored" />
        <VerdictCard
          label="Snapshot captured"
          value={system?.freshest_quote_observed_at ? localTime(system.freshest_quote_observed_at).split(",")[0] : "—"}
          sub="frozen, not live"
        />
      </div>
      <p className="mt-4 text-sm text-neutral-600 dark:text-neutral-400">
        Venues: {(system?.data_sources ?? []).filter((s) => /kalshi|polymarket/i.test(s.name)).map((s) => s.name).join(" · ") || "Kalshi · Polymarket"}
      </p>
    </Section>
  );
}

function StepFilter({ stages, conclusion }: { stages: FunnelStage[]; conclusion?: string }) {
  const widest = Math.max(...stages.map((s) => s.count), 1);
  return (
    <Section
      title="Why most markets are rejected"
      lead="Each gate removes markets for a stated reason: no tradeable book, no probability source independent of the market's own price, or no edge left once fees, slippage and capital costs are deducted."
    >
      <ol className="space-y-2">
        {stages.map((stage, i) => {
          const last = i === stages.length - 1;
          return (
            <li key={stage.label}>
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className={last ? "font-semibold" : ""}>{stage.label}</span>
                <span className="num shrink-0 font-mono font-semibold">{count(stage.count)}</span>
              </div>
              <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-800">
                <div
                  className={`h-full rounded-full ${last && stage.count === 0 ? "bg-neutral-400 dark:bg-neutral-600" : "bg-neutral-900 dark:bg-neutral-100"}`}
                  style={{ width: `${Math.max(2, (stage.count / widest) * 100)}%` }}
                />
              </div>
              <div className="mt-0.5 text-xs text-neutral-500">{stage.note}</div>
            </li>
          );
        })}
      </ol>
      <p className="mt-5 border-t border-neutral-200 pt-4 text-sm font-medium dark:border-neutral-800">
        Reporting zero opportunities is a feature, not a failure.{" "}
        <span className="font-normal text-neutral-600 dark:text-neutral-400">
          {conclusion} The most common rejection is the independence gate: a model
          estimate derived from the market&apos;s own price cannot demonstrate an edge
          against that price.
        </span>
      </p>
    </Section>
  );
}

function StepInspect({ cs }: { cs: CaseStudy | null }) {
  if (!cs) {
    return (
      <Section title="Inspect one recommendation" lead="No settled example is available in this dataset yet.">
        <Link href={`/case-study${qs({ mode: "demo" })}`} className="text-sm underline">
          Open the case study
        </Link>
      </Section>
    );
  }
  return (
    <Section
      title="Inspect one recommendation"
      lead="The same numbers the ranking engine used, read from the record frozen at publication. Costs are what turn a visible disagreement into a decision."
    >
      <h3 className="text-sm font-semibold">{cs.market.title}</h3>
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
        <VerdictCard label="Executable entry" value={cents(cs.execution.entry_vwap)} sub="VWAP at size" />
        <VerdictCard label="Market implied" value={prob(cs.probability.market_implied)} sub="what the book said" />
        <VerdictCard label="Model probability" value={prob(cs.probability.win_probability_for_side)} sub={`${cs.market.side.toUpperCase()} wins`} />
        <VerdictCard label="Conservative bound" value={prob(cs.probability.conservative_bound)} sub="used for the decision" />
        <VerdictCard label="All-in cost" value={cents(cs.costs.total_cost_per_contract)} sub={`${cents(cs.costs.cost_above_entry)} above entry`} />
        <VerdictCard
          label="Conservative net EV"
          value={cents(cs.decision.conservative_net_ev)}
          sub={cs.decision.qualified ? "qualified" : "rejected after costs"}
          tone={toneFor(Number(cs.decision.conservative_net_ev))}
          help={METRIC_HELP.conservative_net_ev}
        />
      </div>
      <p className="mt-4 text-sm font-medium">{cs.decision.verdict}</p>
      <Link href={`/case-study${qs({ mode: "demo" })}`} className="mt-3 inline-block text-sm underline">
        Open full case study
      </Link>
    </Section>
  );
}

function StepBacktest({ runs }: { runs: BacktestRun[] }) {
  const focus =
    runs.find((r) => r.strategy === "top10_equal_10usd" && r.n_settled > 0) ??
    runs.find((r) => r.n_settled > 0) ??
    runs[0];
  const m = focus?.metrics;
  const beat = m?.brier_improvement_vs_market;
  const beatsMarket = beat != null && beat > 0;

  return (
    <Section
      title="Check whether the model actually worked"
      lead="Two separate questions: did the strategy make money, and were its probabilities more accurate than the market's? They can disagree, and this dataset shows exactly that."
    >
      {focus ? (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <VerdictCard label="Net ROI" value={m?.roi != null ? `${m.roi > 0 ? "+" : ""}${(m.roi * 100).toFixed(1)}%` : "—"} sub={strategyLabel(focus.strategy)} tone={toneFor(m?.roi)} help={METRIC_HELP.roi} />
            <VerdictCard label="Beat the market?" value={beat == null ? "Unknown" : beatsMarket ? "Yes" : "No"} sub={beat != null ? `Brier ${beat > 0 ? "+" : ""}${beat.toFixed(5)}` : undefined} tone={toneFor(beat)} help={METRIC_HELP.vs_market} />
            <VerdictCard label="Settled trades" value={count(m?.n_settled ?? 0)} sub="sample size" help={METRIC_HELP.settled} />
            <VerdictCard label="Max drawdown" value={m?.max_drawdown != null ? usd(m.max_drawdown) : "—"} sub="worst stretch" tone={toneFor(Number(m?.max_drawdown ?? 0))} help={METRIC_HELP.max_drawdown} />
          </div>
          <p className="mt-4 rounded-lg border border-neutral-200 bg-neutral-50 p-3 text-sm dark:border-neutral-800 dark:bg-neutral-900">
            {m?.roi != null && m.roi > 0 && !beatsMarket ? (
              <>
                <span className="font-semibold">Positive ROI, but it did not beat the market.</span>{" "}
                The strategy made money while its probabilities were less accurate than
                the market&apos;s own. Positive ROI does not automatically mean the model
                beat market probabilities.
              </>
            ) : (
              <>Positive ROI does not automatically mean the model beat market probabilities. Both figures are reported separately above.</>
            )}
          </p>
        </>
      ) : (
        <p className="text-sm text-neutral-500">No settled backtest results yet.</p>
      )}
      <Link href={`/backtest${qs({ mode: "demo" })}`} className="mt-3 inline-block text-sm underline">
        Open the full backtest
      </Link>
    </Section>
  );
}

function StepTrackRecord({ rows, total }: { rows: TrackRecordRow[]; total?: number }) {
  const settled = rows.filter((r) => r.final_result);
  const winners = settled.filter((r) => Number(r.realized_profit_per_contract ?? 0) > 0);
  const losers = settled.filter((r) => Number(r.realized_profit_per_contract ?? 0) <= 0);

  return (
    <Section
      title="Audit every published call"
      lead="Recommendations are frozen when published. Entry price, probability, interval and evidence are never rewritten, and losing calls stay visible."
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <VerdictCard label="Published" value={count(total ?? rows.length)} sub="total recommendations" />
        <VerdictCard label="Settled" value={count(settled.length)} sub="graded" />
        <VerdictCard label="Winners" value={count(winners.length)} tone="text-edge dark:text-edge-dark" />
        <VerdictCard label="Losers" value={count(losers.length)} tone="text-risk dark:text-risk-dark" sub="cannot be hidden" />
      </div>
      <p className="mt-4 text-sm text-neutral-600 dark:text-neutral-400">
        Published recommendations cannot be removed from the track record simply
        because they lost.
      </p>
      <div className="mt-5 flex flex-wrap gap-3">
        <Link href={`/${qs({ mode: "demo" })}`} className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white dark:bg-neutral-100 dark:text-neutral-900">
          Explore demo opportunities
        </Link>
        <Link href={`/track-record${qs({ mode: "demo" })}`} className="rounded-lg border border-neutral-300 px-4 py-2 text-sm dark:border-neutral-700">
          View complete track record
        </Link>
      </div>
    </Section>
  );
}
