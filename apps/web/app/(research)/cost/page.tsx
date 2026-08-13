import type { Metadata } from "next";
import Link from "next/link";

import { apiGet, qs, type CostIndexRow, type DataMode } from "@/lib/api";
import { cents, compactUsd, displayTitle, num, pct } from "@/lib/format";
import {
  ApiDown,
  DemoBanner,
  EmptyState,
  HelpDot,
  PageHeader,
  PlatformChip,
} from "@/components/ui";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Execution cost",
  description:
    "An auditable prediction-market entry-cost estimate: observed order-book depth and venue fee rules, plus disclosed transfer and capital-cost assumptions.",
};

/**
 * The cost surface.
 *
 * Every other research page here is downstream of a probability estimate, and the
 * independence rule refuses to produce one for most markets. That is correct, and
 * it is why those pages are usually empty — which leaves a reader with nothing on
 * the majority of visits.
 *
 * This page asks a question that needs no probability: what does it cost to get in.
 * Every market with a quote has an answer, so this surface is never empty for the
 * reason the others are. It is not a weaker version of the opportunity scan; it
 * measures something the venues themselves do not display.
 */

const SIZES = ["1", "10", "100", "1000"] as const;

/**
 * The categories offered as a front door, sports and politics first.
 *
 * Not alphabetical and not by market count. These two carry most of the retail
 * volume on both venues and they are precisely the categories where every
 * probability model here declines - sports has a base-rate prior that is off by
 * default pending evidence, and politics has no keyless polling aggregate to
 * build on at all. Cost needs no forecast, so this is the one surface that has a
 * real answer for them, and it is the reason they belong at the front rather
 * than behind an opportunity list that is empty for them by construction.
 */
const CATEGORIES: Array<{ key: string | undefined; label: string }> = [
  { key: undefined, label: "All" },
  { key: "sports", label: "Sports" },
  { key: "politics", label: "Politics" },
  { key: "crypto", label: "Crypto" },
  { key: "economics", label: "Economics" },
  { key: "finance", label: "Finance" },
  { key: "weather", label: "Weather" },
];

export default async function CostPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const one = (key: string) => {
    const value = params[key];
    return typeof value === "string" ? value : undefined;
  };

  const size = SIZES.includes((one("size") ?? "") as (typeof SIZES)[number])
    ? (one("size") as string)
    : "100";
  const platform = one("platform");
  const rawCategory = one("category");
  const category = CATEGORIES.some((c) => c.key === rawCategory)
    ? rawCategory
    : undefined;
  const mode = (one("mode") as DataMode) ?? "live";
  const q = (one("q") ?? "").trim().slice(0, 120);

  const res = await apiGet<CostIndexRow[]>(
    `/cost${qs({ size, platform, category, q: q || undefined, mode, limit: 40 })}`,
  );
  if (!res) return <ApiDown />;

  const rows = res.data ?? [];

  return (
    <>
      <DemoBanner notice={res.demo_notice} />
      <PageHeader
        title="What entry can cost"
        subtitle="A contract's quoted price is not the whole entry cost. This page combines observed ask depth and published venue fee rules with disclosed transfer and capital-cost assumptions, then shows the estimate and its basis for every market with a quote."
      />

      <LiveOverlayCallout />
      <Explainer size={size} />
      <ContractSearch
        q={q}
        size={size}
        platform={platform}
        category={category}
        mode={mode}
      />

      <div className="mt-6 flex min-w-0 flex-wrap items-center gap-x-6 gap-y-3">
        <SizePicker current={size} platform={platform} category={category} q={q} mode={mode} />
        <PlatformPicker current={platform} size={size} category={category} q={q} mode={mode} />
        <CategoryPicker current={category} size={size} platform={platform} q={q} mode={mode} />
      </div>

      <CategoryNote category={category} />

      {rows.length === 0 ? (
        <div className="mt-6">
          <EmptyState
            title={q ? `No priced contract matched “${q}”` : "No markets could be priced"}
            body={
              q
                ? "Try fewer words, a venue market ID, or clear the search. Search results still require an observed ask, so an unpriced contract will not appear here."
                : "This is an observation gap, not a finding: no market in the current snapshot carried an ask price from either the order book or the venue summary. The system page reports when data was last ingested."
            }
            action={
              <Link
                href={q ? `/cost${qs({ size, platform, category, mode })}` : "/system"}
                className="btn-quiet"
              >
                {q ? "Clear search" : "Check system status"}
              </Link>
            }
          />
        </div>
      ) : (
        <CostTable rows={rows} size={size} mode={mode} />
      )}

      <Method />
    </>
  );
}

