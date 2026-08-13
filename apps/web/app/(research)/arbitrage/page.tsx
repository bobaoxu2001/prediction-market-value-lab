import Link from "next/link";
import { apiGet, qs, type ArbitrageOpportunity, type DataMode } from "@/lib/api";
import { cents, compactUsd, displayTitle, localTime, pct, relativeToSnapshot, usd } from "@/lib/format";
import { withResearchMode } from "@/lib/research-mode";
import {
  ApiDown,
  ArbLabelChip,
  DemoBanner,
  EmptyState,
  Metric,
  PageHeader,
  PlatformChip,
} from "@/components/ui";

export const dynamic = "force-dynamic";

/*
 * Actionable and Diagnostics are rendered by two different components on purpose.
 *
 * They previously shared one card, one chip set, one metric grid and one leg
 * table, so a stale quote or a rule mismatch - findings that are explicitly not
 * tradeable - looked exactly like a validated executable basket. On a page whose
 * entire job is to say which relationships survived verification, that is the
 * one visual equivalence that must not exist.
 *
 * Actionable is enclosed and substantial: a framed panel, an edge-toned rule, the
 * money foregrounded. Diagnostics is deliberately unenclosed: rule-separated
 * entries on a sunken ground, the blocking reason foregrounded and the money
 * demoted to muted text, because those figures describe a basket nobody can fill.
 */

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
  // Anchor relative times to the capture instant. On a frozen deployment now()
  // advances while the data does not, so a market with hours left at capture drifts
  // into "resolved 12h ago" while still listed as upcoming.
  const system = await apiGet<{
    snapshot_mode?: boolean;
    freshest_quote_observed_at?: string | null;
  }>("/system");
  const snapshotAt = system?.data?.snapshot_mode
    ? (system?.data?.freshest_quote_observed_at ?? null)
    : null;

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

      <ViewSwitch mode={mode} view={view} />

      {view === "actionable" ? (
        <ActionableView
          rows={rows}
          mode={mode}
          meanings={meanings}
          counts={counts}
          emptyReason={res.empty_reason as string | undefined}
          snapshotAt={snapshotAt}
        />
      ) : (
        <DiagnosticsView
          rows={rows}
          mode={mode}
          diagnostics={diagnostics}
          meanings={meanings}
          counts={counts}
          snapshotAt={snapshotAt}
        />
      )}

      <p className="mt-6 t-meta">{res.disclaimer}</p>
    </div>
  );
}

/**
 * A segmented control, not the filter pills used elsewhere.
 *
 * These two views do not narrow a list, they change what the rows on it claim.
 * Reusing the horizon-filter pill styling made switching read as filtering.
 */
function ViewSwitch({ mode, view }: { mode: DataMode; view: string }) {
  const items = [
    ["actionable", "Actionable"],
    ["diagnostics", "Diagnostics"],
  ] as const;
  return (
    <div className="mb-4">
      <div className="seg" role="group" aria-label="Arbitrage view">
        {items.map(([key, label]) => (
          <Link
            key={key}
            href={withResearchMode(`/arbitrage${qs({ view: key })}`, mode)}
            aria-current={view === key ? "page" : undefined}
            className={`seg-item ${view === key ? "seg-item-on" : "hover:text-ink"}`}
          >
            {label}
          </Link>
        ))}
      </div>
      {/* Status in text, not only in the selected-pill colour. */}
      <p className="mt-2 t-prose">
        {view === "actionable" ? (
          <>
            <span className="font-semibold text-ink">Executable claims only.</span>{" "}
            Every gate cleared: an exact settlement-rule match, fillable depth on
            every leg, and a net edge above the required safety margin after all
            costs.
          </>
        ) : (
          <>
            <span className="font-semibold text-ink">
              Research findings — not executable.
            </span>{" "}
            A stale quote, a rule mismatch or a logical inconsistency is worth
            recording. None of these is something to act on, and the figures shown
            describe a basket that cannot currently be filled.
          </>
        )}
      </p>
    </div>
  );
}

/** The label legend. Subordinate on Actionable, primary on Diagnostics. */
function LabelLegend({
  meanings,
  counts,
  tone,
}: {
  meanings: Record<string, string>;
  counts: Record<string, number>;
  tone: "quiet" | "prominent";
}) {
  if (!Object.keys(meanings).length) return null;
  const body = (
    <dl className="grid gap-2 sm:grid-cols-2">
      {Object.entries(meanings).map(([label, meaning]) => (
        <div key={label} className="flex gap-2">
          <dt className="shrink-0">
            <ArbLabelChip label={label} />
          </dt>
          <dd className="t-meta">
            {meaning}
            {counts[label] ? (
              <span className="ml-1 font-semibold text-ink-muted">
                ({counts[label]} now)
              </span>
            ) : null}
          </dd>
        </div>
      ))}
    </dl>
  );

  if (tone === "quiet") {
    // Below the results and unenclosed: reference material, not a finding.
    return (
      <details className="mt-6 block">
        <summary className="cursor-pointer t-label">What each label means</summary>
        <div className="mt-3">{body}</div>
      </details>
    );
  }
  return (
    <section className="panel mb-4 p-4">
      <h2 className="t-section-title mb-3">What each label means</h2>
      {body}
    </section>
  );
}

