// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ThemeToggle } from "@/components/ThemeToggle";

afterEach(() => {
  document.documentElement.classList.remove("dark");
  localStorage.clear();
});

describe("ThemeToggle", () => {
  it("hydrates from the class applied by the pre-paint theme script", async () => {
    document.documentElement.classList.add("dark");
    render(<ThemeToggle />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Switch to light mode" }),
      ).toBeTruthy();
    });
    expect(screen.getByText("Light")).toBeTruthy();
  });

  it("switches the document theme and persists the choice", async () => {
    render(<ThemeToggle />);
    const button = screen.getByRole("button", { name: "Switch to dark mode" });

    fireEvent.click(button);

    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("pmvl-theme")).toBe("dark");
    expect(
      screen.getByRole("button", { name: "Switch to light mode" }),
    ).toBeTruthy();
  });
});
