import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import {
  isAlwaysOpenPath,
  isProtectedPath,
  PUBLIC_RESEARCH_PATHS,
} from "@/lib/route-policy";

const ROOT = path.resolve(__dirname, "..");

function walk(dir: string, filter: (file: string) => boolean): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry === ".next" || entry === ".vercel") continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full, filter));
    else if (filter(full)) out.push(full);
  }
  return out;
}

const SOURCE_DIRS = ["app", "components", "lib"].map((dir) => path.join(ROOT, dir));

const SOURCE_FILES = SOURCE_DIRS.flatMap((dir) =>
  walk(dir, (file) => /\.tsx?$/.test(file)),
).map((file) => ({ file, relative: path.relative(ROOT, file), text: readFileSync(file, "utf8") }));

/** Modules that hold or reach a secret and must never enter a client bundle. */
const SERVER_ONLY_MODULES = [
  "lib/auth-server.ts",
  "lib/billing/config.ts",
  "lib/billing/entitlement.ts",
  "lib/billing/stripe.ts",
  "lib/billing/urls.ts",
  "lib/http.ts",
];

describe("server/client boundary", () => {
  it("marks every secret-holding module as server-only", () => {
    // The real protection: importing one of these into a client component is a
    // BUILD failure rather than a runtime credential leak.
    for (const relative of SERVER_ONLY_MODULES) {
      const text = readFileSync(path.join(ROOT, relative), "utf8");
      expect(text.startsWith('import "server-only";'), `${relative} must be server-only`).toBe(
        true,
      );
    }
  });

  it("never imports a server-only module from a client component", () => {
    const offenders: string[] = [];
    for (const { relative, text } of SOURCE_FILES) {
      if (!/^\s*["']use client["']/m.test(text)) continue;
      for (const serverModule of SERVER_ONLY_MODULES) {
        const specifier = `@/${serverModule.replace(/\.tsx?$/, "")}`;
        if (text.includes(specifier)) offenders.push(`${relative} -> ${specifier}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("never references a secret environment variable outside a server-only module", () => {
    // A `process.env.STRIPE_SECRET_KEY` in a client component is inlined into
    // the browser bundle verbatim.
    const secrets = [
      "STRIPE_SECRET_KEY",
      "STRIPE_WEBHOOK_SECRET",
      "CLERK_SECRET_KEY",
      "STRIPE_PRO_MONTHLY_PRICE_ID",
      "STRIPE_PRO_ANNUAL_PRICE_ID",
    ];
    const allowed = new Set([...SERVER_ONLY_MODULES, "proxy.ts"]);
    const offenders: string[] = [];
    for (const { relative, text } of SOURCE_FILES) {
      if (allowed.has(relative)) continue;
      for (const secret of secrets) {
        if (text.includes(`process.env.${secret}`)) offenders.push(`${relative}: ${secret}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("contains no literal Stripe or Clerk credential anywhere in the source", () => {
    // Catches a key pasted in while debugging. `sk_test_` is included: a test
    // key is still a credential and still must not be committed.
    const patterns = [/sk_live_[A-Za-z0-9]/, /rk_live_[A-Za-z0-9]/, /whsec_[A-Za-z0-9]{16}/, /sk_[A-Za-z0-9]{24}/];
    const offenders: string[] = [];
    for (const { relative, text } of SOURCE_FILES) {
      for (const pattern of patterns) {
        if (pattern.test(text)) offenders.push(`${relative}: ${pattern}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("keeps the client-visible env surface to NEXT_PUBLIC_ names only", () => {
    const publicNames = new Set<string>();
    for (const { text } of SOURCE_FILES) {
      for (const match of text.matchAll(/process\.env\.(NEXT_PUBLIC_[A-Z0-9_]+)/g)) {
        publicNames.add(match[1]);
      }
    }
    // Every one of these is public by design. A new entry here is a decision
    // that deserves the test failure that surfaces it.
    expect([...publicNames].sort()).toEqual([
      "NEXT_PUBLIC_API_BASE",
      "NEXT_PUBLIC_BILLING_ENABLED",
      "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
      "NEXT_PUBLIC_SITE_URL",
    ]);
  });
});

describe("route policy", () => {
  it("keeps every public research route public", () => {
    // The product decision this test defends: the research is free and is not
    // gated on an account or a subscription.
    for (const route of PUBLIC_RESEARCH_PATHS) {
      expect(isProtectedPath(route), `${route} must stay public`).toBe(false);
    }
  });

  it("protects the account routes", () => {
    expect(isProtectedPath("/account")).toBe(true);
    expect(isProtectedPath("/account/")).toBe(true);
    expect(isProtectedPath("/account/billing")).toBe(true);
    expect(isProtectedPath("/account/billing/anything")).toBe(true);
  });

  it("does not protect a lookalike path", () => {
    // `startsWith("/account")` alone would have gated these; the exact-match
    // plus `/account/` prefix is deliberate.
    expect(isProtectedPath("/accounts")).toBe(false);
    expect(isProtectedPath("/account-recovery")).toBe(false);
  });

  it("never gates the Stripe webhook", () => {
    // An auth redirect in front of it would swallow every event while looking
    // like a successful delivery to Stripe.
    expect(isAlwaysOpenPath("/api/stripe/webhook")).toBe(true);
    expect(isAlwaysOpenPath("/sign-in")).toBe(true);
    expect(isAlwaysOpenPath("/sign-up/continue")).toBe(true);
  });
});

describe("built client bundle", () => {
  const staticDir = path.join(ROOT, ".next", "static");
  let bundles: string[] = [];
  try {
    bundles = walk(staticDir, (file) => file.endsWith(".js"));
  } catch {
    bundles = [];
  }

  it.skipIf(bundles.length === 0)("ships no server secret VALUE to the browser", () => {
    // Runs against a real `next build` output when one exists; skipped rather
    // than failed when the tests run before a build (`npm run verify` builds
    // first).
    //
    // It matches secret *values*, not variable names. Clerk's isomorphic code
    // legitimately contains the expression `process.env.CLERK_SECRET_KEY` in a
    // browser chunk: Next inlines a literal only for NEXT_PUBLIC_ names, so
    // that expression evaluates to undefined in the browser and the name alone
    // discloses nothing. Failing on the name would be a false positive that
    // trains everyone to ignore this test.
    //
    // `PMVL_BUNDLE_CANARY` lets a build prove the stronger property end to end:
    // build with every secret set to a distinctive value, export that value
    // here, and this asserts none of it survived into the client bundle. The
    // procedure is in docs/saas-setup.md.
    const canary = process.env.PMVL_BUNDLE_CANARY;
    const forbidden = [
      /sk_live_[A-Za-z0-9]/,
      /sk_test_[A-Za-z0-9]/,
      /rk_(live|test)_[A-Za-z0-9]/,
      /whsec_[A-Za-z0-9]/,
      ...(canary ? [new RegExp(canary.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))] : []),
    ];
    const offenders: string[] = [];
    for (const bundle of bundles) {
      const text = readFileSync(bundle, "utf8");
      for (const pattern of forbidden) {
        if (pattern.test(text)) {
          offenders.push(`${path.relative(ROOT, bundle)}: ${pattern}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe("mobile layout regressions", () => {
  // Layout cannot be measured in jsdom, so the live measurement at five
  // viewports is recorded in the pull request. What IS checkable here is the
  // shape of the two regressions this repository has actually shipped: an
  // element sized to the viewport, and an overflow container that cannot
  // scroll because its flex parent gave it `min-width: auto`.
  it("uses no viewport-width sizing that can exceed the document", () => {
    const offenders = SOURCE_FILES.filter(({ text }) =>
      /className="[^"]*\b(w-screen|min-w-screen)\b/.test(text),
    ).map(({ relative }) => relative);
    expect(offenders).toEqual([]);
  });

  it("keeps every table inside a horizontal scroll container", () => {
    // The regression this catches, measured on both this branch and main:
    // /arbitrage?view=diagnostics rendered a bare <table> whose cells do not
    // wrap. Its min-content width was 413px, so on a 375px viewport the
    // overflow escaped to the document root and scrolled the whole page
    // sideways instead of scrolling inside the table.
    //
    // Every other table on the site is wrapped in `.table-wrap`
    // (min-w-0 max-w-full w-full overflow-x-auto). This asserts the one that
    // was not stays wrapped, and that a new one cannot quietly skip it.
    const offenders: string[] = [];
    for (const { relative, text } of SOURCE_FILES) {
      let index = text.indexOf("<table");
      while (index !== -1) {
        // Look back far enough to catch the wrapper element and any comment
        // between it and the table.
        const preceding = text.slice(Math.max(0, index - 400), index);
        if (!/table-wrap/.test(preceding)) {
          const line = text.slice(0, index).split("\n").length;
          offenders.push(`${relative}:${line}`);
        }
        index = text.indexOf("<table", index + 1);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("gives every scrolling flex item a min-width of zero", () => {
    // The exact regression this repository shipped twice: a flex item defaults
    // to `min-width: auto`, so `overflow-x-auto` on one never engages - instead
    // of scrolling internally it forces the whole document sideways. A `<pre>`
    // or a plain block with `overflow-x-auto` is unaffected and is not flagged.
    const offenders: string[] = [];
    for (const { relative, text } of SOURCE_FILES) {
      for (const match of text.matchAll(/className="([^"]*\boverflow-x-auto\b[^"]*)"/g)) {
        const classes = match[1];
        const isFlexItem = /\bflex-(1|auto|initial)\b|\bshrink\b|\bgrow\b/.test(classes);
        if (isFlexItem && !/\bmin-w-0\b/.test(classes)) {
          offenders.push(`${relative}: ${classes}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
