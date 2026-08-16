/**
 * The content script's two failure modes that matter most.
 *
 * Side detection must fail closed: no confident side means no numbers, never a
 * guessed YES. And overlapping reloads must be last-request-wins: a slow
 * response for a previous contract must never overwrite the panel showing the
 * contract the trader is looking at now.
 */

// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type SendMessage = (message: { type: string; url: string }) => Promise<unknown>;

const PANEL_ID = "pmvl-cost-overlay";

let sendMock: ReturnType<typeof vi.fn>;
let content: typeof import("../src/content");

function makeVisible(root: ParentNode): void {
  for (const input of Array.from(root.querySelectorAll("input"))) {
    input.getBoundingClientRect = () => ({ width: 120, height: 32 }) as DOMRect;
  }
}

beforeEach(async () => {
  vi.resetModules();
  document.body.innerHTML = "";
  sendMock = vi.fn(async () => ({ ok: false, error: "unmocked" }));
  const chromeMock = {
    runtime: {
      id: "test-extension",
      getURL: (path: string) => "chrome-extension://test-extension/" + path,
      sendMessage: ((message: { type: string; url: string }) =>
        sendMock(message)) as SendMessage,
    },
  };
  vi.stubGlobal("chrome", chromeMock);
  content = await import("../src/content");
});

afterEach(() => {
  window.clearInterval(content.refreshTimer);
  document.getElementById(PANEL_ID)?.remove();
});

describe("detectSide fails closed", () => {
  it("returns null when neither the DOM nor the URL names a side", () => {
    expect(content.detectSide("https://kalshi.com/trade/KXTEST")).toBeNull();
    expect(content.detectSide("https://polymarket.com/market/slug")).toBeNull();
  });

  it("reads an explicit URL side", () => {
    expect(content.detectSide("https://kalshi.com/trade/KXTEST?side=no")).toBe("no");
    expect(content.detectSide("https://polymarket.com/market/slug?outcome=yes")).toBe(
      "yes",
    );
  });

  it("prefers the DOM toggle over the URL", () => {
    document.body.innerHTML =
      "<input placeholder=\"0\">" +
      "<button aria-pressed=\"false\">YES 62¢</button>" +
      "<button aria-pressed=\"true\">NO 39¢</button>";
    makeVisible(document);
    expect(content.detectSide("https://kalshi.com/trade/KXTEST?side=yes")).toBe("no");
  });

  it("a reload with no detectable side shows an explicit unknown state and fetches nothing", async () => {
    await content.reload();
    const node = document.getElementById(PANEL_ID);
    expect(node).not.toBeNull();
    expect(node?.textContent ?? "").toContain("Cannot determine whether this ticket is YES or NO");
    expect(node?.textContent ?? "").not.toContain("Break-even");
    expect(sendMock).not.toHaveBeenCalled();
  });

  it("malformed side toggles do not become a YES guess", () => {
    document.body.innerHTML =
      "<input placeholder=\"0\">" +
      "<button aria-pressed=\"true\">Buy</button>" +
      "<button aria-pressed=\"false\">Sell</button>";
    makeVisible(document);
    expect(content.detectSide("https://kalshi.com/trade/KXTEST")).toBeNull();
  });

  it("two selected toggles fail closed instead of picking YES", () => {
    document.body.innerHTML =
      "<input placeholder=\"0\">" +
      "<button aria-pressed=\"true\">YES 62¢</button>" +
      "<button aria-pressed=\"true\">NO 39¢</button>";
    makeVisible(document);
    // No URL hint either: a contradictory ticket must not become YES.
    expect(content.detectSide("https://kalshi.com/trade/KXTEST")).toBeNull();
  });
});

describe("reload is last-request-wins", () => {
  function marketPayload(token: string, question: string) {
    return [
      {
        id: "m-" + token,
        question,
        clobTokenIds: JSON.stringify([token, token + "-NO"]),
        fee: "0",
        orderPriceMinTickSize: "0.01",
        endDate: "2027-01-01T00:00:00Z",
      },
    ];
  }

  const BOOK_B = {
    asks: [{ price: "0.60", size: "1000" }],
    min_order_size: "1",
    tick_size: "0.01",
  };

  it("a slow response for an old contract cannot overwrite a newer panel", async () => {
    let resolveStale!: (value: unknown) => void;
    const stalePending = new Promise((resolve) => {
      resolveStale = resolve;
    });
    let stalePhase = "pending";

    sendMock.mockImplementation((message: { type: string; url: string }) => {
      const url = message.url as string;
      if (url.includes("slug=stale") && stalePhase === "pending") {
        return stalePending.then((value) => ({ ok: true, value }));
      }
      if (url.includes("slug=stale")) {
        return Promise.resolve({ ok: true, value: marketPayload("TOKENA", "STALE") });
      }
      if (url.includes("slug=fresh")) {
        return Promise.resolve({ ok: true, value: marketPayload("TOKENB", "FRESH") });
      }
      if (url.includes("token_id=TOKENA")) {
        // The stale contract's book is EMPTY: if the stale reload ever drew,
        // the panel would show the "no book" message.
        return Promise.resolve({ ok: true, value: { asks: [] } });
      }
      if (url.includes("token_id=TOKENB")) {
        return Promise.resolve({ ok: true, value: BOOK_B });
      }
      return Promise.resolve({ ok: false, error: "unexpected request " + url });
    });

    window.history.pushState({}, "", "https://polymarket.com/market/stale?side=yes");
    const slow = content.reload(); // pending on the stale market fetch

    window.history.pushState({}, "", "https://polymarket.com/market/fresh?side=yes");
    await content.reload(); // completes and draws the fresh contract

    const panelBefore = document.getElementById(PANEL_ID);
    // jsdom does not implement innerText; textContent is the portable read.
    expect(panelBefore?.textContent ?? "").toContain("Break-even");

    // The stale response lands AFTER the fresh panel is on screen.
    stalePhase = "resolved";
    resolveStale?.(marketPayload("TOKENA", "STALE"));
    await slow;

    const panelAfter = document.getElementById(PANEL_ID);
    expect(panelAfter?.textContent ?? "").toContain("Break-even");
    expect(panelAfter?.textContent ?? "").not.toContain("returned no");
  });
});
