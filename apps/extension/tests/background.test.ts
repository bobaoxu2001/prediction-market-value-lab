/**
 * The service worker's message boundary.
 *
 * The worker is a fetch proxy for the content script, so its listener is a
 * security boundary: it must answer only its own extension's contexts, only
 * for the allowlisted venue endpoints, and only for well-formed messages.
 */

// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

type Message = { type?: string; url?: string };
type Sender = { id?: string; url?: string };

const VENUE_PAGE = "https://kalshi.com/markets/kxtest/kxtest";
type Response = { ok: boolean; value?: unknown; error?: string };
type Listener = (
  message: Message,
  sender: Sender,
  sendResponse: (response: Response) => void,
) => boolean | void;

const ALLOWED = "https://api.elections.kalshi.com/trade-api/v2/markets/KXTEST";

let listener: Listener;

beforeEach(async () => {
  vi.resetModules();
  listener = () => false;

  const listeners: Listener[] = [];
  const chromeMock = {
    runtime: {
      id: "extension-under-test",
      getURL: (path: string) => "chrome-extension://extension-under-test/" + path,
      onMessage: { addListener: (fn: Listener) => listeners.push(fn) },
      onInstalled: { addListener: () => {} },
    },
    action: { onClicked: { addListener: () => {} } },
    tabs: { create: vi.fn() },
  };
  vi.stubGlobal("chrome", chromeMock);

  await import("../src/background");
  listener = listeners[0] ?? (() => false);
});

function respondTo(message: Message, sender: Sender): Promise<Response> {
  return new Promise((resolve) => {
    const handled = listener(message, sender, (response) => resolve(response));
    if (!handled) resolve({ ok: false, error: "not handled" });
  });
}

describe("background message boundary", () => {
  it("rejects a sender that is not this extension", async () => {
    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { id: "someone-elses-extension", url: VENUE_PAGE },
    );
    expect(response.ok).toBe(false);
    expect(response.error).toContain("not this extension");
  });

  it("rejects a sender with no id", async () => {
    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { url: VENUE_PAGE },
    );
    expect(response.ok).toBe(false);
  });

  it("rejects its own extension from a non-venue page", async () => {
    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { id: "extension-under-test", url: "https://evil.example/steal" },
    );
    expect(response.ok).toBe(false);
    expect(response.error).toContain("not a venue page");
  });

  it("rejects a missing sender URL even from this extension", async () => {
    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { id: "extension-under-test" },
    );
    expect(response.ok).toBe(false);
    expect(response.error).toContain("not a venue page");
  });

  it("rejects a kalshi.com lookalike host", async () => {
    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { id: "extension-under-test", url: "https://kalshi.com.evil.example/trade" },
    );
    expect(response.ok).toBe(false);
    expect(response.error).toContain("not a venue page");
  });

  it("accepts its own content script and fetches an allowlisted endpoint", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ price: "1" }) });
    vi.stubGlobal("fetch", fetchMock);

    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { id: "extension-under-test", url: VENUE_PAGE },
    );
    expect(response.ok).toBe(true);
    expect(response.value).toEqual({ price: "1" });
    expect(fetchMock).toHaveBeenCalledWith(
      ALLOWED,
      expect.objectContaining({ method: "GET", credentials: "omit" }),
    );
  });

  it("accepts a venue subdomain", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ price: "1" }) });
    vi.stubGlobal("fetch", fetchMock);

    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { id: "extension-under-test", url: "https://trading.kalshi.com/markets/x" },
    );
    expect(response.ok).toBe(true);
    expect(response.value).toEqual({ price: "1" });
  });

  it("rejects a disallowed URL even from its own extension", async () => {
    const response = await respondTo(
      { type: "pmvl:fetch", url: "https://evil.example/steal" },
      { id: "extension-under-test", url: VENUE_PAGE },
    );
    expect(response.ok).toBe(false);
    expect(response.error).toContain("not a permitted endpoint");
  });

  it("rejects a same-prefix imposter origin", async () => {
    // startsWith is not a boundary: this must not pass the allowlist.
    const response = await respondTo(
      {
        type: "pmvl:fetch",
        url: "https://api.elections.kalshi.com.evil.example/trade-api/v2/markets/X",
      },
      { id: "extension-under-test", url: VENUE_PAGE },
    );
    expect(response.ok).toBe(false);
    expect(response.error).toContain("not a permitted endpoint");
  });

  it("ignores malformed messages", async () => {
    expect(
      await respondTo({}, { id: "extension-under-test", url: VENUE_PAGE }),
    ).toEqual({
      ok: false,
      error: "not handled",
    });
    expect(
      await respondTo(
        { type: "other" },
        { id: "extension-under-test", url: VENUE_PAGE },
      ),
    ).toEqual({ ok: false, error: "not handled" });
    expect(
      await respondTo(
        { type: "pmvl:fetch", url: 42 as unknown as string },
        { id: "extension-under-test", url: VENUE_PAGE },
      ),
    ).toEqual({ ok: false, error: "not handled" });
  });

  it("reports fetch failures to the caller", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({}) }),
    );
    const response = await respondTo(
      { type: "pmvl:fetch", url: ALLOWED },
      { id: "extension-under-test", url: VENUE_PAGE },
    );
    expect(response.ok).toBe(false);
    expect(response.error).toContain("503");
  });
});
