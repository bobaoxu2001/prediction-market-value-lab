import { defineConfig } from "vitest/config";

// The content-script tests navigate with history.pushState, which jsdom only
// allows within the same origin as the test document. Pinning the jsdom URL to
// a Polymarket page makes those navigations same-origin. The URL deliberately
// carries NO side parameter: the import-time reload must fail closed and fetch
// nothing, which is itself asserted.
export default defineConfig({
  test: {
    environmentOptions: {
      jsdom: {
        url: "https://polymarket.com/market/initial",
      },
    },
  },
});
