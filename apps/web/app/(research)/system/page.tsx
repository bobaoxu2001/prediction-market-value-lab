import { apiGet, type SystemInfo } from "@/lib/api";
import { safeExternalUrl } from "@/lib/safe-url";
import { localTime, utcTime } from "@/lib/format";
import { ApiDown, Metric, PageHeader } from "@/components/ui";

export const dynamic = "force-dynamic";

interface Eligibility {
  region: string | null;
  research_access: string;
  trading_execution_available: boolean;
  trading_execution_reason: string;
  would_be_restricted_for_trading: boolean;
  restricted_regions_configured: string[];
  note: string;
}

export default async function SystemPage() {
  const [res, eligibilityRes] = await Promise.all([
    apiGet<SystemInfo>("/system"),
    apiGet<Eligibility>("/system/eligibility"),
  ]);
  // /system/eligibility returns a bare object, not an envelope.
  const eligibility = (eligibilityRes as unknown as Eligibility) ?? null;
  if (!res) return <ApiDown />;
  const s = res.data;

  // Available to the server render on Vercel; absent locally, where "—" is honest.
  const short = (sha: string | undefined | null) => (sha ? sha.slice(0, 12) : "—");
  const webCommit = short(process.env.VERCEL_GIT_COMMIT_SHA);
  const webRef = process.env.VERCEL_GIT_COMMIT_REF ?? "—";
  const apiCommit = short(s.deployment?.commit_sha);
  const pipeline = s.pipeline ?? null;

  return (
    <div>
      <PageHeader
        title="System"
        subtitle="Data sources, job health and update cadences. A job that has silently stopped running shows here rather than being mistaken for 'no opportunities today'."
      />

      {/* The web bundle's OWN commit, read from the build environment rather than
          from the API. Without it the two projects can drift - the web app once
          served a commit two merges behind the API with nothing on the page
          saying so - and comparing these two rows is how you catch it. */}
      <section className="panel mb-4 border-l-2 border-l-accent p-4">
        <h2 className="t-section-title mb-3">Deployment parity</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Metric label="Web commit" value={webCommit} />
          <Metric label="Web branch" value={webRef} />
          <Metric label="API commit" value={apiCommit} />
          <Metric
            label="Web / API match"
            value={
              webCommit === "—" || apiCommit === "—"
                ? "unknown"
                : webCommit === apiCommit
                  ? "same commit"
                  : "DIFFERENT"
            }
            tone={
              webCommit !== "—" && apiCommit !== "—" && webCommit !== apiCommit
                ? "warn"
                : "neutral"
            }
            hint="The frontend and the API are deployed separately and can drift apart."
          />
        </div>
      </section>

      <section className="panel mb-4 p-4">
        <h2 className="t-section-title mb-3">Status</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Metric label="Environment" value={s.environment} />
          <Metric label="Model version" value={s.model_version} />
          <Metric label="Latest captured quote" value={utcTime(s.freshest_quote_observed_at)} />
          <Metric
            label="Trading execution"
            value={s.trading_execution_enabled ? "enabled" : "disabled"}
            tone={s.trading_execution_enabled ? "warn" : "neutral"}
            hint="This release has no execution service at all."
          />
        </div>
      </section>

      {s.snapshot_timing && (
        <section className="panel mb-4 p-4">
          <h2 className="t-section-title mb-1">Snapshot timing</h2>
          {/* These were previously collapsed into one "snapshot timestamp", which
              reported the single freshest observation as though every quote had
              been captured then. They are hours to weeks apart, so each one is
              named for the question it answers. */}
          <p className="mb-3 max-w-3xl text-xs text-ink-muted">
            {s.snapshot_timing.note}
          </p>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            <Metric label="Ingest started" value={utcTime(s.snapshot_timing.market_ingest_started_at)} />
            <Metric label="Ingest finished" value={utcTime(s.snapshot_timing.market_ingest_finished_at)} />
            <Metric label="Arbitrage scan" value={utcTime(s.snapshot_timing.arbitrage_scan_at)} />
            <Metric label="Latest captured quote" value={utcTime(s.snapshot_timing.freshest_quote_observed_at)} />
            <Metric label="Median captured quote" value={utcTime(s.snapshot_timing.median_quote_observed_at)} />
            <Metric label="Oldest captured quote" value={utcTime(s.snapshot_timing.oldest_quote_observed_at)} />
          </div>
          {/* Reported as absent with a reason rather than approximated from a
              value that happens to be recorded. */}
          <dl className="mt-4 space-y-1 border-t border-line-subtle pt-3 t-meta">
            {Object.entries(s.snapshot_timing.unavailable ?? {}).map(([field, why]) => (
              <div key={field} className="flex flex-wrap gap-x-2">
                <dt className="font-mono text-ink-muted">{field}</dt>
                <dd>not recorded — {why}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      <section className="panel mb-4 p-4">
        <h2 className="t-section-title mb-3">Job health</h2>
        {s.jobs?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr><th scope="col">Job</th><th scope="col">Status</th><th scope="col">Last run</th><th scope="col" className="num">Duration</th><th scope="col" className="num">Records</th><th scope="col">Error</th></tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {s.jobs.map((j) => (
                  <tr key={j.job_name}>
                    <td className="font-medium">{j.job_name}</td>
                    <td>
                      <span className={`chip ${j.status === "success" ? "bg-edge/15 text-edge" : j.status === "failed" ? "bg-risk/15 text-risk" : "bg-sunken text-ink-muted"}`}>
                        {j.status}
                      </span>
                    </td>
                    <td className="text-ink-faint">{localTime(j.started_at)}</td>
                    <td className="num">{j.duration_seconds != null ? `${j.duration_seconds.toFixed(1)}s` : "—"}</td>
                    <td className="num">{j.records_written}</td>
                    <td className="max-w-md whitespace-normal text-xs text-risk">{j.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-faint">
            No jobs have run yet. Start with <code>make ingest</code>.
          </p>
        )}
      </section>

      {/* min-w-0: grid items default to `min-width: auto`, so the wide row-count
          and cadence tables stretched their tracks instead of scrolling inside
          their own containers. */}
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="block min-w-0">
          <h2 className="t-section-title mb-3">Row counts</h2>
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr>
                  <th scope="col">Table</th>
                  <th scope="col" className="num">Rows</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {Object.entries(s.row_counts).map(([table, count]) => (
                  <tr key={table}>
                    <td className="font-mono text-xs text-ink-muted">{table}</td>
                    <td className="num">{count.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3 className="t-label mt-5">Live vs demo</h3>
          <div className="table-wrap mt-1">
            <table className="w-full">
              <thead className="border-b border-line"><tr><th scope="col">Table</th><th scope="col" className="num">Live</th><th scope="col" className="num">Demo</th></tr></thead>
              <tbody className="divide-y divide-line-subtle">
                {Object.entries(s.provenance_split).map(([table, split]) => (
                  <tr key={table}>
                    <td>{table}</td>
                    <td className="num">{split.live ?? 0}</td>
                    <td className="num">{split.demo ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* This was a bare list of intervals - "arbitrage scan: 1 minute" - on a
            deployment with no worker and a frozen database. Every number described
            scheduler.py correctly and the running system not at all. The caveat now
            precedes the table, and each row shows whether that job is actually
            running rather than only how often it is configured to. */}
        <section className="block min-w-0">
          <h2 className="t-section-title mb-1">Worker cadence</h2>
          {pipeline?.cadence_notice ? (
            <p className="mb-3 rounded-[2px] border border-stale/50 bg-stale/10 px-2 py-1.5 text-xs text-stale">
              {pipeline.cadence_notice}
            </p>
          ) : (
            <p className="mb-3 text-xs text-ink-faint">
              Scheduler {pipeline?.scheduler_status ?? "unknown"}.
            </p>
          )}
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-line">
                <tr>
                  <th scope="col">Job</th>
                  <th scope="col" className="num">Configured</th>
                  <th scope="col" className="num">Active</th>
                  <th scope="col" className="num">Last success</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {(pipeline?.jobs ?? []).map((j) => (
                  <tr key={j.job_name}>
                    <td title={j.description}>{j.job_name.replace(/_/g, " ")}</td>
                    <td className="num text-right text-ink-faint">
                      {j.configured_cadence}
                    </td>
                    <td className="num text-right">
                      {/* A dash here is the honest answer, not missing data: the
                          job is configured but nothing is executing it. */}
                      {j.active_cadence ?? (
                        <span className="text-ink-faint">
                          {j.scheduler_status === "not_deployed" ? "not deployed" : "—"}
                        </span>
                      )}
                    </td>
                    <td className="num text-right text-ink-faint">
                      {j.last_success_at ? utcTime(j.last_success_at) : "never"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section className="block mt-6">
        <h2 className="t-section-title mb-3">Data sources</h2>
        <div className="table-wrap">
          <table className="w-full">
            <thead className="border-b border-line">
              <tr><th scope="col">Source</th><th scope="col">Auth</th><th scope="col">Used for</th><th scope="col">Docs</th></tr>
            </thead>
            <tbody className="divide-y divide-line-subtle">
              {s.data_sources.map((src) => (
                <tr key={src.name}>
                  <td className="font-medium">{src.name}</td>
                  <td>
                    {src.auth_required ? (
                      <span className="chip bg-sunken text-ink-muted">
                        {src.configured ? (src.enabled ? "enabled" : "configured, disabled") : "not configured"}
                      </span>
                    ) : (
                      <span className="chip bg-edge/15 text-edge">
                        public
                      </span>
                    )}
                  </td>
                  <td className="max-w-md whitespace-normal text-xs text-ink-muted">
                    {src.used_for}
                  </td>
                  <td>
                    {(() => {
                      const docsUrl = safeExternalUrl(src.docs);
                      return docsUrl ? (
                        <a href={docsUrl} target="_blank" rel="noopener noreferrer" className="text-xs underline">
                          docs
                        </a>
                      ) : (
                        <span className="text-xs text-ink-faint">docs unavailable</span>
                      );
                    })()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {eligibility && (
        <section className="block mt-6">
          <h2 className="t-section-title mb-2">Eligibility &amp; compliance</h2>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric label="Research access" value={eligibility.research_access} />
            <Metric label="Trading execution" value={String(eligibility.trading_execution_available)} />
            <Metric label="Restricted regions configured" value={(eligibility.restricted_regions_configured ?? []).length} />
          </div>
          <p className="mt-3 text-xs text-ink-muted">
            {eligibility.trading_execution_reason}
          </p>
          <p className="mt-2 text-xs text-ink-muted">
            {eligibility.note}
          </p>
        </section>
      )}

      <p className="mt-6 t-meta">{res.disclaimer}</p>
    </div>
  );
}
