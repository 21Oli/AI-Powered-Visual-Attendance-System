import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ThemeToggle } from "../src/components/ui/ThemeToggle";
import { THEME_ATTRIBUTE, THEME_STORAGE_KEY } from "../src/lib/theme";

const appliedTheme = (): string | null =>
  document.documentElement.getAttribute(THEME_ATTRIBUTE);

describe("ThemeToggle", () => {
  it("applies the system theme on mount when nothing is stored", () => {
    render(<ThemeToggle />);
    // The setup stubs `prefers-color-scheme: dark` to false.
    expect(appliedTheme()).toBe("light");
  });

  it("renders without touching the document, so hydration cannot mismatch", () => {
    const html = renderToString(<ThemeToggle />);
    expect(html).toContain("Toggle colour theme");
    expect(html).not.toContain("Switch to");
  });

  it("switches the theme and persists the explicit choice", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);
    expect(appliedTheme()).toBe("light");

    await user.click(screen.getByRole("button"));

    expect(appliedTheme()).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(
      screen.getByRole("button", { name: "Switch to light theme" }),
    ).toBeDefined();

    await user.click(screen.getByRole("button"));

    expect(appliedTheme()).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });

  it("honours a previously stored preference over the OS preference", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    render(<ThemeToggle />);
    expect(appliedTheme()).toBe("dark");
    expect(
      screen.getByRole("button", { name: "Switch to light theme" }),
    ).toBeDefined();
  });

  it("ignores an unrecognised stored value", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "midnight");
    render(<ThemeToggle />);
    expect(appliedTheme()).toBe("light");
  });

  it("is reachable and operable by keyboard", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    const button = screen.getByRole("button");
    await user.tab();
    expect(document.activeElement).toBe(button);

    await user.keyboard("{Enter}");
    expect(appliedTheme()).toBe("dark");
  });
});
