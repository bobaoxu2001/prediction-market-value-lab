"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * The current theme lives on `<html class="dark">`, set by an inline script before
 * paint. The button deliberately synchronises after hydration: the server cannot
 * know the visitor's stored or OS preference, and pretending it can left the page
 * visibly dark while the control still said "Dark" and offered to switch to dark.
 *
 * A MutationObserver keeps the label honest if another surface changes the class.
 * The click handler also updates state directly, so the control never depends on a
 * future mutation notification to reflect the action it just took.
 */
export function ThemeToggle() {
  const [dark, setDark] = useState(false);

  useEffect(() => {
    const sync = () =>
      setDark(document.documentElement.classList.contains("dark"));
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => observer.disconnect();
  }, []);

  const toggle = useCallback(() => {
    const next = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", next);
    setDark(next);
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
      className="shrink-0 rounded border border-line px-2 py-1 text-xs text-ink-muted hover:bg-sunken dark:hover:bg-sunken"
    >
      {dark ? "Light" : "Dark"}
    </button>
  );
}
