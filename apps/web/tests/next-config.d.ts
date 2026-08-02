/**
 * `next.config.mjs` is plain JavaScript with no types, and `security.test.ts`
 * imports it to call `headers()` — asserting on the real exported function
 * rather than on a regex over the file, so a header that stops being emitted
 * fails the test rather than passing on a stale string match.
 */
declare module "@/next.config.mjs" {
  interface NextHeaderRule {
    source: string;
    headers: Array<{ key: string; value: string }>;
  }
  const config: { headers: () => Promise<NextHeaderRule[]> };
  export default config;
}
