import { ImageResponse } from "next/og";

import { apiGet, qs, type CostDetail } from "@/lib/api";

/**
 * The share card for a single contract's cost.
 *
 * This is the product's distribution mechanism, not decoration. The finding it
 * carries — a contract quoted at 1c costs 2c to buy one of, because the venue
 * ceils its fee to the whole cent on the whole order — is checkable, surprising
 * to people who trade these venues, and true every day. A link to it was
 * previously rendered by every social platform as a blank rectangle: the root
 * layout declares `summary_large_image` and no image existed anywhere in the app.
 *
 * Numbers are read from the same API the page reads, so a card can never claim a
 * premium the page itself does not show.
 */

export const alt = "What this prediction-market contract actually costs to buy";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/** Contract price in cents, matching the site's own formatting. */
function cents(value: string | null | undefined): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return `${(parsed * 100).toFixed(1)}¢`;
}

function pct(value: string | null | undefined): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return `${(parsed * 100).toFixed(1)}%`;
}

export default async function Image({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const res = await apiGet<CostDetail>(`/cost/${id}${qs({ size: "1" })}`);
  const data = res?.data;
  const entry = data?.priced ? data.requested : null;

  // A card that cannot state the finding states the product instead. Rendering a
  // fabricated or placeholder number here would put a figure into someone's
  // timeline that no page backs up.
  const title = data?.market?.title ?? "Prediction-market execution cost";
  const quoted = entry ? cents(entry.nominal_price) : null;
  const trueCost = entry ? cents(entry.measured_cost) : null;
  const breakEven = entry ? pct(entry.breakeven_probability) : null;

  const INK = "#E9ECF0";
  const MUTED = "#9BA3AF";
  const BG = "#0C0E12";
  const ACCENT = "#7AAEDB";

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: BG,
          padding: "64px 72px",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div
            style={{
              display: "flex",
              fontSize: 22,
              letterSpacing: 2,
              color: MUTED,
              textTransform: "uppercase",
            }}
          >
            PMVL · Execution cost
          </div>
          <div
            style={{
              display: "flex",
              fontSize: title.length > 90 ? 40 : 52,
              lineHeight: 1.15,
              color: INK,
              marginTop: 24,
              maxWidth: 1050,
            }}
          >
            {title.length > 150 ? `${title.slice(0, 150)}…` : title}
          </div>
        </div>

        {quoted && trueCost ? (
          <div style={{ display: "flex", alignItems: "flex-end", gap: 56 }}>
            <Figure label="Quoted" value={quoted} color={MUTED} />
            <div
              style={{ display: "flex", fontSize: 64, color: MUTED, paddingBottom: 8 }}
            >
              →
            </div>
            <Figure label="Actually costs" value={trueCost} color={ACCENT} />
            <Figure label="Break-even" value={breakEven ?? "—"} color={INK} />
          </div>
        ) : (
          <div style={{ display: "flex", fontSize: 32, color: MUTED, maxWidth: 1000 }}>
            What a contract costs after venue fees, the fee-rounding rule, book
            depth, transfer and capital cost.
          </div>
        )}

        <div style={{ display: "flex", fontSize: 22, color: MUTED }}>
          {quoted
            ? "Buying one contract. Measured from observed depth and published fee schedules."
            : "Research only. Not investment advice."}
        </div>
      </div>
    ),
    size,
  );
}

function Figure({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <div style={{ display: "flex", fontSize: 22, color: "#9BA3AF", letterSpacing: 1 }}>
        {label}
      </div>
      <div style={{ display: "flex", fontSize: 92, color, lineHeight: 1.1 }}>
        {value}
      </div>
    </div>
  );
}
