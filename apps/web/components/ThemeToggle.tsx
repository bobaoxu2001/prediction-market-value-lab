"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * The current theme lives on `<html class="dark">`, set by an inline script that
 * runs before paint so the page never flashes the wrong colours. That makes the
 * class list an external store, not React state.
 *
 * Reading it with `useState` + `useEffect` meant rendering `false`, then
 * immediately setting the real value - a cascading render on every mount, and the
 * button briefly claiming the wrong theme. `useSyncExternalStore` reads it during
 * render instead, and returns `false` on the server where there is no document,
 * which is also what the pre-paint script assumes before it runs.
 */
function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
  return () => observer.disconnect();
}

const isDark = () => document.documentElement.classList.contains("dark");
const isDarkOnServer = () => false;

export function ThemeToggle() {
  const dark = useSyncExternalStore(subscribe, isDark, isDarkOnServer);

  const toggle = useCallback(() => {
    const next = !document.documentElement.classList.contains("dark");
    // The MutationObserver above turns this into the new rendered value, so the
    // class list stays the single source of truth rather than being mirrored.
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("pmvl-theme", next ? "dark" : "light");
    } catch {
      /* private browsing - the in-session toggle still works */
    }
  }, []);

  return (
    <button
      onClick={toggle}
      aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
      className="shrink-0 rounded border border-neutral-300 px-2 py-1 text-xs text-neutral-600 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
    >
      {dark ? "Light" : "Dark"}
    </button>
  );
}
