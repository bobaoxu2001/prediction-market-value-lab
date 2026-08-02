/**
 * Stand-in for the `server-only` package.
 *
 * The real module throws at build time when a client component imports it,
 * which is precisely the protection we want in production and precisely the
 * thing that cannot work inside a test runner with no client/server graph.
 * `tests/security.test.ts` asserts the real import is present in every module
 * that holds a secret, so aliasing it here does not weaken that guarantee.
 */
export {};
