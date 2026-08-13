// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SnapshotBanner } from "@/components/ui";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-08-13T12:00:00Z"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("research freshness banner", () => {
  it("blocks a live-labelled surface from looking current when ingestion is stale", () => {
    render(
      <SnapshotBanner
        active={false}
        staleLiveData
        latestQuoteAt="2026-08-13T10:00:00Z"
      />,
    );

    expect(screen.getByRole("status").textContent).toMatch(/recorded data is stale/i);
    expect(screen.getByRole("status").textContent).toMatch(/treat every research surface as historical/i);
  });

  it("stays out of the way when a live pipeline is actually fresh", () => {
    const { container } = render(
      <SnapshotBanner
        active={false}
        staleLiveData={false}
        latestQuoteAt="2026-08-13T11:45:00Z"
      />,
    );

    expect(container.textContent).toBe("");
  });

  it("always labels a frozen snapshot", () => {
    render(<SnapshotBanner active latestQuoteAt="2026-08-13T11:59:00Z" />);

    expect(screen.getByRole("status").textContent).toMatch(/research snapshot/i);
  });
});
