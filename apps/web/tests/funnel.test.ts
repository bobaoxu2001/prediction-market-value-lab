import { describe, expect, it } from "vitest";

import { parseFunnelEvent } from "@/lib/funnel";

describe("parseFunnelEvent", () => {
  it("accepts the closed event and placement vocabulary", () => {
    expect(
      parseFunnelEvent({
        name: "extension_install_intent",
        source: "web",
        placement: "beta_zip",
      }),
    ).toEqual({
      name: "extension_install_intent",
      source: "web",
      placement: "beta_zip",
    });
  });

  it("rejects unknown names, placements and sources", () => {
    expect(parseFunnelEvent({ name: "page_view", source: "web" })).toBeNull();
    expect(
      parseFunnelEvent({ name: "result_shared", source: "web", placement: "twitter" }),
    ).toBeNull();
    expect(parseFunnelEvent({ name: "result_shared", source: "extension" })).toBeNull();
    expect(
      parseFunnelEvent({ name: "founding_offer_intent", source: "web", placement: "beta_zip" }),
    ).toBeNull();
    expect(
      parseFunnelEvent({ name: "extension_install_intent", source: "web" }),
    ).toBeNull();
    expect(
      parseFunnelEvent({ name: "watchlist_added", source: "web", placement: "pricing" }),
    ).toBeNull();
  });

  it("rejects extra context instead of accepting accidental personal data", () => {
    for (const extra of [
      { url: "https://pmvl.example/cost/42" },
      { marketId: 42 },
      { orderSize: 100 },
      { email: "a@example.com" },
      { visitorId: "persistent-id" },
    ]) {
      expect(parseFunnelEvent({ name: "watchlist_added", source: "web", ...extra })).toBeNull();
    }
  });
});
