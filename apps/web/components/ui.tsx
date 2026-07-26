import Link from "next/link";
import { RISK_FLAG_EXPLANATIONS, humanizeFlag, num } from "@/lib/format";

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
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && (
          <p className="mt-1 max-w-3xl text-sm text-neutral-600 dark:text-neutral-400">
            {subtitle}
          </p>
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
    <div className="mb-4 rounded-lg border-2 border-warn/60 bg-warn/10 px-4 py-3 text-sm dark:border-warn-dark/50 dark:bg-warn-dark/10">
      <div className="font-semibold text-warn dark:text-warn-dark">
        SYNTHETIC DEMO DATA
      </div>
      <p className="mt-1 text-neutral-700 dark:text-neutral-300">{notice}</p>
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
    <div className="card p-8 text-center">
      <p className="text-sm font-semibold">{title}</p>
      <p className="mx-auto mt-2 max-w-2xl text-sm text-neutral-600 dark:text-neutral-400">
        {body}
      </p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ApiDown() {
  return (
    <EmptyState
      title="API unavailable"
      body="Could not reach the research API. Start it with `make api` (or `make dev` to run the API and this site together), then reload."
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
      ? "text-edge dark:text-edge-dark"
      : tone === "bad"
        ? "text-risk dark:text-risk-dark"
        : tone === "warn"
          ? "text-warn dark:text-warn-dark"
          : "";
  return (
    <div title={hint}>
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${toneClass}`}>{value}</div>
    </div>
  );
}

export function PlatformChip({ platform }: { platform: string | null }) {
  const styles: Record<string, string> = {
    kalshi: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
    polymarket:
      "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
    demo: "bg-warn/20 text-warn dark:bg-warn-dark/20 dark:text-warn-dark",
  };
  const label =
    platform === "kalshi"
      ? "Kalshi"
      : platform === "polymarket"
        ? "Polymarket"
        : (platform ?? "—");
  return (
    <span
      className={`chip ${styles[platform ?? ""] ?? "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"}`}
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
          ? "bg-edge/15 text-edge dark:bg-edge-dark/15 dark:text-edge-dark"
          : "bg-risk/15 text-risk dark:bg-risk-dark/15 dark:text-risk-dark"
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
          className="chip cursor-help bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400"
        >
          {humanizeFlag(flag)}
        </span>
      ))}
    </div>
  );
}

export function StateChip({ state }: { state: string }) {
  const styles: Record<string, string> = {
    still_actionable: "bg-edge/15 text-edge dark:bg-edge-dark/15 dark:text-edge-dark",
    edge_reduced: "bg-warn/15 text-warn dark:bg-warn-dark/15 dark:text-warn-dark",
    no_longer_actionable:
      "bg-neutral-200 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400",
    market_closed:
      "bg-neutral-200 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-400",
    settled: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  };
  return (
    <span className={`chip ${styles[state] ?? "bg-neutral-100 text-neutral-600"}`}>
      {humanizeFlag(state)}
    </span>
  );
}

/** Colour-coded by whether the label claims a locked-in result. */
export function ArbLabelChip({ label }: { label: string }) {
  const styles: Record<string, string> = {
    executable: "bg-edge/20 text-edge dark:bg-edge-dark/20 dark:text-edge-dark",
    logical_mispricing:
      "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
    stale_quote: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300",
  };
  return (
    <span
      className={`chip font-semibold ${
        styles[label] ??
        "bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"
      }`}
    >
      {humanizeFlag(label)}
    </span>
  );
}

export function ValueTone({ value, children }: { value: string | number | null; children: React.ReactNode }) {
  const parsed = num(value);
  const tone =
    parsed === null ? "" : parsed > 0 ? "text-edge dark:text-edge-dark" : parsed < 0 ? "text-risk dark:text-risk-dark" : "";
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
    <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">{text}</p>
  );
}
