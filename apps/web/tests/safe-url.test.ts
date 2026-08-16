import { describe, expect, it } from "vitest";

import { safeExternalUrl } from "@/lib/safe-url";

describe("safeExternalUrl", () => {
  it("accepts https", () => {
    expect(safeExternalUrl("https://www.reuters.com/a")).toBe(
      "https://www.reuters.com/a",
    );
  });

  it("accepts http", () => {
    expect(safeExternalUrl("http://example.org/research")).toBe(
      "http://example.org/research",
    );
  });

  it("rejects javascript: URLs", () => {
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("JavaScript:alert(1)")).toBeNull();
    expect(safeExternalUrl(" javascript:alert(1)")).toBeNull();
  });

  it("rejects data: URLs", () => {
    expect(safeExternalUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
  });

  it("rejects other executable or exotic schemes", () => {
    expect(safeExternalUrl("vbscript:x")).toBeNull();
    expect(safeExternalUrl("file:///etc/passwd")).toBeNull();
    expect(safeExternalUrl("chrome://settings")).toBeNull();
  });

  it("rejects malformed URLs", () => {
    expect(safeExternalUrl("not a url")).toBeNull();
    expect(safeExternalUrl("https://")).toBeNull();
  });

  it("rejects relative URLs (these are not external links)", () => {
    expect(safeExternalUrl("/system")).toBeNull();
    expect(safeExternalUrl("//evil.example")).toBeNull();
  });

  it("returns null for empty input", () => {
    expect(safeExternalUrl("")).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl(undefined)).toBeNull();
    expect(safeExternalUrl("   ")).toBeNull();
  });
});