function ActionableView({
  rows,
  mode,
  meanings,
  counts,
  emptyReason,
  snapshotAt,
}: {
  rows: ArbitrageOpportunity[];
  mode: DataMode;
  meanings: Record<string, string>;
  counts: Record<string, number>;
  emptyReason?: string;
  snapshotAt: string | null;
}) {
  return (
    <>
      {rows.length === 0 ? (
        <EmptyState
          title="No executable arbitrage in the latest scan"
          body={
            emptyReason ??
            "Finding nothing is the normal result. Both venues are actively arbitraged, and this scanner refuses to label anything executable unless every leg is fillable after all fees, slippage and capital costs."
          }
          action={
            <Link
              href={withResearchMode(
                `/arbitrage${qs({ view: "diagnostics" })}`,
                mode,
              )}
              className="text-sm underline"
            >
              See what was examined and why it was rejected
            </Link>
          }
        />
      ) : (
        <div className="space-y-4">
          {rows.map((a) => (
            <article
              key={a.id}
              className="panel border-l-2 border-l-edge p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <ArbLabelChip label={a.label} />
                    <span className="chip bg-sunken text-ink-muted">
                      {a.kind.replace(/_/g, " ")}
                    </span>
                    <span className="chip bg-edge/15 text-edge">
                      rules: {a.rule_compatibility}
                    </span>
                  </div>
                  <p className="mt-2 t-sub-title">{displayTitle(a.title)}</p>
                </div>
                <div className="text-right">
                  <div className="metric-label">Net profit / set</div>
                  <div className="num text-xl font-bold text-edge">
                    {cents(a.net_profit_per_set)}
                  </div>
                </div>
              </div>

              <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
                <Metric label="Gross edge / set" value={cents(a.gross_edge_per_set)} />
                <Metric label="All-in cost / set" value={cents(a.total_cost_per_set)} />
                <Metric label="Executable sets" value={Number(a.max_executable_sets).toFixed(0)} />
                <Metric label="Max net profit" value={usd(a.max_net_profit)} />
                <Metric label="Capital required" value={compactUsd(a.capital_required)} />
                <Metric label="Net ROI" value={pct(a.net_roi)} />
              </div>

              <LegTable legs={a.legs} mode={mode} />

              {a.risk_flags?.length > 0 && <RiskNotes flags={a.risk_flags} />}

              <div className="mt-3 border-t border-line-subtle pt-2 t-meta">
                Quote age {a.quote_age_seconds ?? "—"}s · resolves{" "}
                {relativeToSnapshot(a.expected_resolution_time, snapshotAt)} · scanned{" "}
                {localTime(a.created_at)}
              </div>
            </article>
          ))}
        </div>
      )}

      <LabelLegend meanings={meanings} counts={counts} tone="quiet" />
    </>
  );
}

