import { apiGet } from "@/lib/api";
import { ApiDown, PageHeader } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function MethodologyPage() {
  const res = await apiGet<any>("/methodology");
  if (!res) return <ApiDown />;
  const m = res.data;

  return (
    <div className="max-w-4xl">
      <PageHeader
        title="Methodology"
        subtitle={`The formulas and decision rules the platform actually uses. Model version ${m.model_version}.`}
      />

      <Section title="Executable price">
        <p className="mb-2">{m.executable_price.rule}</p>
        <ul className="list-disc space-y-1 pl-5">
          <li>{m.executable_price.yes_side}</li>
          <li>{m.executable_price.no_side}</li>
          <li><strong>Kalshi:</strong> {m.executable_price.kalshi_note}</li>
          <li><strong>Polymarket:</strong> {m.executable_price.polymarket_note}</li>
        </ul>
      </Section>

      <Section title="Fees">
        <Formula label="Kalshi taker" value={m.fees.kalshi_taker} />
        <Formula label="Kalshi maker" value={m.fees.kalshi_maker} />
        <Formula label="Polymarket taker" value={m.fees.polymarket_taker} />
        <Formula label="Polymarket maker" value={m.fees.polymarket_maker} />
        <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">{m.fees.note}</p>
      </Section>

      <Section title="Cost stack">
        <p className="mb-2">Every component between a quoted ask and true all-in cost:</p>
        <ol className="list-decimal space-y-1 pl-5">
          {m.cost_stack.map((c: string, i: number) => <li key={i}>{c}</li>)}
        </ol>
      </Section>

      <Section title="Value calculation">
        <Formula label="Gross" value={m.value.gross_expected_profit_per_contract} />
        <Formula label="Net EV" value={m.value.net_ev_per_contract} />
        <Formula label="Conservative" value={m.value.conservative_net_ev} />
        <p className="mt-3"><strong>Admission rule.</strong> {m.value.admission_rule}</p>
        <p className="mt-2"><strong>NO-side bound.</strong> {m.value.no_side_bound}</p>
        <p className="mt-2"><strong>Ranking.</strong> {m.value.ranking}</p>
      </Section>

      <Section title="Fair probability">
        <p className="mb-2"><strong>Combination:</strong> {m.probability.combination}</p>
        <div className="my-3 rounded border-2 border-neutral-300 p-3 dark:border-neutral-700">
          <p className="font-semibold">The independence rule</p>
          <p className="mt-1 text-sm">{m.probability.independence_rule}</p>
        </div>
        <p className="font-medium">Components independent of the target market:</p>
        <ul className="mb-3 list-disc space-y-1 pl-5">
          {m.probability.independent_components.map((c: string, i: number) => <li key={i}>{c}</li>)}
        </ul>
        <p className="font-medium">Components that are NOT independent:</p>
        <ul className="mb-3 list-disc space-y-1 pl-5">
          {m.probability.non_independent_components.map((c: string, i: number) => <li key={i}>{c}</li>)}
        </ul>
        <p className="mb-2"><strong>Unmodelled categories.</strong> {m.probability.unmodelled_categories}</p>
        <p><strong>Interval.</strong> {m.probability.interval}</p>
      </Section>

      <Section title="Arbitrage">
        <p className="mb-2 font-medium">
          &quot;Executable&quot; is claimed only when every one of these holds:
        </p>
        <ul className="mb-3 list-disc space-y-1 pl-5">
          {m.arbitrage.executable_definition.map((c: string, i: number) => <li key={i}>{c}</li>)}
        </ul>
        <p className="mb-2">{m.arbitrage.other_labels}</p>
        <p className="mb-2"><strong>Multi-outcome guard.</strong> {m.arbitrage.multi_outcome_guard}</p>
        <p><strong>Logical constraints.</strong> {m.arbitrage.logical_constraints}</p>
      </Section>

      <Section title="Backtest">
        <p className="mb-2"><strong>Look-ahead prevention.</strong> {m.backtest.look_ahead_prevention}</p>
        <p className="mb-2"><strong>Data quality.</strong> {m.backtest.data_quality}</p>
        <p><strong>Benchmark.</strong> {m.backtest.benchmark}</p>
      </Section>

      <Section title="Known limitations">
        <ul className="list-disc space-y-1 pl-5">
          {m.limitations.map((c: string, i: number) => <li key={i}>{c}</li>)}
        </ul>
      </Section>

      <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">{res.disclaimer}</p>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="card mb-4 p-5">
      <h2 className="mb-3 text-base font-semibold">{title}</h2>
      <div className="space-y-1 text-sm text-neutral-700 dark:text-neutral-300">{children}</div>
    </section>
  );
}

function Formula({ label, value }: { label: string; value: string }) {
  return (
    <div className="mb-1.5">
      <span className="metric-label">{label}</span>
      <pre className="num mt-0.5 overflow-x-auto rounded bg-neutral-100 px-3 py-2 text-xs dark:bg-neutral-800">
        {value}
      </pre>
    </div>
  );
}
