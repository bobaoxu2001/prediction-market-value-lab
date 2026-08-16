// @vitest-environment jsdom
import { fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FunnelTracker, trackFunnelEvent } from "@/components/funnel-tracker";

const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
  new Response(null, { status: 204 }),
);

beforeEach(() => {
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});

describe("FunnelTracker", () => {
  it("sends an allowlisted CTA event without credentials or page context", async () => {
    const { container } = render(
      <>
        <FunnelTracker />
        <a
          href="/downloads/beta.zip"
          data-pmvl-funnel="extension_install_intent"
          data-pmvl-placement="beta_zip"
        >
          Download
        </a>
      </>,
    );

    fireEvent.click(container.querySelector("a")!);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());

    expect(fetchMock.mock.calls[0][0]).toBe("/api/funnel");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe("omit");
    expect(init.keepalive).toBe(true);
    expect(JSON.parse(String(init.body))).toEqual({
      name: "extension_install_intent",
      source: "web",
      placement: "beta_zip",
    });
  });

  it("ignores unknown DOM attributes", () => {
    const { getByRole } = render(
      <>
        <FunnelTracker />
        <button data-pmvl-funnel="email_collected">Continue</button>
      </>,
    );
    fireEvent.click(getByRole("button"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("lets product code report a successful action directly", () => {
    trackFunnelEvent("result_shared", "clipboard");
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      name: "result_shared",
      source: "web",
      placement: "clipboard",
    });
  });
});
