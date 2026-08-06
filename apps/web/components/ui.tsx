import Link from "next/link";
import { RISK_FLAG_EXPLANATIONS, humanizeFlag, num, utcTime } from "@/lib/format";

export function PageHeader({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="t-page-title">{title}</h1>
        {subtitle && (
          <p className="t-prose mt-2">{subtitle}</p>
        )}
      </div>
      {right}
    </div>
  );
}

/**
 * Shown whenever a surface is rendering synthetic data. Deliberately loud: the one
 * failure this project must never have is demo data mistaken for a real opportunity.
 */
export function DemoBanner({ notice }: { notice?: string }) {
  if (!notice) return null;
  return (
    <div className="mb-4 rounded-[3px] border border-demo/50 border-l-2 border-l-demo bg-demo/10 px-4 py-3 text-sm">
      <div className="t-label text-demo">Synthetic demo data</div>
      <p className="mt-1.5 text-ink">{notice}</p>
    </div>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="panel p-8 text-center">
      <p className="t-sub-title">{title}</p>
      <p className="mx-auto mt-2 max-w-2xl text-sm text-ink-muted">{body}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ApiDown() {
  return (
    <EmptyState
      title="Data temporarily unavailable"
      body={
        // The old copy told visitors to run `make api`, a local-development
        // instruction that means nothing to someone opening the hosted site - and
        // it appeared on every page during an outage.
        process.env.NODE_ENV === "development"
          ? "Could not reach the research API. Start it with `make api` (or `make dev` to run the API and this site together), then reload."
          : "We could not load market data just now. This is a problem on our side, not with your connection. Please try again in a moment."
      }
    />
  );
}

export function Metric({
  label,
  value,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn";
  hint?: string;
}) {
  const toneClass =
    tone === "good"
      ? "text-edge"
      : tone === "bad"
        ? "text-risk"
        : tone === "warn"
          ? "text-warn"
          : "";
  return (
    <div title={hint}>
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${toneClass}`}>{value}</div>
    </div>
  );
}

export function PlatformChip({ platform }: { platform: string | null }) {
  // Venue is an identifier, not a status: it gets a neutral outline, and only the
  // demo pill is tinted, because that one IS a status.
  const styles: Record<string, string> = {
    kalshi: "text-ink-muted ring-1 ring-inset ring-line",
    polymarket: "text-ink-muted ring-1 ring-inset ring-line",
    demo: "bg-demo/15 text-demo",
  };
  const label =
    platform === "kalshi"
      ? "Kalshi"
      : platform === "polymarket"
        ? "Polymarket"
        : (platform ?? "—");
  return (
    <span
      className={`chip ${styles[platform ?? ""] ?? "bg-sunken text-ink-muted"}`}
    >
      {label}
    </span>
  );
}

export function SideChip({ side }: { side: string }) {
  const isYes = side === "yes";
  return (
    <span
      className={`chip ${
        isYes
          ? "bg-edge/15 text-edge"
          : "bg-risk/15 text-risk"
      }`}
    >
      {isYes ? "BUY YES" : "BUY NO"}
    </span>
  );
}

export function RiskFlags({ flags }: { flags: string[] }) {
  if (!flags?.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {flags.map((flag) => (
        <span
          key={flag}
          title={RISK_FLAG_EXPLANATIONS[flag] ?? flag}
          className="chip cursor-help bg-sunken text-ink-muted"
        >
          {humanizeFlag(flag)}
        </span>
      ))}
    </div>
  );
}

export function StateChip({ state }: { state: string }) {
  const styles: Record<string, string> = {
    still_actionable: "bg-edge/15 text-edge",
    edge_reduced: "bg-warn/15 text-warn",
    no_longer_actionable:
      "bg-sunken text-ink-muted",
    market_closed:
      "bg-sunken text-ink-muted",
    settled: "bg-info/15 text-info",
  };
  return (
    <span className={`chip ${styles[state] ?? "bg-sunken text-ink-muted"}`}>
      {humanizeFlag(state)}
    </span>
  );
}

/** Colour-coded by whether the label claims a locked-in result. */
export function ArbLabelChip({ label }: { label: string }) {
  const styles: Record<string, string> = {
    executable: "bg-edge/20 text-edge",
    logical_mispricing:
      "bg-unverified/15 text-unverified",
    stale_quote: "bg-stale/15 text-stale",
  };
  return (
    <span
      className={`chip font-semibold ${
        styles[label] ??
        "bg-sunken text-ink-muted"
      }`}
    >
      {humanizeFlag(label)}
    </span>
  );
}

export function ValueTone({ value, children }: { value: string | number | null; children: React.ReactNode }) {
  const parsed = num(value);
  const tone =
    parsed === null ? "" : parsed > 0 ? "text-edge" : parsed < 0 ? "text-risk" : "";
  return <span className={`num ${tone}`}>{children}</span>;
}

export function MarketLink({ id, children }: { id: number; children: React.ReactNode }) {
  return (
    <Link href={`/market/${id}`} className="hover:underline">
      {children}
    </Link>
  );
}

export function Disclaimer({ text }: { text: string }) {
  return (
    <p className="mt-6 t-meta">{text}</p>
  );
}

/**
 * Site-wide notice that the deployment is serving a frozen snapshot.
 *
 * A serverless deployment ships a pre-built database inside the bundle, so prices
 * and model estimates are frozen at build time. The platform's whole premise is a
 * daily scan, so rendering stale numbers without saying so would misrepresent it -
 * this is the same reason demo data carries a banner.
 */
export function SnapshotBanner({
  active,
  latestQuoteAt,
  arbitrageScanAt,
}: {
  active?: boolean;
  /** The single most recent observation in the database - NOT a capture time
      for the dataset. Most markets are older, some by weeks. */
  latestQuoteAt?: string | null;
  arbitrageScanAt?: string | null;
}) {
  if (!active) return null;
  return (
    <div className="mb-4 rounded-[3px] border border-stale/50 border-l-2 border-l-stale bg-stale/10 px-4 py-3 text-sm">
      <span className="font-semibold text-stale">
        Research snapshot.
      </span>{" "}
      <span className="text-ink">
        This hosted deployment serves frozen data, not a live scan. Orderbooks and
        model estimates are stale.
        {process.env.NODE_ENV === "development" ? (
          <>
            {" "}Run the pipeline locally (
            <code className="font-mono text-xs">make ingest &amp;&amp; make rank</code>)
            for current data.
          </>
        ) : null}
      </span>
      {/* Previously one timestamp introduced as "quotes captured", which read as
          though every price was that fresh. On the deployed artefact that
          described 12 markets out of 1850. Both times are labelled for what they
          are, and the spread is stated rather than left to be inferred. */}
      {(latestQuoteAt || arbitrageScanAt) && (
        <span className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
          {latestQuoteAt && (
            <span>
              Latest captured quote:{" "}
              <span className="font-mono">{utcTime(latestQuoteAt)}</span>
            </span>
          )}
          {arbitrageScanAt && (
            <span>
              Arbitrage scan:{" "}
              <span className="font-mono">{utcTime(arbitrageScanAt)}</span>
            </span>
          )}
          <span>Individual markets may have older quotes; each page shows its own.</span>
        </span>
      )}
    </div>
  );
}

/** Tappable inline explanation. Not hover-only — hover does not exist on touch. */
export function HelpDot({ text }: { text: string }) {
  return (
    <details className="group relative inline-block align-middle">
      <summary
        className="ml-1 inline-flex h-4 w-4 cursor-pointer list-none items-center justify-center rounded-full border border-line text-[10px] leading-none text-ink-faint hover:border-line-strong hover:text-ink"
        aria-label="What does this mean?"
      >
        ?
      </summary>
      <span className="absolute left-0 top-6 z-30 block w-64 rounded-lg border border-line bg-raised p-3 text-xs font-normal normal-case leading-relaxed text-ink shadow-lg">
        {text}
      </span>
    </details>
  );
}

/**
 * Three-state numeric tone.
 *
 * The previous logic coloured anything non-positive red, so a genuine zero and an
 * unavailable value both rendered as a loss. Zero means "no difference" and unknown
 * means "no answer"; neither is bad news.
 */
export function toneFor(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "text-ink-faint";
  if (value > 0) return "text-edge";
  if (value < 0) return "text-risk";
  return "text-ink-faint";
}

/** Headline answer card for the top of a results page. */
export function VerdictCard({
  label,
  value,
  sub,
  tone,
  help,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: string;
  help?: string;
}) {
  return (
    <div className="panel p-4">
      <div className="flex items-center t-label">
        {label}
        {help ? <HelpDot text={help} /> : null}
      </div>
      <div className={`mt-1 font-mono text-2xl font-semibold ${tone ?? ""}`}>
        {value}
      </div>
      {sub ? (
        <div className="mt-1 t-meta">{sub}</div>
      ) : null}
    </div>
  );
}

/**
 * Venue availability chips.
 *
 * Broker availability is shown separately from exchange availability and is never
 * inferred from it. A contract listed on Kalshi says nothing about whether a broker
 * resells it, and presenting the two identically would imply a claim the platform
 * cannot support.
 */
export function VenueAvailability({
  venues,
  compact = false,
}: {
  venues: { venue: string; status: string; label: string }[];
  compact?: boolean;
}) {
  if (!venues?.length) return null;
  // A list row already shows which exchange the contract came from, so repeating it
  // is noise. What a reader cannot infer is broker availability - and that is exactly
  // the thing that must never be guessed from an exchange listing.
  const shown = compact
    ? venues.filter((v) => v.status === "unverified")
    : venues;
  if (!shown.length) return null;
  const tone = (status: string) =>
    status === "observed_via_public_api" || status === "confirmed_available"
      ? "border-edge/40 text-edge"
      : status === "confirmed_unavailable"
        ? "border-line text-ink-faint"
        : "border-unverified/50 text-unverified";
  return (
    <div className="flex flex-wrap gap-1">
      {shown.map((v) => (
        <span
          key={v.venue}
          title={v.label}
          className={`rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${tone(v.status)}`}
        >
          {v.venue}: {v.label}
        </span>
      ))}
    </div>
  );
}
