import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { apiGet, qs, type CostAtSize, type CostDetail } from "@/lib/api";
import {
  cents,
  centsFine,
  compactNumber,
  displayTitle,
  humanizeSeconds,
  num,
  pct,
  usd,
  utcTime,
} from "@/lib/format";
import {
  ApiDown,
  DemoBanner,
  HelpDot,
  Metric,
  PlatformChip,
} from "@/components/ui";
import { WatchButton } from "@/components/watch-button";

export const dynamic = "force-dynamic";

/**
 * Title and description carry the actual finding.
 *
 * A shared link previously read "Contract cost — PMVL" whatever it pointed at,
 * which wastes the one line a reader sees before deciding whether to click. The
 * numbers come from the same request the page makes, so a preview can never
 * state a premium the page does not.
 */
export async function generateMetadata({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}): Promise<Metadata> {
  const { id } = await params;
  const query = await searchParams;
  const raw = query.size;
  const size = sanitiseSize(typeof raw === "string" ? raw : undefined);
  const side = query.side === "no" ? "no" : "yes";

  const res = await apiGet<CostDetail>(`/cost/${id}${qs({ size, side })}`);
  const data = res?.data;
  const entry = data?.priced ? data.requested : null;
  if (!data || !entry) return { title: "Contract cost" };

  const contract = displayTitle(data.market.title);
  const quoted = cents(entry.nominal_price);
  const real = cents(entry.measured_cost);
  const title = `${contract} — quoted ${quoted}, actually costs ${real}`;
  const description =
    `Buying ${size} contract${size === "1" ? "" : "s"} costs ${real} each after the ` +
    `venue's fee, its rounding rule, book depth, transfer and capital cost. ` +
    `Break-even probability ${
      entry.breakeven_probability === null
        ? "is unreachable at this size"
        : pct(entry.breakeven_probability)
    }, not ${pct(entry.nominal_price)}. Research only.`;

  return {
    title,
    description,
    openGraph: { title, description },
    twitter: { card: "summary_large_image", title, description },
  };
}

/**
 * The cost calculator for one contract.
 *
 * Size is carried in the URL rather than held in client state. It makes a result
 * shareable — the interesting findings here are specific sizes on specific
 * contracts — and it keeps the page a server component with no client-side fetch
 * of money values.
 */

const PRESETS = ["1", "5", "10", "25", "50", "100", "250", "1000"];

export default async function CostDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const marketId = Number(id);
  if (!Number.isFinite(marketId)) notFound();

  const raw = query.size;
  const sizeParam = typeof raw === "string" ? raw : undefined;
  const side = query.side === "no" ? "no" : "yes";
  const size = sanitiseSize(sizeParam);

  const res = await apiGet<CostDetail>(
    `/cost/${marketId}${qs({ size, side })}`,
  );
  if (!res) return <ApiDown />;
  const data = res.data;
  if (!data) notFound();

  if (!data.priced) {
    return (
      <>
        <DemoBanner notice={res.demo_notice} />
        <Header data={data} side={side} size={size} />
        <div className="panel mt-6 p-6">
          <p className="t-sub-title">This contract cannot be priced</p>
          <p className="t-body mt-2 max-w-2xl">{data.reason}</p>
        </div>
      </>
    );
  }

  const entry = data.requested;

  return (
    <>
      <DemoBanner notice={res.demo_notice} />
      <Header data={data} side={side} size={size} />

      {entry ? (
        <Headline data={data} entry={entry} size={size} />
      ) : (
        <div className="panel mt-6 p-6">
          <p className="t-body">
            The observed book could not fill {size} contracts on this side.
          </p>
        </div>
      )}

      <SizeControls marketId={data.market.id} current={size} side={side} />

      {entry && <Decomposition entry={entry} data={data} />}

      <Ladder ladder={data.ladder} current={size} marketId={data.market.id} side={side} />

      <Caveats caveats={data.caveats} />
    </>
  );
}

function sanitiseSize(raw: string | undefined): string {
  const parsed = num(raw ?? "");
  if (parsed === null || parsed <= 0) return "100";
  if (parsed > 100000) return "100000";
  // Whole contracts only in the UI; the API accepts fractions but presenting
  // "17.4 contracts" invites a reader to believe they can buy that.
  return String(Math.floor(parsed));
}

/* ------------------------------------------------------------------ header -- */

