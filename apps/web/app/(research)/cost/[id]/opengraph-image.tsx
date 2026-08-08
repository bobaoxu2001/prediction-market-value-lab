import { ImageResponse } from "next/og";

import { apiGet, qs, type CostDetail } from "@/lib/api";
import { dominantDriver, ladderStrip } from "@/lib/cost-ladder";

/**
 * The share card for one contract's entry cost.
 *
 * Every link to a cost page previously rendered the site-wide card, which states
 * a general claim about rounding. That card is true and it is the same on every
 * link, so a contract shared into a Discord or a thread arrived carrying no
 * information about the contract.
 *
 * This one carries the ladder: the same contract, the same instant, at several
 * order sizes. Neither venue displays that, and it does not depend on any
 * forecast being right — which is the only reason it is safe on a card that will
 * be seen out of context by people who never read the methodology page.
 *
 * Deliberately absent: any probability estimate, edge, or recommendation. A
 * share card reaches exactly the audience least able to check it, and the
 * retrodiction says the models do not beat the price anyway.
 *
 * ## Two things this card must never do, both learned by rendering it
 *
 * **Quote a size the venue would reject.** The first version headlined the
 * requested size, defaulting to one contract. On a Polymarket contract with a
 * five-contract minimum that produced "quoted 0.10¢ → costs 50.1¢, +50010%" — a
 * spectacular, screenshot-ready number for an order that cannot be placed.
 * `below_min_order_size` exists for this and every rung is now filtered through
 * it.
 *
 * **Let a configuration assumption carry the headline unlabelled.** On that same
 * contract the transfer amortisation was 0.0050 of a 0.0061 total: 82% of the
 * "cost" was a config default, not an observation. The README's rule is that the
 * product's central claim must never be an artefact of a config default, and a
 * share card is where that rule matters most, so the dominant driver is named on
 * the card whenever an assumption is the majority of the premium.
 *
 * Next.js does not pass `searchParams` to this route, so the card cannot know
 * which size a link was shared with. That is why it shows the ladder rather than
 * one size: a card that silently rendered a different size from the page it
 * previews would be worse than one that shows the range.
 */

export const alt = "Estimated entry cost for one prediction-market contract";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const INK = "#E9ECF0";
const MUTED = "#9BA3AF";
const BG = "#0C0E12";
const ACCENT = "#7AAEDB";
const WARN = "#E0A458";

function centsOf(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(n * 100 < 10 ? 2 : 1)}¢`;
}

function percentOf(ratio: string | null | undefined): string {
  if (ratio === null || ratio === undefined) return "—";
  const n = Number(ratio);
  if (!Number.isFinite(n)) return "—";
  const pct = n * 100;
  return `${pct >= 0 ? "+" : ""}${pct < 10 ? pct.toFixed(1) : pct.toFixed(0)}%`;
}

function truncate(text: string, limit: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length <= limit ? clean : `${clean.slice(0, limit - 1)}…`;
}

function Fallback({ note }: { note: string }) {
  return (
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
      <div
        style={{
          display: "flex",
          fontSize: 22,
          letterSpacing: 2,
          color: MUTED,
          textTransform: "uppercase",
        }}
      >
        Prediction Market Value Lab
      </div>
      <div style={{ display: "flex", fontSize: 54, color: INK, maxWidth: 1020 }}>
        What entry actually costs, before you trade.
      </div>
      <div style={{ display: "flex", fontSize: 24, color: MUTED }}>{note}</div>
    </div>
  );
}

export default async function Image({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  const res = await apiGet<CostDetail>(`/cost/${id}${qs({ side: "yes" })}`);
  const data = res?.data;

  const strip = data?.priced ? ladderStrip(data.ladder ?? []) : [];

  // A card is rendered by a crawler with no user waiting, so a failure here must
  // degrade to the general claim rather than to a broken image.
  if (!data || !strip.length) {
    return new ImageResponse(
      <Fallback note="Costs are computed from observed depth and published venue rules." />,
      size,
    );
  }

  const contract = truncate(data.market.title ?? "This contract", 92);
  const venue = (data.market.platform ?? "").toUpperCase();
  const smallest = strip[0];
  const driver = dominantDriver(smallest);

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
          padding: "56px 72px",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div
            style={{
              display: "flex",
              fontSize: 20,
              letterSpacing: 2,
              color: MUTED,
              textTransform: "uppercase",
            }}
          >
            {venue ? `${venue} · ` : ""}Estimated entry cost
          </div>
          <div
            style={{
              display: "flex",
              fontSize: 38,
              lineHeight: 1.25,
              color: INK,
              maxWidth: 1040,
            }}
          >
            {contract}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "flex-end",
            gap: 20,
            marginTop: 8,
          }}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <div style={{ display: "flex", fontSize: 22, color: MUTED }}>
              Quoted
            </div>
            <div style={{ display: "flex", fontSize: 64, color: MUTED }}>
              {centsOf(smallest.nominal_price)}
            </div>
          </div>
          <div
            style={{
              display: "flex",
              fontSize: 26,
              color: MUTED,
              paddingBottom: 20,
              marginLeft: 18,
            }}
          >
            costs, per contract
          </div>
        </div>

        <div style={{ display: "flex", gap: 64 }}>
          {strip.map((rung) => (
            <div
              key={rung.size}
              style={{ display: "flex", flexDirection: "column", gap: 4 }}
            >
              <div style={{ display: "flex", fontSize: 22, color: MUTED }}>
                at {rung.size} contract{rung.size === "1" ? "" : "s"}
              </div>
              <div style={{ display: "flex", fontSize: 52, color: ACCENT }}>
                {centsOf(rung.measured_cost)}
              </div>
              <div style={{ display: "flex", fontSize: 30, color: WARN }}>
                {percentOf(rung.measured_premium_ratio)}
              </div>
            </div>
          ))}
        </div>

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            maxWidth: 1050,
          }}
        >
          {driver && driver.assumed && driver.share > 0.5 ? (
            <div style={{ display: "flex", fontSize: 22, color: WARN }}>
              At {smallest.size} contracts this estimate is{" "}
              {driver.share > 0.95
                ? "almost entirely"
                : `${Math.round(driver.share * 100)}%`}{" "}
              {driver.label}, a disclosed configuration input rather than an
              observation.
            </div>
          ) : null}
          <div style={{ display: "flex", fontSize: 20, color: MUTED }}>
            Observed ask depth and published venue fee and rounding rules, with
            transfer and capital assumptions disclosed. Sizes below the venue
            minimum are excluded. Slippage is not in these figures. Research only
            — not advice.
          </div>
        </div>
      </div>
    ),
    size,
  );
}
