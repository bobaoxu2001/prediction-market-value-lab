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
  "PMVL — the price on the screen is not what a prediction-market contract costs";
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
            The price on the screen is not what the contract costs.
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
            Buy a contract quoted at{" "}
            <span style={{ color: ACCENT, margin: "0 10px" }}>1¢</span> on Kalshi
            and the fee alone doubles what you pay.
          </div>
        </div>

        <div style={{ display: "flex", fontSize: 22, color: MUTED }}>
          Fees, rounding, book depth, transfer and capital cost — measured, for every
          market with a quote. Research only.
        </div>
      </div>
    ),
    size,
  );
}
