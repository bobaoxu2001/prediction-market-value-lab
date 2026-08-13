// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModeNav } from "@/components/mode-nav";
import { MarketLink } from "@/components/ui";
import {
  normalizeResearchMode,
  withResearchMode,
} from "@/lib/research-mode";

const navigation = vi.hoisted(() => ({ pathname: "/markets", search: "" }));

vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

afterEach(() => {
  cleanup();
  navigation.pathname = "/markets";
  navigation.search = "";
  vi.unstubAllGlobals();
});

const MARKET = {
  id: 42,
  platform: "kalshi",
  platform_market_id: "DEMO-42",
  title: "Will the demo retain its dataset?",
  subtitle: "",
  category: "technology",
  status: "open",
  accepting_orders: true,
  best_yes_bid: "0.40",
  best_yes_ask: "0.42",
  best_no_bid: "0.57",
  best_no_ask: "0.59",
  spread: "0.02",
  orderbook_depth_usd: "1000",
  volume_24h: "5000",
  total_volume: "20000",
  open_interest: "3000",
  last_trade_price: "0.41",
  tick_size: "0.01",
  fee_rate: "0.01",
  close_time: null,
  expected_resolution_time: null,
  horizon: "30d",
  quote_observed_at: null,
  result: null,
  provenance: "synthetic",
};

function mockMarketsApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const url = new URL(String(input));
      const body = url.pathname === "/markets/categories"
        ? { data: [] }
        : url.pathname === "/markets"
          ? { data: [MARKET], total: 1, disclaimer: "Research only." }
          : url.pathname === "/system"
            ? { data: { snapshot_mode: false } }
            : { data: [] };
      return { ok: true, json: async () => body };
    }),
  );
}

describe("research mode URLs", () => {
  it("accepts only the explicit demo mode", () => {
    expect(normalizeResearchMode("demo")).toBe("demo");
    expect(normalizeResearchMode("live")).toBe("live");
    expect(normalizeResearchMode("all")).toBe("live");
    expect(normalizeResearchMode(undefined)).toBe("live");
  });

  it("keeps demo mode with existing filters and fragments", () => {
    expect(withResearchMode("/market/42?side=yes#evidence", "demo")).toBe(
      "/market/42?side=yes&mode=demo#evidence",
    );
  });

  it("does not add live mode and removes a stale mode parameter", () => {
    expect(withResearchMode("/market/42", "live")).toBe("/market/42");
    expect(withResearchMode("/markets?q=OpenAI&mode=demo", "live")).toBe(
      "/markets?q=OpenAI",
    );
  });

  it("renders market deep links in demo without changing live URLs", () => {
    const demo = render(
      <MarketLink id={42} mode="demo">
        Demo contract
      </MarketLink>,
    );
    expect(demo.getByRole("link").getAttribute("href")).toBe(
      "/market/42?mode=demo",
    );
    demo.unmount();

    const live = render(
      <MarketLink id={42} mode="live">
        Live contract
      </MarketLink>,
    );
    expect(live.getByRole("link").getAttribute("href")).toBe("/market/42");
  });

  it("carries demo through research navigation but keeps live navigation clean", () => {
    const items = [
      { href: "/markets", label: "Markets" },
      { href: "/track-record", label: "Track record" },
    ] as const;
    navigation.search = "mode=demo";
    const demo = render(<ModeNav items={items} />);
    expect(
      [...demo.container.querySelectorAll("a")].map((link) => link.getAttribute("href")),
    ).toEqual(["/markets?mode=demo", "/track-record?mode=demo"]);
    demo.unmount();

    navigation.search = "";
    const live = render(<ModeNav items={items} />);
    expect(
      [...live.container.querySelectorAll("a")].map((link) => link.getAttribute("href")),
    ).toEqual(["/markets", "/track-record"]);
  });

  it("keeps every market-browser deep link on its selected dataset", async () => {
    mockMarketsApi();
    const { default: MarketsPage } = await import("@/app/(research)/markets/page");

    const demo = render(
      await MarketsPage({ searchParams: Promise.resolve({ mode: "demo" }) }),
    );
    const demoMarketLinks = [
      ...demo.container.querySelectorAll('a[href^="/market/42"]'),
    ];
    expect(demoMarketLinks.length).toBeGreaterThan(1);
    expect(
      demoMarketLinks.every(
        (link) => link.getAttribute("href") === "/market/42?mode=demo",
      ),
    ).toBe(true);
    demo.unmount();

    const live = render(
      await MarketsPage({ searchParams: Promise.resolve({ mode: "live" }) }),
    );
    const liveMarketLinks = [
      ...live.container.querySelectorAll('a[href^="/market/42"]'),
    ];
    expect(liveMarketLinks.length).toBeGreaterThan(1);
    expect(
      liveMarketLinks.every((link) => link.getAttribute("href") === "/market/42"),
    ).toBe(true);
  });
});
