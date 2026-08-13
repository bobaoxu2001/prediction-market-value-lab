/**
 * Bundles the two entry points into `dist/`.
 *
 * They are built in **different formats**, and that is not a detail.
 *
 * A MV3 content script is loaded as a *classic* script — there is no way to mark
 * it as a module — so a top-level `export` in it is a SyntaxError and the whole
 * file fails to parse. Chrome reports nothing useful on the page; the script
 * simply never runs.
 *
 * That is exactly what shipped: both files were built as ESM, `content.js` ended
 * with `export { draw, refreshTimer, reload };`, and the overlay never appeared
 * in a real browser. Every unit test passed throughout, because they import the
 * source modules and never touch the bundle. `scripts/verify-loaded.mjs` is what
 * caught it, by loading the packaged extension into Chrome.
 *
 * The service worker is the opposite case: `manifest.json` declares
 * `"type": "module"`, so it must be ESM.
 */
import { build } from "esbuild";

const shared = {
  bundle: true,
  target: "chrome110",
  // BigInt literals are load-bearing: the exact-decimal arithmetic the whole
  // cost stack rests on is written with them, and a downlevel target would
  // silently rewrite it into floats.
  supported: { bigint: true },
  logLevel: "info",
};

// Classic script. `iife` also discards the module's exports rather than
// emitting statements a content script cannot parse.
await build({
  ...shared,
  entryPoints: ["src/content.ts"],
  outfile: "dist/content.js",
  format: "iife",
});

// Module, as the manifest declares.
await build({
  ...shared,
  entryPoints: ["src/background.ts"],
  outfile: "dist/background.js",
  format: "esm",
});
