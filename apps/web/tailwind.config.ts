import type { Config } from "tailwindcss";

/**
 * Colours resolve through CSS custom properties defined in globals.css, using the
 * `rgb(var(--x) / <alpha-value>)` form so opacity modifiers keep working
 * (`bg-edge/15`, `border-warn/60` are both already in use).
 *
 * The payoff is that `dark:` variants disappear from colour decisions: a token
 * flips once under `.dark` instead of every element restating the theme.
 */
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Grounds and surfaces.
        sunken: token("surface-sunken"),
        base: token("surface-base"),
        raised: token("surface-raised"),

        // Border hierarchy.
        line: {
          DEFAULT: token("line"),
          subtle: token("line-subtle"),
          strong: token("line-strong"),
        },

        // Text hierarchy.
        ink: {
          DEFAULT: token("ink"),
          muted: token("ink-muted"),
          faint: token("ink-faint"),
        },

        accent: {
          DEFAULT: token("accent"),
          ink: token("accent-ink"),
        },

        // Semantic states.
        edge: {
          DEFAULT: token("edge"),
          // `-dark` aliases resolve to the same token. Kept so the existing
          // `dark:text-edge-dark` markup keeps compiling while pages migrate off
          // it; in dark mode the token has already flipped, so the two agree.
          dark: token("edge"),
        },
        risk: { DEFAULT: token("risk"), dark: token("risk") },
        warn: { DEFAULT: token("warn"), dark: token("warn") },
        info: { DEFAULT: token("info"), dark: token("info") },

        // Provenance and confidence, first-class in a research product.
        demo: token("demo"),
        live: token("live"),
        stale: token("stale"),
        unverified: token("unverified"),

        focus: token("focus"),
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        serif: ["var(--font-serif)"],
        mono: ["var(--font-mono)"],
      },
    },
  },
  plugins: [],
};
export default config;
