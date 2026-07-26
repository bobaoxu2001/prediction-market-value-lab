import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // A restrained terminal palette: one accent for positive edge, one for risk.
        edge: { DEFAULT: "#0f9d58", dark: "#34d399" },
        risk: { DEFAULT: "#c5221f", dark: "#f87171" },
        warn: { DEFAULT: "#b45309", dark: "#fbbf24" },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "SF Mono", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
