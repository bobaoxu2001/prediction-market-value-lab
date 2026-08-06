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
    "What a prediction-market contract actually costs to buy, after the venue's fee schedule, its rounding rule, order-book depth, transfer and capital cost — for every market with a quote, not only the ones a model has an opinion about.",
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
  const mode = (one("mode") as DataMode) ?? "live";

  const res = await apiGet<CostIndexRow[]>(
    `/cost${qs({ size, platform, mode, limit: 40 })}`,
  );
  if (!res) return <ApiDown />;

  const rows = res.data ?? [];

  return (
    <>
      <DemoBanner notice={res.demo_notice} />
      <PageHeader
        title="What it actually costs"
        subtitle="A contract's quoted price is not its cost. Between the number on the screen and the money that leaves your account sit the venue's fee, its rounding rule, the depth you have to walk to fill your size, the cost of moving funds onto the venue, and the capital locked up until the market resolves. This page measures that gap for every market with a quote."
      />

      <Explainer size={size} />

      <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-3">
        <SizePicker current={size} platform={platform} />
        <PlatformPicker current={platform} size={size} />
      </div>

      {rows.length === 0 ? (
        <div className="mt-6">
          <EmptyState
            title="No markets could be priced"
            body="This is an observation gap, not a finding: no market in the current snapshot carried an ask price from either the order book or the venue summary. The system page reports when data was last ingested."
            action={
              <Link href="/system" className="btn-quiet">
                Check system status
              </Link>
            }
          />
        </div>
      ) : (
        <CostTable rows={rows} size={size} />
      )}

      <Method />
    </>
  );
}

/* ------------------------------------------------------------------ header -- */

function Explainer({ size }: { size: string }) {
  return (
    <div className="panel mt-5 p-4">
      <p className="t-label">The measurement</p>
      <p className="t-body mt-2 max-w-3xl">
        Each row below is priced at <strong>{size} contract{size === "1" ? "" : "s"}</strong>.
        The premium is what you pay above the quoted price, per contract, and it is
        computed only from things that can be checked: the observed ask ladder, the
        venues&apos; published fee schedules and their documented rounding rules.
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
}: {
  current: string;
  platform?: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="t-label">Order size</span>
      <div className="seg" role="group">
        {SIZES.map((size) => (
          <Link
            key={size}
            href={`/cost${qs({ size, platform })}`}
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
}: {
  current?: string;
  size: string;
}) {
  const options: Array<{ key: string | undefined; label: string }> = [
    { key: undefined, label: "Both venues" },
    { key: "kalshi", label: "Kalshi" },
    { key: "polymarket", label: "Polymarket" },
  ];
  return (
    <div className="flex items-center gap-2">
      <span className="t-label">Venue</span>
      <div className="seg" role="group">
        {options.map((option) => (
          <Link
            key={option.label}
            href={`/cost${qs({ size, platform: option.key })}`}
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

/* ------------------------------------------------------------------- table -- */

function CostTable({ rows, size }: { rows: CostIndexRow[]; size: string }) {
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
              True cost
              <HelpDot text="Per contract at this size, from observed depth and the venue's published fee rules. Excludes the modelled slippage pad, which is an assumption rather than a measurement." />
            </th>
            <th scope="col" className="num">
              Premium
            </th>
            <th scope="col" className="num">
              Break-even
              <HelpDot text="A binary contract pays exactly $1, so the cost per contract is the probability at which the purchase breaks even. Quoted price 34¢ with a true cost of 37.2¢ means the event must occur 37.2% of the time, not 34%." />
            </th>
            <th scope="col" className="hidden lg:table-cell">Basis</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <CostRow key={row.market.id} row={row} size={size} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CostRow({ row, size }: { row: CostIndexRow; size: string }) {
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
          href={`/cost/${row.market.id}${qs({ size })}`}
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
 * excludes depth impact entirely, so it is a floor on the true cost. Presenting it
 * beside a book-derived figure without saying which is which would make the two
 * look equally well founded.
 */
function BasisChip({ row }: { row: CostIndexRow }) {
  if (!row.depth_known) {
    return (
      <span className="chip bg-sunken text-ink-muted" title="No order book was captured for this contract, so depth impact is unknown and excluded. The premium shown is a floor.">
        summary only
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
          <h3 className="t-sub-title">Measured — in the headline</h3>
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
            <li>
              <strong>Transfer.</strong> Polymarket settles in USDC on Polygon; the
              bridge allowance is amortised over the position, which is why a small
              position there costs several times its notional.
            </li>
            <li>
              <strong>Capital cost.</strong> The stake is locked until resolution. A
              contract resolving in nine months costs more to hold than the same
              price resolving tomorrow.
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
            At a 1¢ tick the pad is a whole cent, which on a cheap contract is larger
            than every real cost combined. Folding it into one number would make the
            headline mostly an artefact of a configuration default, so it is shown
            beside the measured figure on each contract&apos;s own page and excluded
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
        Research and information only. Measuring what a trade costs is not a
        recommendation to make it.
      </p>
    </section>
  );
}