function Header({
  data,
  side,
  size,
}: {
  data: CostDetail;
  side: string;
  size: string;
}) {
  const market = data.market;
  return (
    <div className="mb-5">
      <div className="flex flex-wrap items-center gap-2">
        <PlatformChip platform={market.platform} />
        <span className="t-meta font-mono">{market.platform_market_id}</span>
        {market.category && <span className="chip bg-sunken text-ink-muted">{market.category}</span>}
      </div>
      <h1 className="t-page-title mt-3">{displayTitle(market.title)}</h1>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <SideToggle marketId={market.id} current={side} size={size} />
        <WatchButton
          marketId={market.id}
          title={market.title}
          platform={market.platform}
          side={side === "no" ? "no" : "yes"}
          size={size}
        />
        <Link
          href={`/market/${market.id}`}
          className="text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
        >
          Full contract analysis
        </Link>
        <Link
          href="/cost"
          className="text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
        >
          All contracts by premium
        </Link>
      </div>
    </div>
  );
}

function SideToggle({
  marketId,
  current,
  size,
}: {
  marketId: number;
  current: string;
  size: string;
}) {
  return (
    <div className="seg" role="group">
      {["yes", "no"].map((side) => (
        <Link
          key={side}
          href={`/cost/${marketId}${qs({ size, side })}`}
          aria-current={side === current ? "page" : undefined}
          className={`seg-item ${side === current ? "seg-item-on" : "hover:text-ink"}`}
        >
          Buy {side.toUpperCase()}
        </Link>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- headline -- */

function Headline({
  data,
  entry,
  size,
}: {
  data: CostDetail;
  entry: CostAtSize;
  size: string;
}) {
  const ratio = num(entry.measured_premium_ratio);
  const tone = ratio === null ? "neutral" : ratio >= 0.5 ? "bad" : ratio >= 0.1 ? "warn" : "neutral";

  return (
    <section className="panel p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <p className="t-label">
          Buying {compactNumber(size)} contract{size === "1" ? "" : "s"}
        </p>
        <FreshnessNote data={data} />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
        <Metric label="Quoted price" value={cents(entry.nominal_price)} />
        <Metric
          label="True cost each"
          value={cents(entry.measured_cost)}
          tone={tone as "neutral" | "warn" | "bad"}
          hint="Observed depth plus the venue's published fee rules. Excludes the modelled slippage pad."
        />
        <Metric
          label="You pay extra"
          value={`+${cents(entry.measured_premium)}${
            ratio !== null ? ` (${pct(ratio, 0)})` : ""
          }`}
          tone={tone as "neutral" | "warn" | "bad"}
        />
        <Metric
          label="Break-even"
          value={
            entry.breakeven_probability === null
              ? "impossible"
              : pct(entry.breakeven_probability)
          }
          hint="A binary contract pays $1, so cost per contract is the probability at which the trade breaks even."
        />
      </div>

      <p className="t-body mt-5 max-w-3xl">
        The screen says <strong>{cents(entry.nominal_price)}</strong>. Filling{" "}
        {compactNumber(entry.filled_size)} contract
        {entry.filled_size === "1" ? "" : "s"} costs{" "}
        <strong>{cents(entry.measured_cost)}</strong> each — a total outlay of{" "}
        <strong>{usd(entry.total_outlay)}</strong>. For this to break even the event
        must occur{" "}
        <strong>
          {entry.breakeven_probability === null
            ? "more often than always, which is why this size cannot break even"
            : pct(entry.breakeven_probability)}
        </strong>{" "}
        of the time, not {pct(entry.nominal_price)}.
      </p>

      {!entry.fully_filled && data.depth_known && (
        <p className="mt-3 rounded-[3px] border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-ink">
          The observed book holds only {compactNumber(entry.filled_size)} contracts on
          this side. Everything above is priced for the part that fills; the rest
          would execute against orders that were never observed.
        </p>
      )}

      {entry.below_min_order_size && (
        <p className="mt-3 rounded-[3px] border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-ink">
          <strong>{data.market.platform === "polymarket" ? "Polymarket" : "This venue"} will not accept an order this small.</strong>{" "}
          The minimum here is {compactNumber(data.market.min_order_size)} contracts.
          The figures above are arithmetically correct and practically unavailable:
          a fixed cost spread over fewer contracts than the venue permits will
          always look extreme. Price a placeable size to compare it with anything.
        </p>
      )}

      {!data.depth_known && (
        <p className="mt-3 rounded-[3px] border border-line bg-sunken px-3 py-2 text-sm text-ink-muted">
          No order book was captured for this contract, so depth impact is unknown
          and excluded — not assumed to be zero. Fees, transfer and capital cost are
          exact regardless, so the figure above is a{" "}
          <strong>floor on the true cost</strong> rather than an estimate of it.
        </p>
      )}
    </section>
  );
}

function FreshnessNote({ data }: { data: CostDetail }) {
  if (data.quote_observed_at === null) {
    return <span className="t-meta">Observation time unknown</span>;
  }
  return (
    <span className={`t-meta ${data.is_stale ? "text-warn" : ""}`}>
      {data.quote_source === "orderbook" ? "Order book" : "Venue summary"} observed{" "}
      {utcTime(data.quote_observed_at)}
      {data.quote_age_seconds !== null && (
        <> · {humanizeSeconds(data.quote_age_seconds)} old</>
      )}
      {data.is_stale && " · stale"}
    </span>
  );
}

/* ----------------------------------------------------------------- controls -- */

function SizeControls({
  marketId,
  current,
  side,
}: {
  marketId: number;
  current: string;
  side: string;
}) {
  return (
    <section className="mt-6">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <div className="flex items-center gap-2">
          <span className="t-label">Size</span>
          <div className="seg" role="group">
            {PRESETS.map((size) => (
              <Link
                key={size}
                href={`/cost/${marketId}${qs({ size, side })}`}
                aria-current={size === current ? "page" : undefined}
                className={`seg-item ${size === current ? "seg-item-on" : "hover:text-ink"}`}
              >
                {size}
              </Link>
            ))}
          </div>
        </div>

        {/* A plain GET form: works without JavaScript, and the resulting URL is
            the shareable artefact. */}
        <form method="get" action={`/cost/${marketId}`} className="flex items-center gap-2">
          <input type="hidden" name="side" value={side} />
          <label htmlFor="size" className="t-label">
            Custom
          </label>
          <input
            id="size"
            name="size"
            type="number"
            min="1"
            max="100000"
            step="1"
            defaultValue={current}
            className="field w-28"
          />
          <button type="submit" className="btn-quiet">
            Price it
          </button>
        </form>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------ decomposition -- */

function Decomposition({
  entry,
  data,
}: {
  entry: CostAtSize;
  data: CostDetail;
}) {
  const parts = entry.measured_components;
  const rows: Array<{ label: string; value: string | null; note: string }> = [
    {
      label: "Quoted price (top of book)",
      value: entry.nominal_price,
      note: "The price on the venue's screen, for one contract.",
    },
    {
      label: "Depth impact",
      value: parts.depth_impact,
      note: data.depth_known
        ? "The extra paid for walking the ask ladder to fill this size."
        : "Unknown: no order book was observed. Excluded rather than assumed zero.",
    },
    {
      label: "Venue fee",
      value: parts.platform_fee,
      note: "The published taker fee at this size, per contract.",
    },
    {
      label: "Fee rounding",
      value: parts.fee_rounding,
      note: "Kalshi ceils the fee to the whole cent on the whole order. This is the part that exists purely because of that rule.",
    },
    {
      label: "Transfer",
      value: parts.transfer_cost,
      note: "Polygon bridge and gas allowance, amortised over the position. Zero on Kalshi.",
    },
    {
      label: "Capital cost",
      value: parts.capital_cost,
      note: "Opportunity cost of the stake being locked until resolution.",
    },
  ];

  return (
    <section className="mt-8">
      <h2 className="t-section-title">
        Where the money goes
        <HelpDot text="Each line is per contract at the size selected above. The rows sum to the true cost." />
      </h2>

      <div className="table-wrap mt-3">
        <table>
          <thead>
            <tr>
              <th scope="col">Component</th>
              <th scope="col" className="num">
                Per contract
              </th>
              <th scope="col">What it is</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <th scope="row" className="cell-title">
                  {row.label}
                </th>
                <td className="num">
                  {row.value === null ? (
                    <span className="text-ink-faint" title="Not observable from the data captured for this contract.">
                      unknown
                    </span>
                  ) : (
                    centsFine(row.value)
                  )}
                </td>
                <td className="text-ink-muted">{row.note}</td>
              </tr>
            ))}
            <tr className="border-t-2 border-line-strong">
              <th scope="row" className="cell-title font-semibold">True cost per contract</th>
              <td className="num font-semibold">{centsFine(entry.measured_cost)}</td>
              <td className="text-ink-muted">
                Everything above, and nothing that was assumed.
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="mt-4 rounded-[3px] border border-line bg-sunken px-4 py-3">
        <p className="t-label">Modelled separately — not in the figure above</p>
        <p className="t-body mt-2 max-w-3xl">
          A slippage pad of <strong>{cents(entry.modelled_slippage)}</strong> stands
          in for market impact between observing this book and reaching it. It is a
          flat assumption of one tick, not a measurement, so it is excluded from the
          break-even figure. Including it would put the all-in cost at{" "}
          <strong>{cents(entry.all_in_cost)}</strong> and the break-even at{" "}
          <strong>
            {entry.breakeven_probability_with_slippage === null
              ? "above 100%"
              : pct(entry.breakeven_probability_with_slippage)}
          </strong>
          .
        </p>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ ladder -- */

function Ladder({
  ladder,
  current,
  marketId,
  side,
}: {
  ladder: CostAtSize[];
  current: string;
  marketId: number;
  side: string;
}) {
  if (ladder.length === 0) return null;

  const ratios = ladder
    .map((entry) => num(entry.measured_premium_ratio))
    .filter((value): value is number => value !== null);
  const maxRatio = ratios.length > 0 ? Math.max(...ratios) : 0;

  return (
    <section className="mt-8">
      <h2 className="t-section-title">Cost by order size</h2>
      <p className="t-body mt-2 max-w-3xl">
        The same contract, priced at every size. Two forces pull in opposite
        directions: fee rounding punishes small orders, and book depth punishes large
        ones. The cheapest size per contract is usually neither end.
      </p>

      <div className="table-wrap mt-3">
        <table>
          <thead>
            <tr>
              <th scope="col" className="num">
                Size
              </th>
              <th scope="col" className="num">
                Fills
              </th>
              <th scope="col" className="num">
                Entry
              </th>
              <th scope="col" className="num">
                True cost
              </th>
              <th scope="col" className="num">
                Premium
              </th>
              <th scope="col">Relative premium</th>
              <th scope="col" className="num">
                Total outlay
              </th>
            </tr>
          </thead>
          <tbody>
            {ladder.map((entry) => {
              const ratio = num(entry.measured_premium_ratio);
              const width =
                ratio !== null && maxRatio > 0
                  ? Math.max(2, (ratio / maxRatio) * 100)
                  : 0;
              const isCurrent = entry.size === current;
              return (
                <tr key={entry.size} className={isCurrent ? "bg-sunken" : undefined}>
                  <th scope="row" className="num font-normal normal-case tracking-normal text-sm text-ink">
                    <Link
                      href={`/cost/${marketId}${qs({ size: entry.size, side })}`}
                      className="hover:underline"
                    >
                      {compactNumber(entry.size)}
                    </Link>
                  </th>
                  <td className="num">
                    {entry.fully_filled ? (
                      compactNumber(entry.filled_size)
                    ) : (
                      <span className="text-warn" title="The observed book could not fill this size.">
                        {compactNumber(entry.filled_size)}
                      </span>
                    )}
                  </td>
                  <td className="num">{cents(entry.entry_price)}</td>
                  <td className="num">{cents(entry.measured_cost)}</td>
                  <td className="num">+{cents(entry.measured_premium)}</td>
                  <td>
                    <div className="flex items-center gap-2">
                      {/* A bar, not a colour scale: premium is always a cost, and
                          length compares magnitudes without implying good/bad. */}
                      <div
                        className="h-1.5 rounded-[1px] bg-ink-faint"
                        style={{ width: `${width}%`, minWidth: width > 0 ? "2px" : 0 }}
                        aria-hidden="true"
                      />
                      <span className="t-meta">
                        {ratio === null ? "—" : pct(ratio, 0)}
                      </span>
                    </div>
                  </td>
                  <td className="num">{usd(entry.total_outlay)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ----------------------------------------------------------------- caveats -- */

function Caveats({ caveats }: { caveats: string[] }) {
  if (caveats.length === 0) return null;
  return (
    <section className="mt-8 border-t border-line pt-5">
      <h2 className="t-section-title">What this measurement does not settle</h2>
      <ul className="mt-3 space-y-2">
        {caveats.map((caveat) => (
          <li key={caveat} className="t-body max-w-3xl">
            {caveat}
          </li>
        ))}
      </ul>
      <p className="t-meta mt-5">
        Research and information only. Measuring what a trade costs is not a
        recommendation to make it, and this platform places no orders.
      </p>
    </section>
  );
}
