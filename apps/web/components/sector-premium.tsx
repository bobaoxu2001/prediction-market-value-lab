import Link from "next/link";

import type { CostByCategory } from "@/lib/api";
import { num, pct } from "@/lib/format";

/**
 * Execution-cost premium by market category.
 *
 * Design decisions worth stating, because each rules out an easier option:
 *
 * **Area, not length.** A bar chart would encode this magnitude more precisely,
 * and for an analytical page it would be the right call. This is the homepage's
 * one visual, where the job is to make a reader notice a comparison at all, and
 * circles do that better. The precision that area encoding loses is handed back
 * by direct-labelling every circle with its exact figure — no reader has to judge
 * a ratio of areas to get the number.
 *
 * **One hue, light → dark.** The quantity is a magnitude, not an identity and not
 * a polarity, so a categorical palette would be wrong (ten hues for ten
 * categories, several indistinguishable under CVD) and a red/green scale would be
 * worse. There is no good end here: every premium is a cost, and a green circle
 * would say a sector is *earning* you something. The ramp steps are validated for
 * monotone lightness and for clearing the surface, in both themes, in
 * `globals.css`.
 *
 * **Sorted by value, always.** Position carries the ranking, so the encoding
 * survives even if a reader cannot separate two adjacent shades.
 */

/** Ramp step for a value's position within the observed range. */
function rampStep(ratio: number, min: number, max: number): number {
  if (max <= min) return 3;
  const t = (ratio - min) / (max - min);
  return Math.min(5, Math.max(1, Math.ceil(t * 5) || 1));
}

const CATEGORY_LABEL: Record<string, string> = {
  politics: "Politics",
  economics: "Economics",
  sports: "Sports",
  crypto: "Crypto",
  weather: "Weather",
  finance: "Finance",
  culture: "Culture",
  tech: "Tech",
  geopolitics: "Geopolitics",
  other: "Other",
};

