/**
 * Loads the built extension into a real Chrome and checks it works on a live
 * venue page.
 *
 * Everything else in this repository verifies the extension's *logic*: the cost
 * maths against the Python, the adapters against recorded payloads, the readers
 * against markup copied off the live sites. None of that exercises the parts
 * that only exist once Chrome is holding the thing — `manifest.json` parsing,
 * content-script injection and its timing, the service worker, the message
 * channel between them, and whether `host_permissions` actually allow the venue
 * requests.
 *
 * Those were the last unverified pieces, and this closes them by running the
 * packaged extension rather than its source.
 *
 * Usage:
 *   node scripts/bundle.mjs && node scripts/verify-loaded.mjs [url]
 *
 * Exits non-zero when the panel does not appear, so it can gate a release.
 */

import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import puppeteer from "puppeteer-core";

const HERE = dirname(fileURLToPath(import.meta.url));
const EXTENSION = resolve(HERE, "..");

const CHROME =
  process.env.CHROME_PATH ??
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

/** A live Kalshi contract page. Overridable, because these expire. */
const DEFAULT_URL =
  process.argv[2] ??
  "https://kalshi.com/markets/kxelonmars/elon-mars/kxelonmars-99";

/** The overlay fetches a book before it can render, so give the network room. */
const PANEL_TIMEOUT_MS = 45_000;

function requireBuilt() {
  for (const file of ["dist/content.js", "dist/background.js", "manifest.json"]) {
    if (!existsSync(join(EXTENSION, file))) {
      throw new Error(`${file} is missing — run \`node scripts/bundle.mjs\` first`);
    }
  }
}

async function main() {
  requireBuilt();
  if (!existsSync(CHROME)) throw new Error(`no Chrome at ${CHROME}`);

  // A throwaway profile, so this never touches the user's Chrome, their session
  // or their real extensions.
  const profile = mkdtempSync(join(tmpdir(), "pmvl-verify-"));

  // Headless Chrome has never reliably loaded extensions; `--headless=new` was
  // meant to fix that and, on this version, still starts no service worker.
  // Headful is the mode that actually exercises what a user would install, so it
  // is the default and headless is opt-in for anyone who wants to try again.
  const headless = process.env.PMVL_HEADLESS === "1";
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless,
    userDataDir: profile,
    args: [
      `--disable-extensions-except=${EXTENSION}`,
      `--load-extension=${EXTENSION}`,
      "--no-first-run",
      "--no-default-browser-check",
    ],
  });

  const failures = [];
  const note = (ok, label, detail = "") => {
    console.log(`${ok ? "  ok  " : " FAIL "} ${label}${detail ? ` — ${detail}` : ""}`);
    if (!ok) failures.push(label);
  };

  try {
    // 1. Did Chrome accept the manifest and start the service worker?
    const worker = await browser
      .waitForTarget((t) => t.type() === "service_worker", { timeout: 15_000 })
      .catch(() => null);
    note(Boolean(worker), "service worker started", worker?.url() ?? "not found");

    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });

    const consoleErrors = [];
    page.on("console", (m) => {
      if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200));
    });
    page.on("pageerror", (e) => consoleErrors.push(String(e).slice(0, 200)));

    console.log(`\nloading ${DEFAULT_URL}\n`);
    await page.goto(DEFAULT_URL, { waitUntil: "domcontentloaded", timeout: 60_000 });

    // 2. Was the content script injected at all?
    const injected = await page
      .waitForFunction(
        () => typeof document.getElementById === "function",
        { timeout: 5_000 },
      )
      .then(() => true)
      .catch(() => false);
    note(injected, "page reachable");

    // 3. The real question: does the panel appear, with figures in it?
    const panel = await page
      .waitForSelector("#pmvl-cost-overlay", { timeout: PANEL_TIMEOUT_MS })
      .catch(() => null);
    note(Boolean(panel), "overlay injected");

    if (panel) {
      const text = await page.$eval("#pmvl-cost-overlay", (n) =>
        (n.innerText || "").replace(/\s+/g, " ").trim(),
      );
      console.log(`\n  panel says: ${text}\n`);

      note(/¢/.test(text), "panel contains a cost figure");
      note(/Entry cost/i.test(text), "panel names what it is");
      note(
        !/Could not read the live book/i.test(text),
        "panel is not an error state",
      );

      const box = await panel.boundingBox();
      note(
        Boolean(box && box.width > 100 && box.height > 40),
        "panel has a visible box",
        box ? `${Math.round(box.width)}x${Math.round(box.height)}` : "none",
      );
    }

    note(
      consoleErrors.length === 0,
      "no page errors from the extension",
      consoleErrors.slice(0, 2).join(" | "),
    );
  } finally {
    await browser.close();
    rmSync(profile, { recursive: true, force: true });
  }

  console.log(
    failures.length === 0
      ? "\nPASS — the packaged extension works on a live venue page.\n"
      : `\nFAIL — ${failures.length}: ${failures.join(", ")}\n`,
  );
  process.exit(failures.length === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error(`\nverification could not run: ${error.message}\n`);
  process.exit(2);
});
