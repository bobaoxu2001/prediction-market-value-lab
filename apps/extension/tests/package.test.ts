/**
 * The packaged extension, checked without a browser.
 *
 * Every other suite here imports `src/` directly, so all of them passed while
 * the *bundle* was unloadable: `dist/content.js` was built as ESM and ended with
 * `export { draw, refreshTimer, reload };`. A MV3 content script is a classic
 * script with no way to mark it as a module, so that line is a SyntaxError, the
 * script never parsed, and the overlay never appeared in a real browser.
 *
 * Nothing in the source could have revealed that. These assertions are about the
 * artefact Chrome actually loads, and they are cheap enough to run every time —
 * unlike `scripts/verify-loaded.mjs`, which needs a browser that will accept an
 * unpacked extension.
 */

import { execFileSync, execSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, describe, expect, it } from "vitest";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (file: string) => readFileSync(join(ROOT, file), "utf8");

beforeAll(() => {
  // Assert against a current build, not whatever happened to be lying around.
  execSync("node scripts/bundle.mjs", { cwd: ROOT, stdio: "ignore" });
});

interface Manifest {
  manifest_version: number;
  version: string;
  background?: { service_worker?: string; type?: string };
  content_scripts?: Array<{ js?: string[]; css?: string[]; matches?: string[] }>;
  permissions?: string[];
  host_permissions?: string[];
  icons?: Record<string, string>;
  action?: { default_title?: string; default_icon?: Record<string, string> };
}

function manifest(): Manifest {
  return JSON.parse(read("manifest.json")) as Manifest;
}

const PUBLIC_ZIP = join(ROOT, "..", "web", "public", "downloads", "pmvl-entry-cost-beta.zip");
const EXPECTED_ZIP_FILES = [
  "README.md",
  "dist/background.js",
  "dist/content.js",
  "icons/icon128.png",
  "icons/icon16.png",
  "icons/icon32.png",
  "icons/icon48.png",
  "manifest.json",
  "onboarding.css",
  "onboarding.html",
  "overlay.css",
];

describe("the manifest points at files that exist", () => {
  it("references a real service worker and content script", () => {
    const m = manifest();
    const referenced = [
      m.background?.service_worker,
      ...(m.content_scripts?.[0]?.js ?? []),
      ...(m.content_scripts?.[0]?.css ?? []),
    ].filter((f): f is string => Boolean(f));

    expect(referenced.length).toBeGreaterThan(2);
    for (const file of referenced) {
      expect(existsSync(join(ROOT, file)), `${file} is missing`).toBe(true);
    }
  });

  it("is manifest v3", () => {
    expect(manifest().manifest_version).toBe(3);
  });

  it("ships store-ready icons at every Chrome-required size", () => {
    const m = manifest();
    expect(m.icons).toEqual({
      "16": "icons/icon16.png",
      "32": "icons/icon32.png",
      "48": "icons/icon48.png",
      "128": "icons/icon128.png",
    });
    expect(m.action?.default_icon).toEqual({
      "16": "icons/icon16.png",
      "32": "icons/icon32.png",
    });
    for (const file of Object.values(m.icons ?? {})) {
      expect(existsSync(join(ROOT, file)), `${file} is missing`).toBe(true);
    }
  });

  it("ships the first-run guide opened by the service worker", () => {
    expect(existsSync(join(ROOT, "onboarding.html"))).toBe(true);
    expect(existsSync(join(ROOT, "onboarding.css"))).toBe(true);

    const html = read("onboarding.html");
    expect(html).toContain('href="onboarding.css"');
    expect(html).toMatch(/your first live result/i);
    expect(html).toMatch(/does not place an order/i);

    const worker = read("dist/background.js");
    expect(worker).toContain("onInstalled");
    expect(worker).toContain("onboarding.html");
    expect(manifest().action?.default_title).toMatch(/setup and help/i);
  });
});