export function SectorPremium({
  rows,
  size,
}: {
  rows: CostByCategory[];
  size: string;
}) {
  const usable = rows
    .map((row) => ({ row, value: num(row.median_premium_ratio) }))
    .filter((entry): entry is { row: CostByCategory; value: number } =>
      entry.value !== null && entry.value > 0,
    );

  if (usable.length < 2) {
    return (
      <p className="t-body">
        Not enough priced contracts in the current snapshot to compare categories.
        Nothing is shown rather than a comparison drawn from a handful of rows.
      </p>
    );
  }

  const values = usable.map((entry) => entry.value);
  const max = Math.max(...values);
  const min = Math.min(...values);

  // Area proportional to the premium, so a sector twice as expensive draws a
  // circle of twice the area — radius therefore scales with the square root.
  // Encoding it on the radius directly would exaggerate the top of the range by
  // the square of its true ratio, which is the classic bubble-chart lie.
  //
  // No additive floor. An `R_MIN + (R_MAX - R_MIN) * sqrt(...)` ramp keeps small
  // circles comfortably visible and *also* destroys the proportionality this
  // encoding exists for: on the current data it compressed a 4.3x spread in
  // premium into a 2.5x spread in area, understating exactly the difference the
  // section is about. Legibility at the small end is handled by the direct label,
  // which carries the number regardless of the circle's size.
  const R_MAX = 44;
  const R_FLOOR = 4; // below this a circle stops reading as a mark at all
  const radius = (value: number) =>
    Math.max(R_FLOOR, R_MAX * Math.sqrt(value / max));

  // Layout, derived rather than guessed. The previous values put the category
  // label's baseline at y=170 inside a 168-tall viewBox, so every label in the
  // chart was clipped — invisible in the DOM's own numbers until the boxes were
  // measured.
  const CELL = 118;
  const AXIS = 94; // circle centres sit on one line
  const VALUE_GAP = 14; // baseline above the largest circle
  const LABEL_GAP = 20; // baseline below it
  const width = usable.length * CELL;
  const height = AXIS + R_MAX + LABEL_GAP + 14; // + descender and breathing room
  const axis = AXIS;

  return (
    <figure className="mt-6">
      <div className="table-wrap">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          width={width}
          height={height}
          role="img"
          aria-label={`Median execution-cost premium by market category, at ${size} contracts. ${usable
            .map((e) => `${CATEGORY_LABEL[e.row.category] ?? e.row.category} ${pct(e.value, 1)}`)
            .join("; ")}.`}
          className="max-w-none"
        >
          {usable.map((entry, index) => {
            const cx = index * CELL + CELL / 2;
            const r = radius(entry.value);
            const step = rampStep(entry.value, min, max);
            const label = CATEGORY_LABEL[entry.row.category] ?? entry.row.category;
            return (
              <g key={entry.row.category}>
                {/* Native tooltip: an interaction layer that needs no JavaScript
                    and survives with the page as a server component. */}
                <title>
                  {`${label}: median ${pct(entry.value, 1)} premium over the quoted price at ${size} contracts, across ${entry.row.n} contracts.`}
                </title>
                <circle
                  cx={cx}
                  cy={axis}
                  r={r}
                  fill={`rgb(var(--ramp-${step}))`}
                  stroke="rgb(var(--surface-base))"
                  strokeWidth={2}
                />
                <text
                  x={cx}
                  y={axis - R_MAX - VALUE_GAP}
                  textAnchor="middle"
                  className="fill-ink text-[13px] font-semibold"
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {pct(entry.value, 1)}
                </text>
                <text
                  x={cx}
                  y={axis + R_MAX + LABEL_GAP}
                  textAnchor="middle"
                  className="fill-ink-muted text-[11px]"
                >
                  {label}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      <figcaption className="mt-4">
        <p className="t-body max-w-3xl">
          Circle area is the median premium; the figure above each circle is that
          same number exactly. Sorted most expensive first.{" "}
          <strong className="text-ink">
            The categories no model here will forecast — politics, economics,
            sports — are the expensive ones to trade.
          </strong>{" "}
          That is not a coincidence: attention and liquidity are not the same
          thing, and the cost is where the difference shows up.
        </p>
        <DepthCaveat rows={usable.map((entry) => entry.row)} />
      </figcaption>

      <details className="mt-4">
        <summary className="cursor-pointer text-sm text-ink-muted underline decoration-line-strong underline-offset-4 hover:text-ink">
          Show the numbers
        </summary>
        <div className="table-wrap mt-3">
          <table>
            <thead>
              <tr>
                <th scope="col">Category</th>
                <th scope="col" className="num">
                  Contracts
                </th>
                <th scope="col" className="num">
                  25th pct
                </th>
                <th scope="col" className="num">
                  Median
                </th>
                <th scope="col" className="num">
                  75th pct
                </th>
                <th scope="col" className="num">
                  From order book
                </th>
              </tr>
            </thead>
            <tbody>
              {usable.map(({ row, value }) => (
                <tr key={row.category}>
                  <th scope="row" className="cell-title">
                    {CATEGORY_LABEL[row.category] ?? row.category}
                  </th>
                  <td className="num">{row.n}</td>
                  <td className="num">{pct(row.p25_premium_ratio, 1)}</td>
                  <td className="num">{pct(value, 1)}</td>
                  <td className="num">{pct(row.p75_premium_ratio, 1)}</td>
                  <td className="num">{pct(row.depth_coverage, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="t-meta mt-3 max-w-3xl">
          Median rather than mean: the premium distribution has a long right tail,
          and a handful of sub-cent contracts would drag any average to a figure no
          individual contract is near. Sampled from the highest-volume contracts in
          each category.{" "}
          <Link href="/cost" className="underline underline-offset-2">
            Price any single contract
          </Link>
          .
        </p>
      </details>
    </figure>
  );
}

/**
 * How much of the comparison rests on a real ladder.
 *
 * Stated prominently rather than in a footnote, because it cuts the *other* way
 * from the usual caveat: a category priced only from venue summaries has its
 * depth impact excluded, so its true premium is higher than plotted, not lower.
 * A reader who assumed the missing data flattered the expensive sectors would
 * have it backwards.
 */
function DepthCaveat({ rows }: { rows: CostByCategory[] }) {
  const thin = rows.filter((row) => (num(row.depth_coverage) ?? 0) < 0.25);
  if (thin.length === 0) return null;
  return (
    <p className="t-meta mt-3 max-w-3xl">
      Order books have been captured for only part of this snapshot, so{" "}
      {thin.length} of these {rows.length} categories are priced mainly from venue
      summary quotes. That excludes order-book depth impact entirely — meaning
      their true premium is <strong className="text-ink">higher</strong> than
      shown here, not lower. Fees, transfer and capital cost are exact regardless.
    </p>
  );
}