function DiagnosticsView({
  rows,
  mode,
  diagnostics,
  meanings,
  counts,
  snapshotAt,
}: {
  rows: ArbitrageOpportunity[];
  mode: DataMode;
  diagnostics:
    | {
        pairs_examined?: number;
        verified_equivalent?: number;
        blocked_only_by_missing_info?: number;
        top_reasons?: { code: string; count: number; kind: string }[];
        diagnosis?: string;
      }
    | null
    | undefined;
  meanings: Record<string, string>;
  counts: Record<string, number>;
  snapshotAt: string | null;
}) {
  return (
    <>
      {diagnostics?.pairs_examined ? (
        <section className="panel mb-4 p-4">
          <h2 className="t-section-title">Why no cross-platform pair qualified</h2>
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
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
            // `table-wrap`, like every other table on the site. Without it this
            // one scrolled the whole document sideways below 380px: its cells
            // do not wrap, so its min-content width exceeded a phone viewport
            // and the overflow escaped to the root instead of staying inside a
            // scroller. Measured at 413px against a 375px viewport.
            <div className="table-wrap mt-4">
              <table className="w-full">
                <caption className="sr-only">
                  Most frequent reasons a pair was rejected
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Reason</th>
                    <th scope="col">Kind</th>
                    <th scope="col" className="num">
                      Count
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line-subtle">
                  {diagnostics.top_reasons.slice(0, 6).map((r) => (
                    <tr key={r.code}>
                      <td className="text-ink-muted">{r.code.replace(/_/g, " ")}</td>
                      <td className="t-meta">{r.kind.replace(/_/g, " ")}</td>
                      <td className="num">{r.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {diagnostics.diagnosis ? (
            <p className="mt-4 border-t border-line-subtle pt-3 text-sm text-ink">
              {diagnostics.diagnosis}
            </p>
          ) : null}
        </section>
      ) : null}

      <LabelLegend meanings={meanings} counts={counts} tone="prominent" />

      {rows.length === 0 ? (
        <EmptyState
          title="Nothing recorded in the latest scan"
          body="No relationship reached even the diagnostic threshold. This is a normal outcome, not a failure of the scan."
        />
      ) : (
        <section>
          <h2 className="t-section-title mb-1">Findings</h2>
          <p className="t-prose mb-3">
            Recorded because the relationship is interesting, not because it can be
            traded. Figures are shown in muted type to keep that distinction visible.
          </p>
          {/*
           * Unenclosed rows on a sunken ground, separated by rules. The visual
           * weight of a framed panel is reserved for a claim that survived
           * verification.
           */}
          <ul className="divide-y divide-line border-y border-line">
            {rows.map((a) => (
              <li
                key={a.id}
                className="border-l-2 border-l-stale bg-sunken/60 px-4 py-3"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <ArbLabelChip label={a.label} />
                      <span className="chip bg-transparent text-ink-faint ring-1 ring-inset ring-line">
                        {a.kind.replace(/_/g, " ")}
                      </span>
                      <span className="chip bg-stale/15 text-stale">
                        rules: {a.rule_compatibility}
                      </span>
                      <span className="t-label">not executable</span>
                    </div>
                    <p className="mt-2 text-sm font-medium text-ink">
                      {displayTitle(a.title)}
                    </p>
                  </div>
                  <div className="text-right">
                    <div className="metric-label">Indicative / set</div>
                    {/* Muted, not the edge tone: this is not money anyone can take. */}
                    <div className="num text-base font-semibold text-ink-muted">
                      {cents(a.net_profit_per_set)}
                    </div>
                  </div>
                </div>

                <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 t-meta">
                  <div>
                    <dt className="inline">Gross edge </dt>
                    <dd className="num inline text-ink-muted">
                      {cents(a.gross_edge_per_set)}
                    </dd>
                  </div>
                  <div>
                    <dt className="inline">All-in cost </dt>
                    <dd className="num inline text-ink-muted">
                      {cents(a.total_cost_per_set)}
                    </dd>
                  </div>
                  <div>
                    <dt className="inline">Sets </dt>
                    <dd className="num inline text-ink-muted">
                      {Number(a.max_executable_sets).toFixed(0)}
                    </dd>
                  </div>
                  <div>
                    <dt className="inline">Quote age </dt>
                    <dd className="num inline text-ink-muted">
                      {a.quote_age_seconds ?? "—"}s
                    </dd>
                  </div>
                  <div>
                    <dt className="inline">Resolves </dt>
                    <dd className="inline text-ink-muted">
                      {relativeToSnapshot(a.expected_resolution_time, snapshotAt)}
                    </dd>
                  </div>
                </dl>

                {a.risk_flags?.length > 0 && <RiskNotes flags={a.risk_flags} />}

                <details className="mt-2">
                  <summary className="cursor-pointer t-label">
                    Legs ({a.legs.length})
                  </summary>
                  <LegTable legs={a.legs} mode={mode} />
                </details>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function LegTable({
  legs,
  mode,
}: {
  legs: ArbitrageOpportunity["legs"];
  mode: DataMode;
}) {
  return (
    <div className="table-wrap mt-3">
      <table className="w-full">
        <caption className="sr-only">Basket legs</caption>
        <thead className="border-b border-line">
          <tr>
            <th scope="col" className="col-title">
              Leg
            </th>
            <th scope="col">Venue</th>
            <th scope="col">Side</th>
            <th scope="col" className="num">
              Price
            </th>
            <th scope="col" className="num">
              Size available
            </th>
            <th scope="col" className="num">
              Fee / ct
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line-subtle">
          {legs.map((leg, i) => (
            <tr key={`${leg.platform_market_id}-${leg.side}-${i}`}>
              <td className="col-title">
                {leg.market_id ? (
                  <Link
                    href={withResearchMode(`/market/${leg.market_id}`, mode)}
                    className="hover:underline"
                  >
                    {leg.title || leg.platform_market_id}
                  </Link>
                ) : (
                  leg.title || leg.platform_market_id
                )}
              </td>
              <td>
                <PlatformChip platform={leg.platform} />
              </td>
              <td className="uppercase">{leg.side}</td>
              <td className="num">{cents(leg.price)}</td>
              <td className="num">{Number(leg.size_available).toFixed(0)}</td>
              <td className="num">{cents(leg.fee_per_contract)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/*
 * Arbitrage scanners emit full sentences here, not the enum codes the RiskFlags
 * chips are built for - a sentence in a pill wraps badly and its tooltip would
 * just repeat itself. A list is the right container for this shape of text.
 */
function RiskNotes({ flags }: { flags: string[] }) {
  return (
    <ul className="mt-3 space-y-1 t-meta">
      {flags.map((flag, i) => (
        <li key={i} className="flex gap-2">
          {/* Marked in text as well as colour. */}
          <span className="shrink-0 font-semibold text-warn" aria-hidden>
            !
          </span>
          <span className="sr-only">Warning:</span>
          <span className="text-ink-muted">{flag}</span>
        </li>
      ))}
    </ul>
  );
}