function LiveOverlayCallout() {
  return (
    <div className="mt-5 flex flex-col gap-3 rounded-[3px] border border-accent/35 bg-accent/5 p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="max-w-3xl">
        <p className="t-label text-accent">Live on the venue page</p>
        <p className="t-body mt-1">
          This table is a hosted snapshot. The read-only browser overlay recalculates
          the same cost stack for the order size on an open Kalshi or Polymarket page.
        </p>
      </div>
      <Link href="/extension" className="btn-primary shrink-0">
        Get the live overlay
      </Link>
    </div>
  );
}

function ContractSearch({
  q,
  size,
  platform,
  category,
  mode,
}: {
  q: string;
  size: string;
  platform?: string;
  category?: string;
  mode: DataMode;
}) {
  return (
    <form method="get" action="/cost" className="panel mt-5 p-4">
      <label htmlFor="contract-search" className="t-label">
        Find your contract
      </label>
      <div className="mt-2 flex flex-col gap-2 sm:flex-row">
        <input
          id="contract-search"
          name="q"
          type="search"
          maxLength={120}
          defaultValue={q}
          placeholder="Title or venue market ID"
          className="field min-w-0 flex-1"
        />
        <input type="hidden" name="size" value={size} />
        {platform ? <input type="hidden" name="platform" value={platform} /> : null}
        {category ? <input type="hidden" name="category" value={category} /> : null}
        <input type="hidden" name="mode" value={mode} />
        <button type="submit" className="btn-primary">
          Price this contract
        </button>
        {q ? (
          <Link
            href={`/cost${qs({ size, platform, category, mode })}`}
            className="btn-quiet justify-center"
          >
            Clear
          </Link>
        ) : null}
      </div>
      <p className="t-meta mt-2">
        Matches words across the contract title, subtitle, and venue market ID.
      </p>
    </form>
  );
}

/* ------------------------------------------------------------------ header -- */

function Explainer({ size }: { size: string }) {
  return (
    <div className="panel mt-5 p-4">
      <p className="t-label">The cost stack</p>
      <p className="t-body mt-2 max-w-3xl">
        Each row below is priced at <strong>{size} contract{size === "1" ? "" : "s"}</strong>.
        The premium is the estimated amount above the quoted price, per contract.
        Observed depth and published fee rules are checkable inputs; the transfer
        allowance and annual capital rate are explicit configuration assumptions.
      </p>
      <p className="t-body mt-2 max-w-3xl">
        Size matters more than most readers expect. Kalshi ceils its fee to the whole
        cent <em>on the whole order</em>, so a single contract can carry many times
        the per-contract fee of the same contract bought a hundred at a time — on a
        1¢ contract the ceiled fee is a full cent, which doubles the cost of the
        trade. Switch the size above and watch the ranking change.
      </p>
    </div>
  );
}

function SizePicker({
  current,
  platform,
  category,
  q,
  mode,
}: {
  current: string;
  platform?: string;
  category?: string;
  q: string;
  mode: DataMode;
}) {
  return (
    <div className="flex min-w-0 max-w-full items-center gap-2">
      <span className="t-label">Order size</span>
      <div className="seg seg-scroll" role="group" aria-label="Order size">
        {SIZES.map((size) => (
          <Link
            key={size}
            href={`/cost${qs({ size, platform, category, q: q || undefined, mode })}`}
            aria-current={size === current ? "page" : undefined}
            className={`seg-item ${size === current ? "seg-item-on" : "hover:text-ink"}`}
          >
            {size}
          </Link>
        ))}
      </div>
    </div>
  );
}

function PlatformPicker({
  current,
  size,
  category,
  q,
  mode,
}: {
  current?: string;
  size: string;
  category?: string;
  q: string;
  mode: DataMode;
}) {
  const options: Array<{ key: string | undefined; label: string }> = [
    { key: undefined, label: "Both venues" },
    { key: "kalshi", label: "Kalshi" },
    { key: "polymarket", label: "Polymarket" },
  ];
  return (
    <div className="flex min-w-0 max-w-full items-center gap-2">
      <span className="t-label">Venue</span>
      <div className="seg seg-scroll" role="group" aria-label="Venue">
        {options.map((option) => (
          <Link
            key={option.label}
            href={`/cost${qs({ size, platform: option.key, category, q: q || undefined, mode })}`}
            aria-current={option.key === current ? "page" : undefined}
            className={`seg-item ${option.key === current ? "seg-item-on" : "hover:text-ink"}`}
          >
            {option.label}
          </Link>
        ))}
      </div>
    </div>
  );
}

