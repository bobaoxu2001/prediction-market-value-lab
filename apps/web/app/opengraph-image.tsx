import { ImageResponse } from "next/og";

/**
 * The site-wide share card.
 *
 * The root layout has declared `summary_large_image` since the public site was
 * added, with no image anywhere in the app to satisfy it — so every link to
 * PMVL, from anywhere, rendered as a blank rectangle.
 *
 * It carries the one claim that is true every day, needs no model, and is
 * checkable against a published fee schedule. Deliberately not a performance
 * figure: there is no live track record to quote, and a share card is the last
 * place to start implying one.
 */

export const alt =
  "PMVL — the quoted price is only the first input to an entry-cost estimate";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
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

        <div style={{ display: "flex", flexDirection: "column" }}>
          <div
            style={{
              display: "flex",
              fontSize: 62,
              lineHeight: 1.15,
              color: INK,
              maxWidth: 1020,
            }}
          >
            The quoted price is only the first input.
          </div>
          <div
            style={{
              display: "flex",
              fontSize: 30,
              lineHeight: 1.4,
              color: MUTED,
              marginTop: 28,
              maxWidth: 980,
            }}
          >
            On a one-lot Kalshi order quoted at{" "}
            <span style={{ color: ACCENT, margin: "0 10px" }}>1¢</span>, the
            published rounding rule adds a 1¢ venue fee.
          </div>
        </div>

        <div style={{ display: "flex", fontSize: 22, color: MUTED }}>
          Observed depth and venue rules, with transfer and capital assumptions
          disclosed separately. Research only.
        </div>
      </div>
    ),
    size,
  );
}
