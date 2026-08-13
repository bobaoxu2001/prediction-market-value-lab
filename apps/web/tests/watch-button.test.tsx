// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WatchButton } from "@/components/watch-button";
import { clearWatchlist } from "@/lib/watchlist";

const PROPS = {
  marketId: 42,
  title: "Will it rain?",
  platform: "kalshi",
  side: "yes" as const,
  size: "100",
};

beforeEach(() => {
  localStorage.clear();
  clearWatchlist();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
});

describe("WatchButton funnel event", () => {
  it("records a successful add but not a removal", () => {
    render(<WatchButton {...PROPS} />);

    fireEvent.click(screen.getByRole("button", { name: "Watch" }));
    expect(fetch).toHaveBeenCalledWith(
      "/api/funnel",
      expect.objectContaining({
        body: JSON.stringify({ name: "watchlist_added", source: "web" }),
      }),
    );
    expect(screen.getByRole("button", { name: /watching/i })).not.toBeNull();

    vi.mocked(fetch).mockClear();
    fireEvent.click(screen.getByRole("button", { name: /watching/i }));
    expect(fetch).not.toHaveBeenCalled();
  });
});
