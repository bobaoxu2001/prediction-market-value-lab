import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

// `next lint` was removed in Next 16, so the old `npm run lint` script silently
// did nothing while CI reported it green - the CI step also ended in `|| true`.
// ESLint is invoked directly now and CI no longer swallows its exit code.
// eslint-config-next v16 ships flat-config arrays, spread as-is.
const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...coreWebVitals,
  ...typescript,
  {
    rules: {
      // API responses are validated server-side by Pydantic; restating every
      // field's type in TS would duplicate that contract without strengthening
      // it, so `any` at the fetch boundary is a warning, not an error.
      "@typescript-eslint/no-explicit-any": "warn",
      // Unused imports are how the pre-snapshot ageLabel/relativeTime helpers
      // stayed referenced after being replaced. That is worth failing on.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
];

export default config;
