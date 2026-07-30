import Link from "next/link";
import { apiGet, qs, type CaseStudy, type DataMode } from "@/lib/api";
import { METRIC_HELP, cents, count, localTime, pct, prob, usd } from "@/lib/format";
import {
  ApiDown, DemoBanner, EmptyState, HelpDot, PageHeader, PlatformChip, RiskFlags,
  SideChip, toneFor,
} from "@/components/ui";

export const dynamic = "force-dynamic";

type Search = { mode?: DataMode; result?: string; snapshot_id?: string };

const RESULTS = [
  { key: "featured", label: "Featured example" },
  { key: "winner", label: "A winner" },
  { key: "loser", label: "A loser" },
] as const;

export default async function CaseStudyPage({
  searchParams,
}: {
  searchParams: Promise<Search>;
}) {
  const params = await searchParams;
  // The walkthrough only has settled examples in the demo dataset, so demo is the
  // sensible default here rather than an empty live page.
  const mode: DataMode = params.mode === "live" ? "live" : "demo";
  const result = RESULTS.some((r) => r.key === params.result)
    ? params.result!
    : "featured";

  const res = await apiGet<CaseStudy | null>(
    `/case-study${qs({ mode, result, snapshot_id: params.snapshot_id })}`,
  );
  if (!res) return <ApiDown />;

  const cs = res.data;

  return (
    <div>
      <PageHeader
        title="One recommendation, from price to settlement"
        subtitle="Every figure below is read from the record frozen when the recommendation was published — not recalculated now. Follow the six steps to see how a visible model–market disagreement survives, or does not survive, real trading costs."
      />
      <DemoBanner notice={res.demo_notice} />

      <nav aria-label="Choose an example" className="mb-6 flex flex-wrap gap-2">
        {RESULTS.map((r) => {
          const active = r.key === result;
          return (
            <Link
              key={r.key}
              href={`/case-study${qs({ mode, result: r.key })}`}
              aria-current={active ? "page" : undefined}
              className={`rounded-lg border px-3 py-2 text-sm transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ${
                active
                  ? "border-neutral-900 bg-neutral-900 text-white dark:border-line-subtle dark:bg-neutral-100 dark:text-neutral-900"
                  : "border-line hover:bg-neutral-50 dark:hover:bg-neutral-900"
              }`}
            >
              {r.label}
            </Link>
          );
        })}
      </nav>

      {!cs ? (
        <EmptyState
          title="No settled example available"
          body={(res.empty_reason as string) ?? ""}
          action={
            mode === "live" ? (
              <Link href={`/case-study${qs({ mode: "demo" })}`} className="text-sm underline">
                View the walkthrough on the demo dataset
              </Link>
            ) : undefined
          }
        />
      ) : (
        <div className="space-y-4">
          <Step n={1} title="The market">
            <h3 className="text-base font-semibold">{cs.market.title}</h3>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
              <PlatformChip platform={cs.market.platform} />
              <SideChip side={cs.market.side} />
              {cs.market.category ? (
                <span className="text-ink-faint">{cs.market.category}</span>
              ) : null}
            </div>
            <dl className="mt-4 grid gap-x-6 gap-y-2 sm:grid-cols-2">
              <Row label="Published" value={localTime(cs.market.published_at)} />
              <Row label="Expected resolution" value={localTime(cs.market.expected_resolution_time)} />
              <Row label="Actually settled" value={localTime(cs.market.settled_at)} />
              <Row label="Final result" value={cs.market.final_result?.toUpperCase() ?? "not settled"} />
            </dl>
            {cs.market.settlement_rules ? (
              <details className="mt-3">
                <summary className="cursor-pointer text-sm text-ink-muted">
                  Settlement rule
                </summary>
                <p className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-ink-muted">
                  {cs.market.settlement_rules.slice(0, 1200)}
                </p>
              </details>
            ) : null}
            <Note>
              This is synthetic demo data used to explain the research workflow. It is
              not a current trading recommendation.
            </Note>
          </Step>

          <Step n={2} title="What could actually be bought">
            <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
              <Row label="Best ask" value={cents(cs.execution.best_ask)} />
              <Row label="Reference order size" value={`${count(cs.execution.reference_size)} contracts`} />
              <Row label="Executable VWAP at that size" value={cents(cs.execution.entry_vwap)} strong />
              <Row label="Spread" value={cs.execution.spread ? cents(cs.execution.spread) : "—"} />
              <Row label="Depth available" value={cs.execution.depth_usd ? usd(cs.execution.depth_usd) : "—"} />
              <Row label="Quote captured" value={localTime(cs.execution.quote_observed_at)} />
            </dl>
            <Note>
              PMVL does not treat the last trade or midpoint as an executable entry. It
              prices the intended order against the available ask depth.
            </Note>
          </Step>

          <Step n={3} title="Independent probability estimate">
            <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
              <Row label="Market-implied probability" value={prob(cs.probability.market_implied)} />
              <Row
                label={`Model probability (${cs.market.side.toUpperCase()} wins)`}
                value={prob(cs.probability.win_probability_for_side)}
                strong
              />
              <Row
                label="Conservative bound used for the decision"
                value={prob(cs.probability.conservative_bound)}
                help={METRIC_HELP.conservative_bound}
              />
              <Row label="Model confidence" value={pct(cs.probability.model_confidence)} />
              <Row label="Model version" value={cs.probability.model_version} />
              <Row
                label="Independent prior"
                value={cs.probability.has_independent_prior ? "Yes" : "No"}
              />
            </dl>
            <p className="mt-3 text-xs text-ink-faint">
              Bound used: {cs.probability.conservative_bound_label}. For a NO
              recommendation the conservative case is the one where YES turns out more
              likely than estimated, so the bound is mirrored rather than reused.
            </p>
            <Note>
              The model can recommend a trade only when at least one meaningful
              probability input is independent of the target market&apos;s own price.
            </Note>
          </Step>

          <Step n={4} title="Real cost stack">
            <table className="w-full text-sm">
              <tbody className="divide-y divide-line-subtle">
                {cs.costs.components.map((c) => (
                  <tr key={c.key}>
                    <td className="py-1.5">{c.label}</td>
                    <td className="num py-1.5 text-right font-mono">
                      {/* A component that genuinely costs nothing is $0.00, not a dash. */}
                      {c.amount != null ? usd(c.amount) : "Not applicable"}
                    </td>
                  </tr>
                ))}
                <tr className="border-t-2 border-neutral-300 font-semibold dark:border-neutral-700">
                  <td className="py-2">All-in cost per contract</td>
                  <td className="num py-2 text-right font-mono">
                    {cents(cs.costs.total_cost_per_contract)}
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="mt-2 text-xs text-ink-faint">
              Costs added {cents(cs.costs.cost_above_entry)} on top of the entry price.
            </p>
            <Note>A visible model–market disagreement can disappear after trading costs.</Note>
          </Step>

          <Step n={5} title="The decision">
            <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
              <Row label="Raw model edge (before costs)" value={cents(cs.decision.raw_edge)} tone={toneFor(Number(cs.decision.raw_edge))} />
              <Row label="Net edge (after costs)" value={cents(cs.decision.net_edge)} tone={toneFor(Number(cs.decision.net_edge))} />
              <Row
                label="Conservative net EV"
                value={cents(cs.decision.conservative_net_ev)}
                tone={toneFor(Number(cs.decision.conservative_net_ev))}
                strong
                help={METRIC_HELP.conservative_net_ev}
              />
              <Row label="Net ROI" value={cs.decision.net_roi != null ? pct(cs.decision.net_roi) : "—"} />
              <Row label="Recommended position cap" value={cs.decision.position_cap != null ? `${count(cs.decision.position_cap)} contracts` : "—"} />
              <Row label="State" value={cs.decision.state ?? "—"} />
            </dl>
            {cs.decision.risk_flags.length ? (
              <div className="mt-3">
                <RiskFlags flags={cs.decision.risk_flags} />
              </div>
            ) : null}
            <p
              className={`mt-4 rounded-lg border p-3 text-sm font-medium ${
                cs.decision.qualified
                  ? "border-edge/40 bg-edge/10 text-edge"
                  : "border-neutral-300 bg-neutral-50 dark:border-neutral-700 dark:bg-neutral-900"
              }`}
            >
              {cs.decision.verdict}
            </p>
            <Note>Model disagreement alone is not a recommendation.</Note>
          </Step>

          <Step n={6} title="What happened">
            {cs.outcome.settled ? (
              <>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Verdict
                    label="Did the trade make money?"
                    value={cs.outcome.trade_won ? "Yes" : "No"}
                    sub={
                      cs.outcome.realized_profit_per_contract != null
                        ? `${cents(cs.outcome.realized_profit_per_contract)} per contract`
                        : undefined
                    }
                    tone={toneFor(Number(cs.outcome.realized_profit_per_contract ?? 0))}
                  />
                  <Verdict
                    label="Was the forecast better than the market?"
                    value={
                      cs.outcome.forecast_beat_market == null
                        ? "Unknown"
                        : cs.outcome.forecast_beat_market
                          ? "Yes"
                          : "No"
                    }
                    sub={
                      cs.outcome.model_brier != null && cs.outcome.market_brier != null
                        ? `Brier ${cs.outcome.model_brier} vs market ${cs.outcome.market_brier}`
                        : undefined
                    }
                    tone={
                      cs.outcome.forecast_beat_market == null
                        ? "text-ink-faint"
                        : cs.outcome.forecast_beat_market
                          ? "text-edge"
                          : "text-risk"
                    }
                    help={METRIC_HELP.brier}
                  />
                </div>
                <dl className="mt-4 grid gap-x-6 gap-y-2 sm:grid-cols-2">
                  <Row label="Settlement" value={cs.outcome.final_result?.toUpperCase() ?? "—"} />
                  <Row
                    label="Realized on $100"
                    value={cs.outcome.realized_profit_at_100_usd != null ? usd(cs.outcome.realized_profit_at_100_usd) : "—"}
                    tone={toneFor(Number(cs.outcome.realized_profit_at_100_usd ?? 0))}
                  />
                </dl>
                <p className="mt-4 rounded-lg border border-line bg-neutral-50 p-3 text-sm dark:bg-neutral-900">
                  {cs.outcome.summary}
                </p>
              </>
            ) : (
              <p className="text-sm text-ink-muted">
                This recommendation has not settled yet.
              </p>
            )}
          </Step>

          <div className="flex flex-wrap gap-3 pt-2">
            <Link href={`/track-record${qs({ mode })}`} className="text-sm underline">
              View in full track record
            </Link>
            <Link href={`/${qs({ mode })}`} className="text-sm underline">
              Back to demo opportunities
            </Link>
          </div>

          <p className="pt-2 text-xs text-ink-faint">{res.audit_note as string}</p>
        </div>
      )}
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="panel p-5">
      <div className="mb-3 flex items-center gap-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-neutral-900 font-mono text-xs font-semibold text-white dark:bg-neutral-100 dark:text-neutral-900">
          {n}
        </span>
        <h2 className="text-sm font-semibold uppercase tracking-wide">{title}</h2>
      </div>
      {children}
    </section>
  );
}

function Row({
  label, value, strong, tone, help,
}: {
  label: string; value: string; strong?: boolean; tone?: string; help?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line-subtle py-1.5">
      <dt className="flex items-center text-sm text-ink-muted">
        {label}
        {help ? <HelpDot text={help} /> : null}
      </dt>
      <dd className={`num shrink-0 font-mono text-sm ${strong ? "font-semibold" : ""} ${tone ?? ""}`}>
        {value}
      </dd>
    </div>
  );
}

function Verdict({
  label, value, sub, tone, help,
}: {
  label: string; value: string; sub?: string; tone?: string; help?: string;
}) {
  return (
    <div className="rounded-lg border border-line p-3">
      <div className="flex items-center text-xs uppercase tracking-wide text-ink-faint">
        {label}
        {help ? <HelpDot text={help} /> : null}
      </div>
      <div className={`mt-1 font-mono text-xl font-semibold ${tone ?? ""}`}>{value}</div>
      {sub ? <div className="mt-0.5 text-xs text-ink-faint">{sub}</div> : null}
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-3 border-l-2 border-neutral-300 pl-3 text-sm text-neutral-600 dark:border-neutral-700 dark:text-ink-faint">
      {children}
    </p>
  );
}
