import { describe, expect, it } from "vitest";

import { chromeWebStoreUrl } from "@/lib/extension-distribution";

describe("Chrome Web Store distribution URL", () => {
  it("keeps the developer-mode fallback when no listing exists", () => {
    expect(chromeWebStoreUrl(undefined)).toBeNull();
  });

  it("constructs the listing from a pinned Chrome extension ID", () => {
    expect(
      chromeWebStoreUrl("abcdefghijklmnopabcdefghijklmnop"),
    ).toBe(
      "https://chromewebstore.google.com/detail/pmvl-entry-cost/abcdefghijklmnopabcdefghijklmnop",
    );

    expect(chromeWebStoreUrl("abcdefghijklmnopabcdefghijklmnox")).toBeNull();
    expect(chromeWebStoreUrl("https://chromewebstore.google.com/detail/other/id")).toBeNull();
    expect(chromeWebStoreUrl("abcdefghijklmnop")).toBeNull();
  });
});
