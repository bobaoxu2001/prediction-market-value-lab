// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("cost contract search", () => {
  it("searches through the API and keeps the selected dataset and filters", async () => {
    const fetcher = vi.fn(async (_input: string | URL | Request) => ({
      ok: true,
      json: async () => ({ data: [], data_mode: "demo", disclaimer: "Research only." }),
    }));
    vi.stubGlobal("fetch", fetcher);
    const { default: CostPage } = await import("@/app/(research)/cost/page");

    render(
      await CostPage({
        searchParams: Promise.resolve({
          q: "Fed September",
          size: "10",
          platform: "kalshi",
          category: "politics",
          mode: "demo",
        }),
      }),
    );

    const requested = new URL(String(fetcher.mock.calls[0][0]));
    expect(requested.pathname).toBe("/cost");
    expect(requested.searchParams.get("q")).toBe("Fed September");
    expect(requested.searchParams.get("mode")).toBe("demo");
    expect(requested.searchParams.get("size")).toBe("10");

    expect(
      (screen.getByRole("searchbox", { name: /find your contract/i }) as HTMLInputElement)
        .value,
    ).toBe("Fed September");
    const sizeGroup = screen.getByRole("group", { name: /order size/i });
    const oneContract = within(sizeGroup).getByRole("link", { name: "1" });
    const nextUrl = new URL(oneContract.getAttribute("href")!, "http://pmvl.local");
    expect(nextUrl.searchParams.get("q")).toBe("Fed September");
    expect(nextUrl.searchParams.get("mode")).toBe("demo");
    expect(nextUrl.searchParams.get("platform")).toBe("kalshi");
  });

  it("makes the live-overlay wedge visible before the snapshot table", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ data: [], data_mode: "live", disclaimer: "Research only." }),
      })),
    );
    const { default: CostPage } = await import("@/app/(research)/cost/page");
    render(await CostPage({ searchParams: Promise.resolve({}) }));

    expect(
      screen.getByRole("link", { name: /get the live overlay/i }).getAttribute("href"),
    ).toBe("/extension");
    expect(screen.getByText(/this table is a hosted snapshot/i)).toBeTruthy();
  });
});
