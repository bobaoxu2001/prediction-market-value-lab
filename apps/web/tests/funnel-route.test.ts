import { NextRequest } from "next/server";
import { describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/funnel/route";

function post(body: unknown, headers: Record<string, string> = {}): NextRequest {
  return new NextRequest("http://localhost:3000/api/funnel", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "sec-fetch-site": "same-origin",
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

describe("anonymous funnel route", () => {
  it("rejects cross-site and non-JSON submissions", async () => {
    expect(
      (await POST(post({ name: "watchlist_added", source: "web" }, {
        "sec-fetch-site": "cross-site",
      }))).status,
    ).toBe(403);
    expect(
      (await POST(post({ name: "watchlist_added", source: "web" }, {
        "content-type": "text/plain",
      }))).status,
    ).toBe(415);
  });

  it("records only the allowlisted aggregate dimensions", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const response = await POST(
      post({ name: "extension_install_intent", source: "web", placement: "beta_zip" }),
    );

    expect(response.status).toBe(204);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const logged = JSON.parse(String(info.mock.calls[0][0])) as Record<string, unknown>;
    expect(logged).toMatchObject({
      level: "info",
      event: "funnel.extension_install_intent",
      source: "web",
      placement: "beta_zip",
    });
    expect(logged.recordedAt).toEqual(expect.any(String));
    for (const forbidden of [
      "ip",
      "url",
      "referrer",
      "userAgent",
      "cookie",
      "visitorId",
      "marketId",
      "orderSize",
      "email",
    ]) {
      expect(logged).not.toHaveProperty(forbidden);
    }
  });

  it("rejects malformed, unknown and context-bearing events without logging", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const invalid = [
      { name: "page_view", source: "web" },
      { name: "watchlist_added", source: "web", marketId: 42 },
      { name: "result_shared", source: "web", placement: "unknown" },
    ];

    for (const event of invalid) {
      expect((await POST(post(event))).status).toBe(400);
    }
    expect(info).not.toHaveBeenCalled();
  });

  it("bounds payload size before parsing", async () => {
    const response = await POST(
      post(
        { name: "watchlist_added", source: "web" },
        { "content-length": "257" },
      ),
    );
    expect(response.status).toBe(413);
  });

  it("bounds the actual body when content-length is absent", async () => {
    const headers = new Headers({
      "content-type": "application/json",
      "sec-fetch-site": "same-origin",
    });
    headers.delete("content-length");
    const request = new NextRequest("http://localhost:3000/api/funnel", {
      method: "POST",
      headers,
      body: JSON.stringify({
        name: "watchlist_added",
        source: "web",
        padding: "x".repeat(300),
      }),
    });
    expect((await POST(request)).status).toBe(413);
  });

  it("does not let one anonymous visitor suppress aggregate events", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const responses = await Promise.all(
      Array.from({ length: 121 }, () =>
        POST(post({ name: "watchlist_added", source: "web" })),
      ),
    );
    expect(responses.every((response) => response.status === 204)).toBe(true);
    expect(info).toHaveBeenCalledTimes(121);
  });
});