function CategoryPicker({
  current,
  size,
  platform,
  q,
  mode,
}: {
  current?: string;
  size: string;
  platform?: string;
  q: string;
  mode: DataMode;
}) {
  return (
    <div className="flex min-w-0 max-w-full items-center gap-2">
      <span className="t-label">Category</span>
      <div className="seg seg-scroll" role="group" aria-label="Category">
        {CATEGORIES.map((option) => (
          <Link
            key={option.label}
            href={`/cost${qs({ size, platform, category: option.key, q: q || undefined, mode })}`}
            aria-current={option.key === current ? "page" : undefined}
            className={`seg-item ${option.key === current ? "seg-item-on" : "hover:text-ink"}`}
          >
            {option.label}
          </Link>
        ))}
      </div>
    </div>
  );
}

/**
 * Why this category has no forecast, said on the category's own page.
 *
 * Sports and politics are the two the front door leads with, and they are the
 * two where every probability model here declines. A reader who arrives from a
 * shared link and sees only cost figures should be told that is the whole offer
 * for this category, in one sentence, rather than discovering it by noticing an
 * absence.
 */
function CategoryNote({ category }: { category?: string }) {
  const notes: Record<string, string> = {
    sports:
      "No forecast is published for sports. The record-based prior is off by default until a historical evaluation shows it beats the market's own price, and everything that is not a head-to-head game — finishing positions, futures, player props — has no keyless model at all. The cost figures below need no forecast and are the whole offer here.",
    politics:
      "No forecast is published for politics. There is no keyless, live polling aggregate to build one on: FiveThirtyEight was wound down and its data URLs now answer with an HTML error page. Guessing would still be weighted and would still generate edge, so the category stays silent and the cost figures below stand alone.",
  };
  const note = category ? notes[category] : undefined;
  if (!note) return null;
  return (
    <p className="panel mt-4 p-4 t-body max-w-3xl">
      <span className="t-label">No forecast in this category</span>
      <span className="mt-2 block">{note}</span>
    </p>
  );
}

/* ------------------------------------------------------------------- table -- */

