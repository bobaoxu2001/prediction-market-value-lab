import { apiGet, type SystemInfo } from "@/lib/api";
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

  return (
    <div>
      <PageHeader
        title="System"
        subtitle="Data sources, job health and update cadences. A job that has silently stopped running shows here rather than being mistaken for 'no opportunities today'."
      />

      <section className="card mb-4 p-4">
        <h2 className="mb-3 text-sm font-semibold">Status</h2>
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
        <section className="card mb-4 p-4">
          <h2 className="mb-1 text-sm font-semibold">Snapshot timing</h2>
          {/* These were previously collapsed into one "snapshot timestamp", which
              reported the single freshest observation as though every quote had
              been captured then. They are hours to weeks apart, so each one is
              named for the question it answers. */}
          <p className="mb-3 max-w-3xl text-xs text-neutral-600 dark:text-neutral-400">
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
          <dl className="mt-4 space-y-1 border-t border-neutral-100 pt-3 text-xs text-neutral-500 dark:border-neutral-800">
            {Object.entries(s.snapshot_timing.unavailable ?? {}).map(([field, why]) => (
              <div key={field} className="flex flex-wrap gap-x-2">
                <dt className="font-mono text-neutral-600 dark:text-neutral-400">{field}</dt>
                <dd>not recorded — {why}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      <section className="card mb-4 p-4">
        <h2 className="mb-3 text-sm font-semibold">Job health</h2>
        {s.jobs?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead className="border-b border-neutral-200 dark:border-neutral-800">
                <tr><th>Job</th><th>Status</th><th>Last run</th><th>Duration</th><th>Records</th><th>Error</th></tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                {s.jobs.map((j) => (
                  <tr key={j.job_name}>
                    <td className="font-medium">{j.job_name}</td>
                    <td>
                      <span className={`chip ${j.status === "success" ? "bg-edge/15 text-edge dark:bg-edge-dark/15 dark:text-edge-dark" : j.status === "failed" ? "bg-risk/15 text-risk dark:bg-risk-dark/15 dark:text-risk-dark" : "bg-neutral-100 text-neutral-600 dark:bg-neutral-800"}`}>
                        {j.status}
                      </span>
                    </td>
                    <td className="text-neutral-500">{localTime(j.started_at)}</td>
                    <td className="num">{j.duration_seconds != null ? `${j.duration_seconds.toFixed(1)}s` : "—"}</td>
                    <td className="num">{j.records_written}</td>
                    <td className="max-w-md truncate text-xs text-risk dark:text-risk-dark">{j.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-neutral-500">
            No jobs have run yet. Start with <code>make ingest</code>.
          </p>
        )}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card p-4">
          <h2 className="mb-3 text-sm font-semibold">Row counts</h2>
          <div className="table-wrap">
            <table className="w-full">
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                {Object.entries(s.row_counts).map(([table, count]) => (
                  <tr key={table}>
                    <td>{table}</td>
                    <td className="num text-right">{count.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3 className="mt-4 text-xs font-semibold">Live vs demo</h3>
          <div className="table-wrap mt-1">
            <table className="w-full">
              <thead><tr><th>Table</th><th>Live</th><th>Demo</th></tr></thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
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

        <section className="card p-4">
          <h2 className="mb-3 text-sm font-semibold">Update cadence</h2>
          <table className="w-full">
            <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
              {Object.entries(s.update_frequencies).map(([job, freq]) => (
                <tr key={job}>
                  <td>{job.replace(/_/g, " ")}</td>
                  <td className="num text-right">{freq}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      <section className="card mt-4 p-4">
        <h2 className="mb-3 text-sm font-semibold">Data sources</h2>
        <div className="table-wrap">
          <table className="w-full">
            <thead className="border-b border-neutral-200 dark:border-neutral-800">
              <tr><th>Source</th><th>Auth</th><th>Used for</th><th>Docs</th></tr>
            </thead>
            <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
              {s.data_sources.map((src) => (
                <tr key={src.name}>
                  <td className="font-medium">{src.name}</td>
                  <td>
                    {src.auth_required ? (
                      <span className="chip bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400">
                        {src.configured ? (src.enabled ? "enabled" : "configured, disabled") : "not configured"}
                      </span>
                    ) : (
                      <span className="chip bg-edge/15 text-edge dark:bg-edge-dark/15 dark:text-edge-dark">
                        public
                      </span>
                    )}
                  </td>
                  <td className="max-w-md whitespace-normal text-xs text-neutral-600 dark:text-neutral-400">
                    {src.used_for}
                  </td>
                  <td>
                    <a href={src.docs} target="_blank" rel="noopener noreferrer" className="text-xs underline">
                      docs
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {eligibility && (
        <section className="card mt-4 p-4">
          <h2 className="mb-2 text-sm font-semibold">Eligibility &amp; compliance</h2>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric label="Research access" value={eligibility.research_access} />
            <Metric label="Trading execution" value={String(eligibility.trading_execution_available)} />
            <Metric label="Restricted regions configured" value={(eligibility.restricted_regions_configured ?? []).length} />
          </div>
          <p className="mt-3 text-xs text-neutral-600 dark:text-neutral-400">
            {eligibility.trading_execution_reason}
          </p>
          <p className="mt-2 text-xs text-neutral-600 dark:text-neutral-400">
            {eligibility.note}
          </p>
        </section>
      )}

      <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">{res.disclaimer}</p>
    </div>
  );
}
