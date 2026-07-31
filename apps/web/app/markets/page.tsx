import Link from "next/link";
import type { ReactNode } from "react";
import { apiGet, qs, type DataMode, type MarketRow } from "@/lib/api";
import {
  ageRelativeToSnapshot,
  cents,
  compactUsd,
  displayTitle,
  humanizeFlag,
  relativeToSnapshot,
} from "@/lib/format";
import { ApiDown, DemoBanner, EmptyState, PageHeader, PlatformChip, VenueAvailability } from "@/components/ui";

export const dynamic = "force-dynamic";

type Search = {
  q?: string; platform?: string; category?: string; horizon?: string;
  sort?: string; mode?: DataMode; offset?: string;
};

export default async function MarketsPage({
  searchParams,
}: {
  searchParams: Promise<Search>;
}) {
  const params = await searchParams;
  const mode: DataMode = params.mode === "demo" ? "demo" : "live";
  const offset = Number(params.offset ?? 0) || 0;
  const sort = params.sort ?? "volume";

  const [res, cats] = await Promise.all([
    apiGet<MarketRow[]>(
      `/markets${qs({
        q: params.q, platform: params.platform, category: params.category,
        horizon: params.horizon, sort, mode, limit: 50, offset,
      })}`,
    ),
    apiGet<Array<{ category: string; count: number }>>(`/markets/categories${qs({ mode })}`),
  ]);
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
  const total = (res.total as number) ?? 0;
  // Present when the sort is quote-derived: how many rows were actually ranked.
  // `total` counts the table, which is a larger number than the ranking covers.
  const rankedTotal = (res.ranked_total as number | null) ?? null;
  const shownOf = rankedTotal ?? total;

  function link(patch: Record<string, string | number | undefined>) {
    return `/markets${qs({ q: params.q, platform: params.platform, category: params.category, horizon: params.horizon, sort, mode, offset, ...patch })}`;
  }

  return (
    <div>
      <PageHeader
        title="Market browser"
        subtitle={`${total.toLocaleString()} markets ingested from Kalshi and Polymarket. Prices use the latest captured order book when available. A clearly labelled venue-summary quote is used only as a fallback when no order book was captured, and is not an executable top-of-book price.`}
      />
      <DemoBanner notice={res.demo_notice} />

      <form className="panel mb-4 flex flex-wrap items-end gap-3 p-3" action="/markets">
        <input type="hidden" name="mode" value={mode} />
        <label className="flex flex-col gap-1">
          <span className="metric-label">Search</span>
          <input
            name="q" defaultValue={params.q ?? ""} placeholder="title contains…"
            className="field w-56"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="metric-label">Venue</span>
          <select name="platform" defaultValue={params.platform ?? ""}
            className="field">
            <option value="">All</option>
            <option value="kalshi">Kalshi</option>
            <option value="polymarket">Polymarket</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="metric-label">Category</span>
          <select name="category" defaultValue={params.category ?? ""}
            className="field">
            <option value="">All</option>
            {(cats?.data ?? []).map((c) => (
              <option key={c.category} value={c.category}>
                {c.category} ({c.count})
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="metric-label">Horizon</span>
          <select name="horizon" defaultValue={params.horizon ?? ""}
            className="field">
            <option value="">Any</option>
            <option value="24h">24h</option>
            <option value="7d">7d</option>
            <option value="30d">30d</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="metric-label">Sort</span>
          <select name="sort" defaultValue={sort}
            className="field">
            <option value="volume">24h volume</option>
            <option value="liquidity">Book depth</option>
            <option value="spread">Tightest spread</option>
            <option value="resolution">Resolves soonest</option>
          </select>
        </label>
        <button className="btn-primary">
          Apply
        </button>
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title="No markets match"
          body="Adjust the filters, or run `make ingest` if the database has not been populated yet."
        />
      ) : (
        <>
          {/* A phone needs the decision-relevant fields, not a squeezed eleven-
              column table. One divided list keeps the density of a research
              terminal without turning every market into a giant SaaS card. */}
          <ul
            className="panel divide-y divide-line-subtle overflow-hidden md:hidden"
            aria-label="Ingested markets with latest captured quotes"
          >
            {rows.map((m) => (
              <li key={m.id} className="min-w-0 px-3 py-3">
                <Link
                  href={`/market/${m.id}`}
                  className="block min-w-0 break-words text-sm font-medium leading-snug text-ink hover:underline"
                >
                  {displayTitle(m.title)}
                </Link>

                <div className="mt-2 flex min-w-0 flex-wrap items-center gap-2">
                  <PlatformChip platform={m.platform} />
                  <MarketStatus status={m.status} acceptingOrders={m.accepting_orders} />
                </div>

                <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-5">
                  <div>
                    <dt className="metric-label">YES ask</dt>
                    <dd className="num mt-0.5 text-base font-semibold text-ink">
                      {cents(m.best_yes_ask)}
                    </dd>
                  </div>
                  <div className="min-w-0 text-right">
                    <dt className="metric-label">Resolution</dt>
                    <dd className="mt-0.5 text-sm leading-snug text-ink-muted">
                      {relativeToSnapshot(m.expected_resolution_time, snapshotAt)}
                    </dd>
                  </div>
                </dl>

                <details className="group mt-3 border-t border-line-subtle pt-2">
                  <summary className="cursor-pointer text-xs font-medium text-ink-muted hover:text-ink">
                    More market data
                    <span className="sr-only"> for {displayTitle(m.title)}</span>
                  </summary>
                  <dl className="mt-3 grid grid-cols-2 gap-x-5 gap-y-3 text-sm">
                    <MobileMetric label="Category" value={m.category || "—"} />
                    <MobileMetric label="YES bid" value={cents(m.best_yes_bid)} numeric />
                    <MobileMetric label="NO ask" value={cents(m.best_no_ask)} numeric />
                    <MobileMetric label="Spread" value={cents(m.spread)} numeric />
                    <MobileMetric
                      label="Ask depth"
                      value={compactUsd(m.yes_ask_depth_usd ?? m.orderbook_depth_usd)}
                      numeric
                    />
                    <MobileMetric label="24h volume" value={compactUsd(m.volume_24h)} numeric />
                    <MobileMetric
                      label="Model coverage"
                      value={
                        <>
                          Not exposed in index ·{" "}
                          <Link href={`/market/${m.id}`} className="underline hover:text-ink">
                            open analysis
                          </Link>
                        </>
                      }
                    />
                    <MobileMetric
                      label="Quote captured"
                      value={ageRelativeToSnapshot(m.quote_observed_at, snapshotAt)}
                    />
                    <MobileMetric label="Quote status" value={quoteStatusLabel(m)} />
                  </dl>
                  {m.venue_availability ? (
                    <div className="mt-3">
                      <div className="metric-label mb-1">Venue coverage</div>
                      <VenueAvailability venues={m.venue_availability} compact />
                    </div>
                  ) : null}
                </details>
              </li>
            ))}
          </ul>

          <div className="panel table-wrap hidden max-w-full md:block">
            <table className="w-full">
              <caption className="sr-only">
                Ingested markets with latest captured quotes
              </caption>
              <thead className="border-b border-line">
                <tr>
                  {/* Sticky: scrolling right through nine measurement columns used
                      to lose the only thing identifying the row. */}
                  <th scope="col" className="col-sticky market-table-title">Market</th>
                  <th scope="col">Venue</th>
                  <th scope="col" className="hidden lg:table-cell">Category</th>
                  <th scope="col" className="num hidden xl:table-cell">YES bid</th>
                  <th scope="col" className="num">YES ask</th>
                  <th scope="col" className="num hidden xl:table-cell">NO ask</th>
                  <SortableTh
                    label="Spread"
                    sortKey="spread"
                    current={sort}
                    href={link}
                    className="hidden lg:table-cell"
                  />
                  <SortableTh
                    label="Ask depth"
                    sortKey="liquidity"
                    current={sort}
                    href={link}
                    className="hidden xl:table-cell"
                  />
                  <SortableTh
                    label="24h vol"
                    sortKey="volume"
                    current={sort}
                    href={link}
                    className="hidden lg:table-cell"
                  />
                  <SortableTh
                    label="Resolves"
                    sortKey="resolution"
                    current={sort}
                    href={link}
                    numeric={false}
                  />
                  <th scope="col" className="hidden lg:table-cell">Quote</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {rows.map((m) => (
                  <tr key={m.id} className="row-hover">
                    {/* Wraps rather than truncating. A clipped contract title with
                        no way to recover it is worse than a two-line one. */}
                    <td className="col-sticky market-table-title">
                      <Link href={`/market/${m.id}`} className="hover:underline">{displayTitle(m.title)}</Link>
                      {m.venue_availability ? (
                        <div className="mt-1">
                          <VenueAvailability venues={m.venue_availability} compact />
                        </div>
                      ) : null}
                    </td>
                    <td><PlatformChip platform={m.platform} /></td>
                    <td className="hidden text-ink-faint lg:table-cell">{m.category}</td>
                    <td className="num hidden text-ink-muted xl:table-cell">{cents(m.best_yes_bid)}</td>
                    <td className="num font-semibold text-ink">{cents(m.best_yes_ask)}</td>
                    <td className="num hidden text-ink-muted xl:table-cell">{cents(m.best_no_ask)}</td>
                    <td className="num hidden text-ink-muted lg:table-cell">{cents(m.spread)}</td>
                    <td className="num hidden text-ink-muted xl:table-cell">{compactUsd(m.yes_ask_depth_usd ?? m.orderbook_depth_usd)}</td>
                    <td className="num hidden text-ink-muted lg:table-cell">{compactUsd(m.volume_24h)}</td>
                    <td className="text-ink-muted">{relativeToSnapshot(m.expected_resolution_time, snapshotAt)}</td>
                    <td className="hidden text-ink-faint lg:table-cell">
                      {ageRelativeToSnapshot(m.quote_observed_at, snapshotAt)}
                      <div className="mt-0.5 text-[10px] uppercase tracking-wide">
                        {m.quote_source === "orderbook" ? (
                          <span className="text-ink-faint">Order book</span>
                        ) : m.quote_source === "venue_summary" ? (
                          <span className="text-unverified">Venue summary fallback</span>
                        ) : (
                          <span className="text-ink-faint">No quote</span>
                        )}
                        {m.quote_is_stale_summary ? (
                          <span
                            title="The venue's summary price disagrees with the latest order book, so metadata ingest has fallen behind. The order book is shown."
                            className="ml-1 text-stale"
                          >
                            · summary stale
                          </span>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm">
            <span className="text-ink-muted">
              Showing {offset + 1}–{offset + rows.length} of{" "}
              {shownOf.toLocaleString()}
              {rankedTotal !== null && (
                <>
                  {" "}
                  ranked
                  <span className="text-ink-faint">
                    {" "}
                    (of {total.toLocaleString()} total)
                  </span>
                </>
              )}
            </span>
            <div className="flex gap-2">
              {offset > 0 && (
                <Link href={link({ offset: Math.max(0, offset - 50) })} className="btn-quiet">
                  Previous
                </Link>
              )}
              {rows.length === 50 && (
                <Link href={link({ offset: offset + 50 })} className="btn-quiet">
                  Next
                </Link>
              )}
            </div>
          </div>
        </>
      )}
      <p className="mt-6 t-meta">{res.disclaimer}</p>
    </div>
  );
}

/**
 * A column header that sorts.
 *
 * Sorting was previously only reachable through a select plus an Apply
 * round-trip, and nothing on the table said which column was ordering it. The
 * four sortable keys the API accepts are exposed where a reader looks for them,
 * and the active one is marked in text (`aria-sort` plus a caret) rather than by
 * colour alone.
 */
function SortableTh({
  label,
  sortKey,
  current,
  href,
  numeric = true,
  className = "",
}: {
  label: string;
  sortKey: string;
  current: string;
  href: (patch: Record<string, string | number | undefined>) => string;
  numeric?: boolean;
  className?: string;
}) {
  const active = current === sortKey;
  return (
    <th
      scope="col"
      className={`${numeric ? "num" : ""} ${className}`.trim() || undefined}
      aria-sort={active ? "descending" : "none"}
    >
      <Link
        href={href({ sort: sortKey, offset: 0 })}
        className={`inline-flex items-center gap-1 hover:text-ink ${
          active ? "text-ink" : ""
        }`}
      >
        {label}
        <span aria-hidden className={active ? "" : "opacity-30"}>
          {active ? "▾" : "▿"}
        </span>
        {active ? <span className="sr-only">(sorted)</span> : null}
      </Link>
    </th>
  );
}

function MobileMetric({
  label,
  value,
  numeric = false,
}: {
  label: string;
  value: ReactNode;
  numeric?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="metric-label">{label}</dt>
      <dd className={`mt-0.5 break-words text-ink-muted ${numeric ? "num" : ""}`}>
        {value}
      </dd>
    </div>
  );
}

function MarketStatus({
  status,
  acceptingOrders,
}: {
  status: string;
  acceptingOrders: boolean;
}) {
  const open = status === "open" && acceptingOrders;
  const label = open
    ? "Open"
    : status === "open"
      ? "Orders paused"
      : humanizeFlag(status);
  return (
    <span
      className={`chip ${
        open
          ? "bg-edge/15 text-edge"
          : status === "settled"
            ? "bg-info/15 text-info"
            : "bg-sunken text-ink-muted"
      }`}
    >
      {label}
    </span>
  );
}

function quoteStatusLabel(market: MarketRow): string {
  if (market.quote_source === "orderbook") return "Order book captured";
  if (market.quote_source === "venue_summary") {
    return market.quote_is_stale_summary
      ? "Venue summary fallback · summary stale"
      : "Venue summary fallback";
  }
  return "No quote";
}
