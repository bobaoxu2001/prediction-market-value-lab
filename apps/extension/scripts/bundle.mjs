/**
 * Bundles the two entry points into `dist/`.
 *
 * Chrome loads `content.js` and `background.js` as single files, so the module
 * graph has to be flattened. esbuild rather than a framework because there are
 * exactly two entry points, no framework, and no dependencies to resolve.
 */
import { build } from "esbuild";

await build({
  entryPoints: ["src/content.ts", "src/background.ts"],
  outdir: "dist",
  bundle: true,
  format: "esm",
  target: "chrome110",
  // BigInt literals are load-bearing here; a downlevel target would break the
  // exact arithmetic the whole port rests on.
  supported: { bigint: true },
  logLevel: "info",
});