describe("the public download is the deterministic package under test", () => {
  it("contains exactly the reviewed files and reproduces byte-for-byte", () => {
    execFileSync("python3", ["scripts/package.py"], { cwd: ROOT, stdio: "ignore" });
    const first = readFileSync(PUBLIC_ZIP);
    const entries = execFileSync("unzip", ["-Z1", PUBLIC_ZIP], {
      encoding: "utf8",
    }).trim().split("\n").sort();

    expect(entries).toEqual(EXPECTED_ZIP_FILES);
    const packagedManifest = JSON.parse(
      execFileSync("unzip", ["-p", PUBLIC_ZIP, "manifest.json"], {
        encoding: "utf8",
      }),
    ) as Manifest;
    expect(packagedManifest.version).toBe(manifest().version);

    execFileSync("python3", ["scripts/package.py"], { cwd: ROOT, stdio: "ignore" });
    const second = readFileSync(PUBLIC_ZIP);
    expect(createHash("sha256").update(second).digest("hex")).toBe(
      createHash("sha256").update(first).digest("hex"),
    );
  });
});

describe("the content script is a classic script", () => {
  it("has no top-level import or export", () => {
    // The bug. `export { … }` at the end of a content script is a SyntaxError,
    // and Chrome reports nothing on the page — the script simply never runs.
    const bundle = read("dist/content.js");
    const offenders = bundle
      .split("\n")
      .map((line, i) => [i + 1, line] as const)
      .filter(([, line]) => /^\s*(export|import)\s/.test(line));

    expect(
      offenders.map(([n, l]) => `${n}: ${l.trim().slice(0, 60)}`),
      "content scripts cannot be ES modules",
    ).toEqual([]);
  });

  it("is wrapped so it cannot leak names onto the venue's page", () => {
    // An IIFE build also keeps the extension's identifiers out of the host
    // page's global scope, which matters when injecting into someone else's app.
    // esbuild emits a `"use strict";` directive prologue ahead of the wrapper.
    const body = read("dist/content.js")
      .replace(/^\s*(["']use strict["'];?\s*)*/, "");
    expect(
      body.startsWith("(()") || body.startsWith("(function"),
      `expected an IIFE, got: ${body.slice(0, 40)}`,
    ).toBe(true);
  });
});

describe("the service worker is a module, as the manifest declares", () => {
  it("says type: module", () => {
    expect(manifest().background?.type).toBe("module");
  });

  it("does not reference chrome APIs the manifest has not asked for", () => {
    // Runtime messaging is a base extension API. Anything beyond it would need a
    // manifest grant and should fail this narrow-permission check first.
    const worker = read("dist/background.js");
    const used = [...worker.matchAll(/chrome\.(\w+)/g)].map((m) => m[1]);
    // `tabs.create` does not require the broad `tabs` manifest permission; it
    // only opens our packaged onboarding page. `action` is likewise available
    // because the manifest declares a toolbar action.
    expect([...new Set(used)].sort()).toEqual(["action", "runtime", "tabs"]);
  });
});

describe("network access is declared and narrow", () => {
  it("requests no general browser permissions", () => {
    // Runtime messaging is available to the extension without a manifest grant.
    // The previous `storage` permission was unused and needlessly widened the
    // install prompt for a product whose trust claim is that it only reads books.
    expect(manifest().permissions ?? []).toEqual([]);
  });

  it("permits exactly the three read-only venue endpoints", () => {
    expect(manifest().host_permissions?.sort()).toEqual([
      "https://api.elections.kalshi.com/*",
      "https://clob.polymarket.com/*",
      "https://gamma-api.polymarket.com/*",
    ]);
  });

  it("only runs on the two venues", () => {
    const matches = manifest().content_scripts?.[0]?.matches ?? [];
    expect(matches.length).toBeGreaterThan(0);
    for (const pattern of matches) {
      expect(pattern).toMatch(/^https:\/\/(\*\.)?(kalshi|polymarket)\.com\/\*$/);
    }
  });

  it("keeps the worker's allowlist in step with the manifest", () => {
    // Two lists that must agree: the manifest grants the permission, the worker
    // enforces the allowlist. If they drift, either the worker blocks a call the
    // extension needs or it forwards one the manifest never sanctioned.
    const worker = read("dist/background.js");
    for (const host of manifest().host_permissions ?? []) {
      const origin = host.replace(/\/\*$/, "");
      expect(worker.includes(origin), `${origin} missing from the worker`).toBe(true);
    }
  });
});
