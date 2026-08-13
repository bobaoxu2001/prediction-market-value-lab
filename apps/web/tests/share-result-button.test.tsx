// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ShareResultButton } from "@/components/share-result-button";

const PROPS = {
  contract: "Will the Fed cut rates?",
  side: "yes" as const,
  size: "100",
  quoted: "40.0¢",
  estimated: "42.0¢",
  breakeven: "42.0%",
};

function setNavigator(name: "share" | "clipboard", value: unknown) {
  Object.defineProperty(navigator, name, {
    configurable: true,
    value,
  });
}

beforeEach(() => {
  window.history.replaceState({}, "", "/cost/42?size=100&side=yes");
  vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
  setNavigator("share", undefined);
  setNavigator("clipboard", undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ShareResultButton", () => {
  it("uses native sharing with the exact URL-backed result", async () => {
    const share = vi.fn(async (_payload: ShareData) => undefined);
    setNavigator("share", share);
    render(<ShareResultButton {...PROPS} />);

    fireEvent.click(screen.getByRole("button", { name: /share result/i }));

    await waitFor(() => expect(share).toHaveBeenCalledOnce());
    expect(share.mock.calls[0][0]).toMatchObject({
      url: window.location.href,
      title: "Will the Fed cut rates? — PMVL entry cost",
    });
    expect(share.mock.calls[0][0].text).toContain("quoted 40.0¢, estimated entry 42.0¢");
    expect(screen.getByRole("status").textContent).toBe("Result shared.");
    expect(fetch).toHaveBeenCalledWith(
      "/api/funnel",
      expect.objectContaining({
        body: JSON.stringify({
          name: "result_shared",
          source: "web",
          placement: "native_share",
        }),
      }),
    );
  });

  it("copies the result and link when native sharing is unavailable", async () => {
    const writeText = vi.fn(async (_text: string) => undefined);
    setNavigator("clipboard", { writeText });
    render(<ShareResultButton {...PROPS} />);

    fireEvent.click(screen.getByRole("button", { name: /share result/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(writeText.mock.calls[0][0]).toContain(window.location.href);
    expect(screen.getByRole("status").textContent).toBe("Result and link copied.");
    expect(fetch).toHaveBeenCalledWith(
      "/api/funnel",
      expect.objectContaining({
        body: JSON.stringify({
          name: "result_shared",
          source: "web",
          placement: "clipboard",
        }),
      }),
    );
  });

  it("leaves an accessible manual fallback when copying fails", async () => {
    const writeText = vi.fn(async (_text: string) => {
      throw new Error("denied");
    });
    setNavigator("clipboard", { writeText });
    render(<ShareResultButton {...PROPS} />);

    fireEvent.click(screen.getByRole("button", { name: /share result/i }));

    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toMatch(/copy this page/i),
    );
    expect(fetch).not.toHaveBeenCalled();
  });
});