function CostTable({
  rows,
  size,
  mode,
}: {
  rows: CostIndexRow[];
  size: string;
  mode: DataMode;
}) {
  return (
    <div className="table-wrap mt-6">
      <table>
        <thead>
          <tr>
            <th scope="col">Contract</th>
            <th scope="col" className="hidden xl:table-cell">Venue</th>
            <th scope="col" className="num hidden md:table-cell">
              Quoted
            </th>
            <th scope="col" className="num">
              Cost estimate
              <HelpDot text="Per contract at this size. Uses observed depth and published venue fee rules plus disclosed transfer and capital-cost assumptions. Excludes the separate slippage pad." />
            </th>
            <th scope="col" className="num">
              Est. premium
            </th>
            <th scope="col" className="num">
              Est. break-even
              <HelpDot text="A binary contract pays exactly $1, so the cost estimate per contract maps to a break-even probability under the stated assumptions." />
            </th>
            <th scope="col" className="hidden lg:table-cell">Basis</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <CostRow key={row.market.id} row={row} size={size} mode={mode} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CostRow({
  row,
  size,
  mode,
}: {
  row: CostIndexRow;
  size: string;
  mode: DataMode;
}) {
  const ratio = num(row.measured_premium_ratio);
  // A premium is a cost, so it never reads as a gain. The scale is severity:
  // a doubling of cost and a 2% overhead should not look the same.
  const tone =
    ratio === null
      ? ""
      : ratio >= 0.5
        ? "text-risk"
        : ratio >= 0.1
          ? "text-warn"
          : "text-ink";

  return (
    <tr>
      <th scope="row" className="cell-title col-sticky">
        <Link
          href={`/cost/${row.market.id}${qs({ size, mode })}`}
          className="hover:underline"
          title={displayTitle(row.market.title)}
        >
          {displayTitle(row.market.title)}
        </Link>
        {row.market.volume_24h && (
          <span className="t-meta ml-2">
            {compactUsd(row.market.volume_24h)} 24h
          </span>
        )}
      </th>
      <td className="hidden xl:table-cell">
        <PlatformChip platform={row.market.platform} />
      </td>
      <td className="num hidden md:table-cell">{cents(row.nominal_price)}</td>
      <td className="num">{cents(row.measured_cost)}</td>
      <td className={`num ${tone}`}>
        +{cents(row.measured_premium)}
        {ratio !== null && (
          <span className="t-meta ml-1">({pct(ratio, 0)})</span>
        )}
      </td>
      <td className="num">
        {row.breakeven_probability === null ? (
          <span title="Cost reaches $1: no probability breaks even.">
            impossible
          </span>
        ) : (
          pct(row.breakeven_probability)
        )}
      </td>
      <td className="hidden lg:table-cell">
        <BasisChip row={row} />
      </td>
    </tr>
  );
}

/**
 * Whether the row rests on a real ladder or only a top-of-book summary.
 *
 * Kept as a visible column rather than a footnote: a summary-derived premium
 * excludes depth impact entirely, so its cost estimate is incomplete. Presenting
 * it beside a book-derived figure without saying which is which would make the two
 * look equally well founded.
 */
function BasisChip({ row }: { row: CostIndexRow }) {
  if (!row.depth_known) {
    return (
      <span
        className={`chip ${row.is_stale ? "bg-warn/15 text-warn" : "bg-sunken text-ink-muted"}`}
        title={
          row.is_stale
            ? "The summary quote is stale and no order book was captured, so depth impact is also unknown and excluded."
            : "No order book was captured for this contract, so depth impact is unknown and excluded. The estimate is incomplete."
        }
      >
        {row.is_stale ? "stale summary" : "summary only"}
      </span>
    );
  }
  if (!row.fully_filled) {
    return (
      <span className="chip bg-warn/15 text-warn" title="The observed book could not fill this size. Cost is reported for the part that fills.">
        partial fill
      </span>
    );
  }
  if (row.is_stale) {
    return (
      <span className="chip bg-warn/15 text-warn" title="The order book behind this row is over 30 minutes old.">
        stale book
      </span>
    );
  }
  return <span className="chip bg-sunken text-ink-muted">order book</span>;
}

/* ------------------------------------------------------------------ method -- */

function Method() {
  return (
    <section className="mt-10 border-t border-line pt-6">
      <h2 className="t-section-title">What is and is not in these numbers</h2>
      <div className="mt-4 grid gap-6 lg:grid-cols-2">
        <div className="block">
          <h3 className="t-sub-title">Observed or rule-derived — in the headline</h3>
          <ul className="t-body mt-2 space-y-1.5">
            <li>
              <strong>Depth impact.</strong> The volume-weighted price of the ask
              ladder at your size, above the top-of-book price. Zero when one level
              fills the order; omitted entirely when no book was observed.
            </li>
            <li>
              <strong>Venue fee.</strong> Kalshi&apos;s quadratic taker fee and
              Polymarket&apos;s per-market rate, at the size being bought.
            </li>
            <li>
              <strong>Fee rounding.</strong> Kalshi ceils to the whole cent on the
              whole order. Reported separately because it is the reason small orders
              are disproportionately expensive.
            </li>
          </ul>
          <h3 className="t-sub-title mt-5">Configured assumptions — in the headline</h3>
          <ul className="t-body mt-2 space-y-1.5">
            <li>
              <strong>Transfer allowance.</strong> A fixed Polygon bridge and gas
              allowance is amortised over the position; it is zero on Kalshi.
            </li>
            <li>
              <strong>Capital rate.</strong> A configured annual opportunity-cost
              rate is applied until expected resolution. It is a scenario input,
              not an observed market charge.
            </li>
          </ul>
        </div>
        <div className="block">
          <h3 className="t-sub-title">Modelled — reported, not in the headline</h3>
          <p className="t-body mt-2">
            A flat slippage pad of one tick stands in for the market impact between
            observing a book and reaching it. It is an assumption, not a measurement,
            and this platform lists it as a known limitation rather than a result.
          </p>
          <p className="t-body mt-2">
            At a 1¢ tick the pad is a whole cent, which on a cheap contract can exceed
            every observed or rule-derived component combined. Folding it into one
            number would make the headline mostly an artefact of a configuration
            default, so it is shown
            beside the headline estimate on each contract&apos;s own page and excluded
            from the break-even probability.
          </p>
          <h3 className="t-sub-title mt-5">Not included at all</h3>
          <p className="t-body mt-2">
            Taxes, withdrawal fees, and the bid-ask spread you would pay to exit
            early. Every figure here prices <em>entering</em> a position and holding
            it to resolution.
          </p>
        </div>
      </div>
      <p className="t-meta mt-6">
        Research and information only. Estimating entry cost is not a recommendation
        to trade.
      </p>
    </section>
  );
}
