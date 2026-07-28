import Link from "next/link";
import { apiGet, qs, type DataMode, type MarketRow } from "@/lib/api";
import { ageLabel, cents, compactUsd, displayTitle, relativeTime } from "@/lib/format";
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
  if (!res) return <ApiDown />;

  const rows = res.data ?? [];
  const total = (res.total as number) ?? 0;

  function link(patch: Record<string, string | number | undefined>) {
    return `/markets${qs({ q: params.q, platform: params.platform, category: params.category, horizon: params.horizon, sort, mode, offset, ...patch })}`;
  }

  return (
    <div>
      <PageHeader
        title="Market browser"
        subtitle={`${total.toLocaleString()} markets ingested from Kalshi and Polymarket. Prices shown are live top-of-book, not last trades.`}
      />
      <DemoBanner notice={res.demo_notice} />

      <form className="card mb-4 flex flex-wrap items-end gap-3 p-3" action="/markets">
        <input type="hidden" name="mode" value={mode} />
        <label className="flex flex-col gap-1">
          <span className="metric-label">Search</span>
          <input
            name="q" defaultValue={params.q ?? ""} placeholder="title contains…"
            className="w-56 rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="metric-label">Venue</span>
          <select name="platform" defaultValue={params.platform ?? ""}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700">
            <option value="">All</option>
            <option value="kalshi">Kalshi</option>
            <option value="polymarket">Polymarket</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="metric-label">Category</span>
          <select name="category" defaultValue={params.category ?? ""}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700">
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
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700">
            <option value="">Any</option>
            <option value="24h">24h</option>
            <option value="7d">7d</option>
            <option value="30d">30d</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="metric-label">Sort</span>
          <select name="sort" defaultValue={sort}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700">
            <option value="volume">24h volume</option>
            <option value="liquidity">Book depth</option>
            <option value="spread">Tightest spread</option>
            <option value="resolution">Resolves soonest</option>
          </select>
        </label>
        <button className="rounded bg-neutral-900 px-3 py-1.5 text-sm text-white dark:bg-neutral-100 dark:text-neutral-900">
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
          <div className="card table-wrap">
            <table className="w-full">
              <thead className="border-b border-neutral-200 dark:border-neutral-800">
                <tr>
                  <th>Market</th><th>Venue</th><th>Category</th>
                  <th>YES bid</th><th>YES ask</th><th>NO ask</th>
                  <th>Spread</th><th>Depth</th><th>24h vol</th>
                  <th>Resolves</th><th>Quote</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
                {rows.map((m) => (
                  <tr key={m.id} className="hover:bg-neutral-50 dark:hover:bg-neutral-900">
                    <td className="max-w-md">
                      <Link href={`/market/${m.id}`} className="hover:underline">{displayTitle(m.title)}</Link>
                    </td>
                    <td><PlatformChip platform={m.platform} /></td>
                    <td className="text-neutral-500">{m.category}</td>
                    <td className="num">{cents(m.best_yes_bid)}</td>
                    <td className="num font-semibold">{cents(m.best_yes_ask)}</td>
                    <td className="num">{cents(m.best_no_ask)}</td>
                    <td className="num">{cents(m.spread)}</td>
                    <td className="num">{compactUsd(m.orderbook_depth_usd)}</td>
                    <td className="num">{compactUsd(m.volume_24h)}</td>
                    <td className="text-neutral-500">{relativeTime(m.expected_resolution_time)}</td>
                    <td className="text-neutral-500">{ageLabel(m.quote_observed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex items-center justify-between text-sm">
            <span className="text-neutral-500">
              Showing {offset + 1}–{offset + rows.length} of {total.toLocaleString()}
            </span>
            <div className="flex gap-2">
              {offset > 0 && (
                <Link href={link({ offset: Math.max(0, offset - 50) })}
                  className="rounded border border-neutral-300 px-3 py-1 dark:border-neutral-700">
                  Previous
                </Link>
              )}
              {rows.length === 50 && (
                <Link href={link({ offset: offset + 50 })}
                  className="rounded border border-neutral-300 px-3 py-1 dark:border-neutral-700">
                  Next
                </Link>
              )}
            </div>
          </div>
        </>
      )}
      <p className="mt-6 text-xs text-neutral-500 dark:text-neutral-400">{res.disclaimer}</p>
    </div>
  );
}
